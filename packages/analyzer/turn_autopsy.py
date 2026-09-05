"""turn_autopsy.py — classify anomalous turns from records that already exist.

Design: internal/design-pa-turn-autopsy-2026-08-31.md (TA-1 + the TA-2 seed).
Born from the 2026-08-31 PoC-bot evening: every failure (output cap,
cross-provider failover, tool-call storm, apology trailer) was diagnosed by
a human reading gateway logs. This sweep reads the same records and turns
each anomaly class into ONE dedup'd Signal per (bot, cause, shape), with a
plain-language explanation and the fix — so the Alerts surface explains
instead of a human excavating.

Sources (read-only, per bot, via the evolve read ACL):
  * ``<home>/.openclaw/logs/gateway.err.log`` — the ``incomplete turn
    detected: … stopReason=…`` lines (tail-capped read).
  * ``<home>/.openclaw/agents/*/sessions/<uuid>.jsonl`` — assistant records
    (provider per turn) and toolCall records (name + argument digest),
    newest files first, count-capped.

Causes (v1 — exactly the classes with real incident evidence):
  * ``output_cap_hit``      stopReason=length — the model hit its output
                            ceiling; remediation: stamp ``maxTokens``.
  * ``incomplete_turn``     any other incomplete-turn stopReason — the
                            HONEST UNKNOWN bucket, never dropped: a rising
                            share here means the taxonomy needs work.
  * ``provider_swap``       consecutive assistant turns in one session
                            answered by different providers (mid-conversation
                            failover — a behavior change, not a config detail).
  * ``tool_repeat_loop``    the same tool called with byte-identical
                            arguments >= TOOL_REPEAT_MIN times inside
                            TOOL_REPEAT_WINDOW_S (the send-storm shape; the
                            below-LLM guards stop the blast, this makes the
                            pattern visible wherever it appears next).

Failure posture (the drift-monitor rule): an unreadable source is REPORTED
and that bot's ``sweep_resolve`` is SKIPPED — blindness must never present
as "all clean" by auto-resolving signals it could not re-observe.

Scheduling: invoked from pod_report's daily main() (best-effort, wrapped) —
no new launchd job; also runnable standalone:

    python3 -m turn_autopsy --shared-dir /Users/Shared/evolve [--bot B]
        [--days 1] [--dry-run]

Thresholds are calibration constants from the incident (the storm ran ~16
identical calls in two minutes; TOOL_REPEAT_MIN=5 is well under it and well
above honest retry patterns). Revisit against a month of sweep output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PRODUCER = "turn_autopsy"

TOOL_REPEAT_MIN = 5
TOOL_REPEAT_WINDOW_S = 600
#: Read at most this much of the tail of a gateway err log per sweep.
ERR_LOG_TAIL_BYTES = 2 * 1024 * 1024
#: Scan at most this many session files per bot (newest first).
MAX_SESSION_FILES = 12

_INCOMPLETE_RE = re.compile(
    r"^(?P<ts>\S+)\s+.*incomplete turn detected:.*?"
    r"provider=(?P<provider>\S+)\s+stopReason=(?P<reason>\w+)"
)

#: TA-2 seed — the explain+remediate registry. Plex language: what happened,
#: why the user saw what they saw, what fixes it. The alert IS these words.
CAUSE_REGISTRY: dict[str, dict[str, str]] = {
    "output_cap_hit": {
        "title": "A reply was cut off by the model's output limit",
        "body": (
            "The bot was mid-answer when it hit the maximum reply size "
            "configured for {provider}, so the whole turn was thrown away "
            "and the user saw a generic \"couldn't generate a response\" "
            "message. Fix: raise maxTokens for this model in the bot's "
            "model settings (the model supports far more than the default)."
        ),
    },
    "incomplete_turn": {
        "title": "A reply failed for a reason this sweep doesn't recognize yet",
        "body": (
            "A turn ended incomplete with stopReason={reason} on {provider}. "
            "The user likely saw a generic failure message. This cause isn't "
            "in the autopsy taxonomy yet — the raw details are attached; if "
            "it recurs, it deserves its own class."
        ),
    },
    "provider_swap": {
        "title": "The bot changed AI providers mid-conversation",
        "body": (
            "Consecutive replies in one conversation were answered by "
            "different providers ({shape}). A different provider is a "
            "different personality and different tool discipline — if the "
            "bot suddenly seemed like someone else, this is why. Usually "
            "means the primary model erred and failover crossed vendors; "
            "check the model rung's failover order."
        ),
    },
    "tool_repeat_loop": {
        "title": "The bot repeated the same action over and over",
        "body": (
            "The tool {shape} was called with identical inputs {count} "
            "times in under {window_min} minutes — a retry loop. Rate "
            "guards limit the blast radius for email; if this tool has "
            "external effects, it may need the same below-the-model guard."
        ),
    },
}


@dataclass
class BotAutopsy:
    bot_id: str
    findings: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    sources_read: int = 0
    sources_missing: int = 0
    sources_unreadable: list[str] = field(default_factory=list)

    def add(self, cause: str, shape: str, detail: dict[str, Any]) -> None:
        key = (cause, shape)
        entry = self.findings.setdefault(
            key, {"cause": cause, "shape": shape, "count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append(detail)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _read_tail(path: Path, cap: int) -> str:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - cap))
        return f.read().decode("utf-8", errors="replace")


def scan_err_log(text: str, since: datetime, out: BotAutopsy) -> None:
    for line in text.splitlines():
        m = _INCOMPLETE_RE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group("ts"))
        if ts is None or ts < since:
            continue
        provider = m.group("provider")
        reason = m.group("reason")
        detail = {"ts": m.group("ts"), "provider": provider, "reason": reason}
        if reason == "length":
            out.add("output_cap_hit", provider, detail)
        else:
            out.add("incomplete_turn", f"{provider}|{reason}", detail)


def scan_session_records(records: Iterable[dict[str, Any]], since: datetime,
                         out: BotAutopsy) -> None:
    """One session file's records → provider swaps + tool repeat loops."""
    prev_provider: str | None = None
    calls: dict[str, list[datetime]] = {}
    reported_repeat: set[str] = set()
    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        ts = _parse_ts(str(rec.get("timestamp") or ""))
        in_window = ts is not None and ts >= since
        if msg.get("role") == "assistant":
            provider = msg.get("provider")
            if isinstance(provider, str) and provider:
                # Track continuity across the whole file so a swap right at
                # the window edge is still seen; only REPORT in-window swaps.
                if (prev_provider and provider != prev_provider and in_window):
                    out.add("provider_swap", f"{prev_provider}->{provider}",
                            {"ts": rec.get("timestamp")})
                prev_provider = provider
            if not in_window:
                continue
            for block in msg.get("content") or []:
                if not (isinstance(block, dict) and block.get("type") == "toolCall"):
                    continue
                name = str(block.get("name") or "?")
                digest = hashlib.sha256(json.dumps(
                    block.get("arguments"), sort_keys=True, default=str,
                ).encode()).hexdigest()[:16]
                key = f"{name}|{digest}"
                stamps = calls.setdefault(key, [])
                if ts is not None:
                    stamps.append(ts)
                    horizon = ts - timedelta(seconds=TOOL_REPEAT_WINDOW_S)
                    recent = [s for s in stamps if s >= horizon]
                    calls[key] = recent
                    if len(recent) >= TOOL_REPEAT_MIN and key not in reported_repeat:
                        reported_repeat.add(key)
                        out.add("tool_repeat_loop", name,
                                {"ts": rec.get("timestamp"),
                                 "identical_calls": len(recent),
                                 "args_digest": digest})


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


