"""bot_recovery_monitor — emit info-tier Signals for bot self-recoveries.

heal already detects when a previously-recurring error (Anthropic context
overflow, provider 429 storm, etc.) stops recurring — the offending log
line drops out of ``recent_errors`` and a sticky entry lands in the bot's
status file under ``recovered_alerts`` (24h decay). The Maintenance ·
Status tab renders these as green chips ("Anthropic: context overflow ·
recovered 10h ago").

That data was previously trapped in the status file: not in the Signal
store, so the alert notifier couldn't see it, evo's narrative on Home
couldn't reference it, and there was no historical trail after the 24h
sticky window. This producer is the bridge — it walks each bot's status
file and emits an info-tier Signal per active recovery entry. The Signal
auto-resolves when the recovery drops out of the status file (decay or
re-occurrence of the underlying error).

Reads on-disk telemetry only (no LLM):
  - {shared_dir}/status/{bot_id}.json — heal-written per-bot status,
    contains ``recovered_alerts: [{provider, condition, error_ts,
    error_line, recovered_ts}, ...]``

One signal type, under producer ``bot_recovery_monitor``:

  bot_recovered        — bot is currently in heal's "recovered" state for
                         a specific (provider, condition). Severity info.

Pod-wide runner that iterates all member bots. Cheap; designed for
hourly launchd cadence aligned with heal's 24h decay window.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable

from evolve_config import (
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
)
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "bot_recovery_monitor"


# ─────────────────────────────────────────────────────────────────────────────
# Status-file reader
# ─────────────────────────────────────────────────────────────────────────────


def _read_status_file(shared_dir: Path, bot_id: str) -> dict | None:
    path = shared_dir / "status" / f"{bot_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────────────


# Producer + condition slugify — keep signatures filesystem-safe and stable.
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("_", (s or "").strip()).strip("_").lower() or "unknown"


def _humanize_condition(provider: str | None, condition: str) -> str:
    """Render a human-readable name for the recovered finding.

    The condition slug from heal looks like ``context_overflow`` /
    ``rate_limited`` / ``invalid_target``. Capitalize + replace
    underscores; prepend the provider when meaningful.
    """
    pretty_cond = (condition or "").replace("_", " ").strip()
    if not pretty_cond:
        pretty_cond = "an error"
    else:
        pretty_cond = pretty_cond[0].upper() + pretty_cond[1:]
    if provider:
        # Capitalize provider's first letter ("Anthropic context overflow").
        pp = provider.strip()
        if pp:
            pp = pp[0].upper() + pp[1:]
            return f"{pp} {pretty_cond.lower()}"
    return pretty_cond


def build_signal_for_recovery(
    bot_id: str,
    entry: dict,
) -> dict | None:
    """Render a Signal spec for a single recovered_alerts entry, or None
    if the entry is missing the fields we need."""
    provider = entry.get("provider") or None
    condition = entry.get("condition") or ""
    if not condition:
        return None
    human = _humanize_condition(provider, condition)
    error_ts = entry.get("error_ts") or ""
    recovered_ts = entry.get("recovered_ts") or ""
    error_line = (entry.get("error_line") or "").strip()
    scope_key = f"{bot_id}:{_slug(provider or '')}:{_slug(condition)}"
    title = f"{bot_id} recovered from {human}"
    body_lines = [
        f"`{bot_id}` recovered from a previous error condition.",
        "",
        f"Provider: `{provider or '(none)'}`",
        f"Condition: `{condition}`",
    ]
    if error_ts:
        body_lines.append(f"First seen: `{error_ts}`")
    if recovered_ts:
        body_lines.append(f"Last success: `{recovered_ts}`")
    if error_line:
        body_lines += [
            "",
            "Error line (sample):",
            "```",
            error_line[:280],
            "```",
        ]
    body_lines += [
        "",
        "Info-tier — no operator action required. Surfaced for the "
        "history trail and so the same condition recurring is recognizable.",
    ]
    body = "\n".join(body_lines)
    return dict(
        signature=make_signature(PRODUCER, "bot_recovered", scope_key),
        producer=PRODUCER,
        type="bot_recovered",
        flavor="maintenance",
        severity="info",
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details=dict(
            provider=provider,
            condition=condition,
            error_ts=error_ts,
            recovered_ts=recovered_ts,
            # Severity framework: operations vector, magnitude 0 —
            # advisory/historical record of a recovery, never crowds
            # the narrative. Spec: §2.3 operations magnitude 0.
            vector="operations",
            magnitude=0,
            # Operator docs (per the per-monitor doc convention from
            # PR #1603). Lighter touch than failure-type producers
            # because this is a recovery surface, not a problem —
            # what_it_means tells the operator why it's surfaced at
            # all, fix_steps is the no-op pointer + dismiss path.
            what_it_means=(
                f"`{bot_id}` previously hit a recurring "
                f"`{condition}` error from "
                f"`{provider or 'an unknown provider'}` and has now "
                "stopped seeing it for long enough that heal's "
                "self-recovery tracker considers it cleared. This "
                "Signal is the history trail for that recovery — so "
                "the same condition recurring is recognizable, and "
                "operators have evidence the underlying problem "
                "actually self-resolved (vs. just falling out of the "
                "log window)."
            ),
            fix_steps=(
                "1. No action required — this is an info-tier "
                "history record, not a fault\n"
                "2. If you want it off the Alerts page sooner than "
                "the 24h decay, dismiss it via the Alerts UI\n"
                "3. If the same `provider`/`condition` recurs, the "
                "underlying error fires a fresh Signal from "
                "`heal`/`audit`/the relevant monitor — investigate "
                "that one, not this recovery record"
            ),
        ),
    )


def collect_for_bot(
    shared_dir: Path,
    bot_id: str,
    *,
    status_loader=_read_status_file,
) -> list[dict]:
    """Read the bot's heal status file and return one Signal spec per
    active recovered_alerts entry. Empty list when no recoveries (or
    the status file is missing)."""
    status = status_loader(shared_dir, bot_id)
    if not isinstance(status, dict):
        return []
    entries = status.get("recovered_alerts")
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        spec = build_signal_for_recovery(bot_id, entry)
        if spec:
            out.append(spec)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def run(
    shared_dir: Path,
    config: dict[str, Any],
    *,
    bots: Iterable[str] | None = None,
    dry_run: bool = False,
    status_loader=_read_status_file,
) -> tuple[set[str], int, int]:
    """Walk all member bots, emit one Signal per active recovery.

    Returns ``(kept_signatures, n_fired, n_resolved)``.
    """
    if bots is None:
        primary = get_primary(config)
        members = get_members(config)
        bots = ([primary] if primary and primary not in members else []) + members
        bots = [b for b in bots if b]

    kept: set[str] = set()
    n_fired = 0
    for bot_id in bots:
        for d in collect_for_bot(shared_dir, bot_id, status_loader=status_loader):
            kept.add(d["signature"])
            n_fired += 1
            if dry_run:
                print(json.dumps({"would_observe": d}, default=str), flush=True)
                continue
            try:
                signals_store.observe(shared_dir, **d)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[bot_recovery_monitor] observe failed for {d['signature']}: {exc}",
                    flush=True,
                )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: recovery decayed out of heal's status window",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[bot_recovery_monitor] sweep_resolve failed: {exc}", flush=True)

    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="bot_recovery_monitor — info-tier Signals for heal recoveries",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument("--bot", default=None, help="Run only for this bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    bots = [args.bot] if args.bot else None
    kept, n_fired, n_resolved = run(
        shared_dir, config, bots=bots, dry_run=args.dry_run
    )
    if args.dry_run:
        print(f"[bot_recovery_monitor] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[bot_recovery_monitor] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
