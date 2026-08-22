"""bootstrap_size_monitor — Signal producer for OC-ingested bootstrap files
growing past the bot's injection caps (content silently dead).

Motivated by the 2026-08-01 live finding: the primary bot's workspace
AGENTS.md grew to 128k chars against its effective ``bootstrapMaxChars`` of
40,000. OpenClaw logged "workspace bootstrap file AGENTS.md is N chars (limit
40000); truncating in injected context" — and 18 of 22 sections, including
every anti-confabulation rule, were silently absent from the bot's context for
an unknown period. Nothing in Evolve watched the ratio, so the only witness
was a gateway log line nobody reads. This monitor makes that ratio a Signal.

What it measures
================

Per bot, per OC-INGESTED bootstrap file — the FIXED filename set OpenClaw
injects into per-turn context (``context_health.OC_BOOTSTRAP_INGESTED``:
AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, USER.md, HEARTBEAT.md,
BOOTSTRAP.md, MEMORY.md; workspace-root files outside that set cost nothing
per turn and are ignored here) — the file's CHAR count against the bot's
*effective* caps from its ``openclaw.json``:

  * ``agents.defaults.bootstrapMaxChars`` — OC's per-file injection cap
    (OC default 12,000; deploy.py sets 40,000 on primary bots).
  * ``agents.defaults.bootstrapTotalMaxChars`` — OC's cap on the combined
    injected total (OC default 60,000; ``balanced`` profile sets 100,000; an
    explicit null disables it, per the ``unrestricted-debug`` cost profile).

Thresholds mirror OC's own near-limit posture:

  * ratio ≥ 0.85 → **warn** — the file is about to start losing content.
  * ratio ≥ 1.00 → **alert** — OC is truncating NOW; everything past the cap
    is silently dead in every turn's context.

The total check sums ``min(size, per_file_cap)`` across the ingested files —
i.e. what OC would actually inject after per-file truncation — against
``bootstrapTotalMaxChars``, with the same two thresholds.

Signal shape
============

  * ``bootstrap_truncation_risk`` — one per bot, rolling up every at-risk
    file plus the total-budget line. Scope ``bot``, producer
    ``bootstrap_size``, signature keyed on the bot id — so a second file
    crossing the threshold merges into the same Signal, and a bot trimmed
    back under 85% drops out of ``kept_signatures`` and auto-archives.
    Severity escalates per-emit to ``alert`` when any finding is at/over
    100% (producer default is warn).
  * ``bootstrap_size_unreadable`` — one per bot whose ``openclaw.json`` or
    any ingested workspace file cannot be read even via the sudo-cat
    fallback. A monitor that can't read must NOT look clean (the
    silent-monitor-drift lesson): a blind bot fires this instead, is
    excluded from the truncation sweep so its prior Signals persist, and the
    unreadable type itself sweeps over all attempted bots so a recovered
    bot's stale Signal clears.

Reads
=====

Bot homes are 0700; the ``evolve`` user reads via the macOS/Linux ACL that
``set_evolve_read_acl`` maintains, with ``sudo /bin/cat`` as the fallback for
a clamped ACL (the CLAUDE.md file-access contract). Char counts come from the
decoded text — OC's caps are in characters, not bytes — with a byte-length
fallback for a non-UTF-8 file (over-counts, which errs toward warning early).
The primary bot running under the ``evo`` OS account is handled by
``evolve_config.bot_home`` (``bots.<id>.user`` override), not by any
hardcoded path. Read-only throughout — this monitor never edits a workspace
file or an ``openclaw.json``; remediation (trim the file, or raise the cap)
is the operator's.

Related: docs/spec-context-observability-2026-07-30.md (the wider
context-observability plan; this producer satisfies its §Implementation
contract) and docs/spec-evolve-overhead-budget-2026-07-31.md §A1 (the
bootstrap-weight decomposition this monitor turns into an alarm).

Run as
======

    sudo -u evolve python3 packages/analyzer/bootstrap_size_monitor.py \\
        --network {shared_dir}/network.json

Installed hourly (evolve user, pod-wide) by
``analyzer_monitor_jobs.install_bootstrap_size_monitor``; watched by
``monitor_coverage``'s producer-liveness layer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from context_health import OC_BOOTSTRAP_INGESTED
from evolve_config import bot_home
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "bootstrap_size"
TRUNCATION_TYPE = "bootstrap_truncation_risk"
UNREADABLE_TYPE = "bootstrap_size_unreadable"

# OC's compiled defaults when the openclaw.json key is absent — per-file
# injection cap and combined-total cap (docs/spec-evo-oc-native-2026-05-19.md
# §"Bootstrap budget"). An explicitly-null key means the cap is DISABLED
# (the unrestricted-debug cost profile writes bootstrapTotalMaxChars: null).
OC_DEFAULT_BOOTSTRAP_MAX_CHARS = 12_000
OC_DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS = 60_000

# OC's own near-limit posture: it starts warning at 85% of a cap, and
# truncates at 100%. warn = about to lose content; alert = losing it NOW.
WARN_RATIO = 0.85
ALERT_RATIO = 1.0

# Sentinels for the tri-state file read. MISSING (file genuinely absent — not
# every bot has every bootstrap file) is a clean skip; None means the read
# FAILED (EACCES even via sudo-cat), which must never collapse to "no file" —
# that is exactly how a clamped ACL would silently sweep-resolve a live
# truncation Signal on the one file too big to matter.
_MISSING = object()


# ── Reads (ACL first, sudo-cat fallback — the CLAUDE.md contract) ──────────


def _read_chars(path: Path) -> "int | None | object":
    """Char count of ``path``: int, ``_MISSING`` (absent), or None (unreadable).

    Direct read first (the evolve ACL), then ``sudo /bin/cat``. A
    ``FileNotFoundError`` on the DIRECT read is trusted as genuinely missing
    only because traversal succeeded; once we are in the sudo fallback,
    "No such file" from cat is the only missing evidence left.
    """
    try:
        return len(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _MISSING
    except UnicodeDecodeError:
        # Non-UTF-8 content: byte length over-counts chars, erring toward an
        # early warning rather than a silent miss.
        try:
            return len(path.read_bytes())
        except OSError:
            return None
    except OSError:
        return _sudo_read_chars(path)


def _sudo_read_chars(path: Path) -> "int | None | object":
    """The ``sudo /bin/cat`` leg of :func:`_read_chars`."""
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode == 0:
        return len(r.stdout)
    if "No such file" in (r.stderr or ""):
        return _MISSING
    return None


def _read_openclaw_config(bot_id: str, network: dict) -> "dict | None":
    """Parsed ``openclaw.json`` for a bot, or None when unreadable/unparseable.

    A MISSING config is also None: without it the effective caps are unknown
    (the primary's 40k override lives there), so guessing OC defaults would
    false-alert every intentionally-large file. The caller treats None as
    blind, which is the safe direction.
    """
    path = bot_home(bot_id, network) / ".openclaw" / "openclaw.json"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:  # PermissionError/FileNotFoundError — try the sudo path.
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if r.returncode != 0:
            return None
        text = r.stdout
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ── Effective caps + assessment (pure) ─────────────────────────────────────


def effective_limits(oc_config: dict) -> "tuple[int | None, int | None]":
    """``(per_file_cap, total_cap)`` from ``agents.defaults``.

    Key absent → OC's compiled default. Present but not a positive int
    (explicit null, 0) → that cap is disabled → None.
    """
    defaults = (oc_config.get("agents") or {}).get("defaults") or {}

    def _cap(key: str, oc_default: int) -> "int | None":
        if key not in defaults:
            return oc_default
        raw = defaults.get(key)
        return raw if isinstance(raw, int) and raw > 0 else None

    return (
        _cap("bootstrapMaxChars", OC_DEFAULT_BOOTSTRAP_MAX_CHARS),
        _cap("bootstrapTotalMaxChars", OC_DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS),
    )


@dataclass(frozen=True)
class Finding:
    """One at-risk bootstrap surface: a single file, or the combined total."""

    surface: str          # file name, or "<total>" for the combined budget
    chars: int
    limit: int
    level: str            # "warn" | "alert"

    @property
    def ratio(self) -> float:
        return self.chars / self.limit

    def to_dict(self) -> dict:
        return {
            "surface": self.surface,
            "chars": self.chars,
            "limit": self.limit,
            "ratio": round(self.ratio, 3),
            "level": self.level,
        }


def _level(chars: int, limit: int) -> "str | None":
    ratio = chars / limit
    if ratio >= ALERT_RATIO:
        return "alert"
    if ratio >= WARN_RATIO:
        return "warn"
    return None


def assess_bot(
    sizes: "dict[str, int]",
    per_file_cap: "int | None",
    total_cap: "int | None",
) -> "list[Finding]":
    """Findings for one bot's ingested-file sizes against its effective caps.

    ``sizes`` maps file name → char count (missing files already skipped).
    The total line sums what OC would actually inject — each file clamped to
    the per-file cap first — so an oversized single file doesn't double-fire
    the total.
    """
    findings: list[Finding] = []
    if per_file_cap:
        for name in sorted(sizes):
            level = _level(sizes[name], per_file_cap)
            if level:
                findings.append(Finding(name, sizes[name], per_file_cap, level))
    if total_cap:
        injected_total = sum(
            min(size, per_file_cap) if per_file_cap else size
            for size in sizes.values()
        )
        level = _level(injected_total, total_cap)
        if level:
            findings.append(Finding("<total>", injected_total, total_cap, level))
    return findings


# ── Signal payloads (pure) ─────────────────────────────────────────────────


def _truncation_signal(bot_id: str, findings: "list[Finding]") -> dict:
    """Per-bot rollup Signal for every at-risk bootstrap surface."""
    severity = "alert" if any(f.level == "alert" for f in findings) else "warn"
    truncating = [f for f in findings if f.level == "alert"]
    worst = max(findings, key=lambda f: f.ratio)
    if truncating:
        title = (
            f"{bot_id}: {worst.surface} at {worst.chars:,}/{worst.limit:,} chars "
            f"— bootstrap content silently dead"
        )
    else:
        title = (
            f"{bot_id}: {worst.surface} at {worst.ratio:.0%} of its "
            f"bootstrap cap — about to truncate"
        )

    lines = []
    for f in sorted(findings, key=lambda f: -f.ratio):
        state = "TRUNCATING" if f.level == "alert" else "near limit"
        lines.append(
            f"- `{f.surface}` — {f.chars:,} chars against a cap of "
            f"{f.limit:,} ({f.ratio:.0%}, {state})"
        )
    body = (
        f"`{bot_id}`'s OC-ingested bootstrap files are at or past the bot's "
        "effective injection caps (`agents.defaults.bootstrapMaxChars` / "
        "`bootstrapTotalMaxChars` in its openclaw.json):\n\n"
        + "\n".join(lines)
        + "\n\nOpenClaw truncates each file at the per-file cap when composing "
        "context — everything past the cap exists on disk but never reaches "
        "the model. When AGENTS.md is the file, the sections that die "
        "typically include the behavioral rules the operator most relies on "
        "(the 2026-08-01 incident lost every anti-confabulation rule this "
        "way, silently, for an unknown period). The `<total>` line is the "
        "combined post-truncation injection against the total budget.\n\n"
        "Fix: trim the file (move reference material out of the ingested "
        "set — workspace files outside OC's fixed bootstrap list cost "
        "nothing per turn), or deliberately raise the cap in the bot's "
        "openclaw.json. Either clears this Signal on the next tick."
    )
    return dict(
        signature=make_signature(PRODUCER, TRUNCATION_TYPE, bot_id),
        producer=PRODUCER,
        type=TRUNCATION_TYPE,
        severity=severity,
        scope="bot",
        bot_id=bot_id,
        incident_key=f"{PRODUCER}:{bot_id}",
        title=title,
        body=body,
        details=dict(
            findings=[f.to_dict() for f in findings],
            warn_ratio=WARN_RATIO,
            alert_ratio=ALERT_RATIO,
            what_it_means=(
                "OpenClaw injects a FIXED set of workspace bootstrap files "
                "into every turn's context, truncating each at "
                "bootstrapMaxChars and the combined total at "
                "bootstrapTotalMaxChars. A file at/over its cap is silently "
                "losing its tail in every turn — the bot behaves as if those "
                "sections were never written, and nothing else in Evolve "
                "notices."
            ),
            fix_steps=(
                "1. Open the listed file(s) in the bot's workspace and check "
                "what falls past the cap (the tail is what's dead).\n"
                "2. Trim: move reference/archive material into a "
                "non-ingested workspace file (anything outside OC's fixed "
                "bootstrap set is free) and keep the ingested file to the "
                "rules that must ride every turn.\n"
                "3. If the size is intentional, raise "
                "agents.defaults.bootstrapMaxChars (and the total budget) in "
                "the bot's openclaw.json instead — an explicit cap is a "
                "decision; a silent truncation is not.\n"
                "4. The Signal auto-resolves once every surface is back "
                "under 85% of its cap."
            ),
        ),
    )


def _unreadable_signal(bot_id: str, detail: str) -> dict:
    """Per-bot 'bootstrap-size check blind' Signal payload."""
    return dict(
        signature=make_signature(PRODUCER, UNREADABLE_TYPE, bot_id),
        producer=PRODUCER,
        type=UNREADABLE_TYPE,
        scope="bot",
        bot_id=bot_id,
        incident_key=f"{PRODUCER}:{bot_id}",
        title=f"{bot_id}: bootstrap-size check blind — workspace/config unreadable",
        body=(
            f"Could not read `{bot_id}`'s openclaw.json or one of its "
            "OC-ingested workspace bootstrap files (EACCES even via the "
            "sudo-cat fallback), so the bootstrap-size check cannot run for "
            "this bot. A monitor that can't read must not look clean — this "
            "Signal marks the blind spot, and any existing truncation "
            "Signals for the bot are left in place (we can't confirm they "
            "cleared).\n\n"
            f"Read error: {detail}\n\n"
            "Fix: ensure the evolve read ACL on `~/.openclaw` is intact "
            "(`sudo evolve-admin ensure-pod-perms`) and that the bot has "
            "been deployed. The Signal auto-resolves once the reads succeed."
        ),
        details=dict(
            error=detail,
            what_it_means=(
                "The bootstrap-size monitor is blind on this bot. Until the "
                "read is restored, a bootstrap file silently truncating past "
                "its injection cap — the exact condition this monitor exists "
                "to catch — would go unreported for this bot."
            ),
            fix_steps=(
                "1. Run `sudo evolve-admin ensure-pod-perms` to reassert the "
                "evolve read ACL on the bot's ~/.openclaw.\n"
                "2. Confirm the bot has been deployed (a fresh bot has no "
                "openclaw.json yet).\n"
                "3. The Signal auto-resolves on the next hourly tick once "
                "the reads succeed."
            ),
        ),
    )


# ── Orchestration ──────────────────────────────────────────────────────────


def scan_bot(bot_id: str, network: dict) -> "tuple[list[Finding] | None, str]":
    """``(findings, "")`` for a readable bot, or ``(None, why)`` when blind.

    Blind means: openclaw.json unreadable/unparseable (effective caps
    unknown), or ANY ingested file read-failed (a partial assessment could
    miss the one oversized file and wrongly sweep-resolve a live Signal).
    Missing files are a clean skip — not every bot carries every bootstrap
    file.
    """
    oc_config = _read_openclaw_config(bot_id, network)
    if oc_config is None:
        return None, "openclaw.json missing, unreadable, or unparseable"
    per_file_cap, total_cap = effective_limits(oc_config)

    workspace = bot_home(bot_id, network) / ".openclaw" / "workspace"
    sizes: dict[str, int] = {}
    unreadable: list[str] = []
    for name in sorted(OC_BOOTSTRAP_INGESTED):
        chars = _read_chars(workspace / name)
        if chars is _MISSING:
            continue
        if chars is None:
            unreadable.append(name)
            continue
        sizes[name] = chars  # type: ignore[assignment]
    if unreadable:
        return None, f"workspace bootstrap file(s) unreadable: {', '.join(unreadable)}"
    return assess_bot(sizes, per_file_cap, total_cap), ""


def run(
    network_path: Path,
    *,
    dry_run: bool = False,
    now: "datetime | None" = None,
) -> dict:
    """One pass: per bot, size every ingested bootstrap file vs the caps;
    emit + sweep."""
    now = now or datetime.now(timezone.utc)
    from platform_profile import get_profile

    try:
        network = json.loads(network_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"network.json unreadable: {exc}",
            }),
            flush=True,
        )
        return {"bots_scanned": 0, "signals_fired": 0}
    if not isinstance(network, dict):
        network = {}

    shared_dir = Path(network.get("sharedDir") or get_profile().shared_dir_default)
    members = network.get("members") or list((network.get("bots") or {}).keys())

    attempted: set[str] = set()
    readable: set[str] = set()
    kept_truncation: set[str] = set()
    kept_unreadable: set[str] = set()
    signals_fired = 0
    unreadable_count = 0
    finding_total = 0

    for bot_id in members:
        attempted.add(bot_id)
        findings, why_blind = scan_bot(bot_id, network)

        if findings is None:
            unreadable_count += 1
            sig = _unreadable_signal(bot_id, why_blind)
            kept_unreadable.add(sig["signature"])
            if dry_run:
                print(json.dumps({"would_observe": sig}, default=str), flush=True)
            else:
                try:
                    signals_store.observe(shared_dir, **sig)
                    signals_fired += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[bootstrap_size] observe(unreadable) failed for "
                          f"{bot_id}: {exc}", flush=True)
            # Not recorded as readable — the bot's prior truncation Signals
            # must survive the sweep below (blind tick, not a cleared one).
            continue

        readable.add(bot_id)
        if findings:
            finding_total += len(findings)
            sig = _truncation_signal(bot_id, findings)
            kept_truncation.add(sig["signature"])
            if dry_run:
                print(json.dumps({"would_observe": sig}, default=str), flush=True)
            else:
                try:
                    signals_store.observe(shared_dir, **sig)
                    signals_fired += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[bootstrap_size] observe(truncation) failed for "
                          f"{bot_id}: {exc}", flush=True)

    signals_resolved = 0
    if not dry_run:
        # Sweep truncation Signals only for bots we fully READ this run — a
        # blind bot's prior Signals must persist.
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_truncation,
                types={TRUNCATION_TYPE},
                bot_ids=readable,
                reason="auto-resolve: bootstrap files back under the caps",
            )
            signals_resolved += len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[bootstrap_size] sweep_resolve(truncation) failed: {exc}",
                  flush=True)
        # Sweep unreadable Signals over every bot we attempted, so a bot that
        # became readable this run has its stale unreadable Signal archived.
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_unreadable,
                types={UNREADABLE_TYPE},
                bot_ids=attempted,
                reason="auto-resolve: bootstrap files readable again",
            )
            signals_resolved += len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[bootstrap_size] sweep_resolve(unreadable) failed: {exc}",
                  flush=True)

    summary = {
        "bots_scanned": len(attempted),
        "readable": len(readable),
        "unreadable": unreadable_count,
        "at_risk_surfaces": finding_total,
        "signals_fired": signals_fired,
        "signals_resolved": signals_resolved,
        "ran_at": now.isoformat(),
    }
    print(json.dumps(summary, default=str), flush=True)
    return summary


def main(argv: "list[str] | None" = None) -> int:
    from platform_profile import get_profile

    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--network",
        default=str(Path(get_profile().shared_dir_default) / "network.json"),
        help="Path to network.json (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Signals that would be observed but don't write them.",
    )
    args = parser.parse_args(argv)

    network_path = Path(args.network)
    if not network_path.exists():
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"network.json not found at {network_path}",
            }),
            flush=True,
        )
        return 0

    run(network_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