_SESSION_FILE_RE = re.compile(r"^[0-9a-f-]{36}\.jsonl$")


def collect_for_bot(
    bot_id: str,
    home: Path,
    since: datetime,
) -> BotAutopsy:
    out = BotAutopsy(bot_id=bot_id)
    err_log = home / ".openclaw" / "logs" / "gateway.err.log"
    try:
        scan_err_log(_read_tail(err_log, ERR_LOG_TAIL_BYTES), since, out)
        out.sources_read += 1
    except FileNotFoundError:
        # A bot with no err log yet is genuinely clean, not blind — counted
        # so the report can distinguish "clean" from "never had the file".
        out.sources_missing += 1
    except OSError:
        out.sources_unreadable.append(str(err_log))

    agents_dir = home / ".openclaw" / "agents"
    stamped: list[tuple[float, Path]] = []
    try:
        for agent_dir in agents_dir.iterdir():
            sess = agent_dir / "sessions"
            if not sess.is_dir():
                continue
            for p in sess.iterdir():
                if not _SESSION_FILE_RE.match(p.name):
                    continue
                try:
                    stamped.append((p.stat().st_mtime, p))
                except OSError:
                    out.sources_unreadable.append(str(p))
    except FileNotFoundError:
        out.sources_missing += 1
    except OSError:
        out.sources_unreadable.append(str(agents_dir))
    stamped.sort(reverse=True)
    for mtime, p in stamped[:MAX_SESSION_FILES]:
        if datetime.fromtimestamp(mtime, tz=timezone.utc) < since:
            continue
        try:
            scan_session_records(_iter_jsonl(p), since, out)
            out.sources_read += 1
        except OSError:
            out.sources_unreadable.append(str(p))
    return out


def emit(shared_dir: Path, autopsy: BotAutopsy, *, dry_run: bool = False) -> list[str]:
    """Write one Signal per finding; sweep-resolve cleared ones. Returns the
    kept signatures. A bot with unreadable sources emits its findings but
    SKIPS sweep_resolve — blindness must not auto-clear old signals."""
    from signals import settle_gate
    from signals import store as signals_store

    # Fresh-pod posture (protection_registry: settle): a pod being brought
    # up throws transient turn failures as a matter of course — withhold
    # this producer entirely (observe AND sweep) until the pod settles.
    if not dry_run and settle_gate.should_withhold(
            shared_dir, severity="warn", transient=True):
        return []

    kept: list[str] = []
    for entry in autopsy.findings.values():
        cause, shape = entry["cause"], entry["shape"]
        reg = CAUSE_REGISTRY[cause]
        example = (entry["examples"] or [{}])[0]
        body = reg["body"].format(
            provider=example.get("provider", shape),
            reason=example.get("reason", "?"),
            shape=shape,
            count=entry["count"] if cause != "tool_repeat_loop"
            else example.get("identical_calls", entry["count"]),
            window_min=TOOL_REPEAT_WINDOW_S // 60,
        )
        signature = f"{autopsy.bot_id}|{cause}|{shape}"
        kept.append(signature)
        if dry_run:
            print(f"[dry-run] {signature}: {reg['title']} (x{entry['count']})")
            continue
        signals_store.observe(
            shared_dir,
            producer=PRODUCER,
            type=f"turn_autopsy_{cause}",
            scope="bot",
            bot_id=autopsy.bot_id,
            signature=signature,
            title=reg["title"],
            body=body,
            details={
                "bot_id": autopsy.bot_id,
                "cause": cause,
                "shape": shape,
                "occurrences": entry["count"],
                "examples": entry["examples"],
            },
        )
    if not dry_run and not autopsy.sources_unreadable:
        signals_store.sweep_resolve(
            shared_dir, producer=PRODUCER, kept_signatures=set(kept),
            bot_ids={autopsy.bot_id},
        )
    return kept


def run_sweep(
    shared_dir: Path,
    *,
    bots: Iterable[str] | None = None,
    days: float = 1.0,
    dry_run: bool = False,
    home_resolver: Callable[[str], Path] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sweep every (or one) bot; returns a report dict for callers/logs."""
    from evolve_config import bot_home, load_config  # analyzer top-level

    network = load_config()
    bot_ids = list(bots) if bots else sorted((network.get("bots") or {}).keys())
    resolver = home_resolver or (lambda b: Path(bot_home(b, network)))
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    report: dict[str, Any] = {"bots": {}, "producer": PRODUCER}
    for bot_id in bot_ids:
        try:
            home = resolver(bot_id)
        except Exception:
            report["bots"][bot_id] = {"error": "home unresolved"}
            continue
        autopsy = collect_for_bot(bot_id, home, since)
        kept = emit(shared_dir, autopsy, dry_run=dry_run)
        report["bots"][bot_id] = {
            "findings": len(autopsy.findings),
            "signatures": kept,
            "sources_read": autopsy.sources_read,
            "sources_missing": autopsy.sources_missing,
            "unreadable": autopsy.sources_unreadable,
        }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--shared-dir", required=True)
    ap.add_argument("--bot", default=None)
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    report = run_sweep(
        Path(args.shared_dir),
        bots=[args.bot] if args.bot else None,
        days=args.days,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
