"""autonomy.actions_ledger — read the bot-side outward-action ledger.

Spec: docs/spec-autonomy-ladder-2026-06-10.md §1.3 (rung-3 counters),
§3.2 (streak evidence), §8 OQ-3 (decided in Phase B: bot-side counters).

The OQ-3 decision, recorded: counters are **bot-side**. The evolve
OpenClaw plugin (``packages/plugin/src/observer/OutwardActionLedger.ts``)
observes each completed turn's ``tool_use`` blocks — the same agent_end
payload the struggle detector already parses — and appends one record
per MCP tool call to an append-only JSONL ledger:

    {shared_dir}/{bot_id}/outward-actions/actions-YYYY-MM-DD.jsonl

(the CascadeTelemetry per-bot path convention; bot user owns the files).
Record shape, one JSON object per line::

    {"ts": "...", "integration_id": "google_workspace",
     "tool_name": "send_gmail_message", "result": "ok"|"error"|"unknown",
     "session_id": "...", "turn_id": "..."}

Only tool NAMES and ids are captured — never arguments, recipients, or
content (per principle-per-bot-inference: Evolve aggregates structured
outputs; the observation runs inside the bot's own gateway process).
Evolve-side derivation was the rejected OQ-3 option: no existing
evolve-side telemetry records per-tool calls, so deriving counts there
would have meant transcript reading.

This module is the evolve-side reader: it classifies raw tool names
into kind verbs via ``autonomy.catalog`` (verb logic stays in one
place — the TS writer records everything ``mcp__``-shaped and never
classifies) and answers the two questions Phase B needs:

  - per-(integration, day) outward-action counts (rung-3 caps),
  - recent outward-action records (streaks + the demotion reflex).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import catalog as _catalog


LEDGER_DIRNAME = "outward-actions"
LEDGER_FILE_PREFIX = "actions-"
# Keep raw ledger files for 90 days — comfortably past the 30-day
# streak window; same horizon as the signal archive.
LEDGER_RETENTION_DAYS = 90


@dataclass(frozen=True)
class OutwardAction:
    """One outward-classified MCP tool call from the ledger."""

    ts: str                 # ISO8601 from the record (plugin-written)
    integration_id: str
    tool_name: str          # bare tool name (mcp__<id>__ prefix stripped)
    verb: str               # kind verb (send / forward / delete / ...)
    result: str             # "ok" | "error" | "unknown"
    session_id: str = ""
    turn_id: str = ""

    @property
    def day(self) -> str:
        return self.ts[:10]

    def parsed_ts(self) -> datetime | None:
        try:
            dt = datetime.fromisoformat(self.ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt


def ledger_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / bot_id / LEDGER_DIRNAME


def _ledger_files(shared_dir: Path, bot_id: str, since_day: str) -> list[Path]:
    """Ledger files for ``since_day``..today, oldest first. Filenames are
    date-stamped so the window prune is a name comparison, no stat."""
    root = ledger_dir(shared_dir, bot_id)
    try:
        names = sorted(p.name for p in root.glob(f"{LEDGER_FILE_PREFIX}*.jsonl"))
    except OSError:
        return []
    cutoff = f"{LEDGER_FILE_PREFIX}{since_day}.jsonl"
    return [root / n for n in names if n >= cutoff]


def _iter_raw_records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def read_outward_actions(
    shared_dir: Path,
    bot_id: str,
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> list[OutwardAction]:
    """Outward-classified actions for one bot over the last N days.

    Classification happens here, not in the writer: the plugin records
    every ``mcp__*`` call it sees; only calls on a ladder-bound
    integration whose tool name classifies into one of the kind's
    ``outward_verbs`` come back from this reader. Unknown integrations
    and read-tier tools are skipped silently — they are not outward
    actions, and surfacing them anywhere would overstate what we know.
    """
    now = now or datetime.now(timezone.utc)
    since_day = (now - timedelta(days=window_days)).date().isoformat()
    out: list[OutwardAction] = []
    for path in _ledger_files(shared_dir, bot_id, since_day):
        for rec in _iter_raw_records(path):
            iid = rec.get("integration_id")
            tool = rec.get("tool_name")
            ts = rec.get("ts")
            if not (isinstance(iid, str) and isinstance(tool, str)
                    and isinstance(ts, str) and ts[:10] >= since_day):
                continue
            binding = _catalog.binding_for(iid)
            if binding is None:
                continue
            spec = _catalog.kind_spec(binding.kind)
            if spec is None:
                continue
            if not _catalog.kind_tools(binding, [tool]):
                continue
            verb = _catalog.classify_tool(spec, tool)
            if verb not in spec.outward_verbs:
                continue
            result = rec.get("result")
            out.append(OutwardAction(
                ts=ts,
                integration_id=iid,
                tool_name=tool,
                verb=verb,
                result=result if result in ("ok", "error") else "unknown",
                session_id=str(rec.get("session_id") or ""),
                turn_id=str(rec.get("turn_id") or ""),
            ))
    out.sort(key=lambda a: a.ts)
    return out


def count_for_day(
    actions: list[OutwardAction],
    integration_id: str,
    day: str,
    *,
    include_errors: bool = False,
) -> int:
    """Outward-action count for one (integration, day).

    ``include_errors=False`` counts performed actions only (an errored
    tool call did not act outward) — the rung-3 cap semantics.
    ``include_errors=True`` counts attempts — the demotion reflex's
    probing-the-cage semantics.
    """
    return sum(
        1 for a in actions
        if a.integration_id == integration_id and a.day == day
        and (include_errors or a.result != "error")
    )


def prune(shared_dir: Path, bot_id: str, *, now: datetime | None = None) -> int:
    """Delete ledger files older than the retention window. Returns the
    number removed. Safe to call from any sweep; the files are derived
    telemetry, never the source of truth for a posture."""
    now = now or datetime.now(timezone.utc)
    cutoff = f"{LEDGER_FILE_PREFIX}{(now - timedelta(days=LEDGER_RETENTION_DAYS)).date().isoformat()}.jsonl"
    removed = 0
    root = ledger_dir(shared_dir, bot_id)
    try:
        paths = list(root.glob(f"{LEDGER_FILE_PREFIX}*.jsonl"))
    except OSError:
        return 0
    for p in paths:
        if p.name < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def utc_today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).date().isoformat()


__all__ = [
    "LEDGER_DIRNAME",
    "LEDGER_RETENTION_DAYS",
    "OutwardAction",
    "count_for_day",
    "ledger_dir",
    "prune",
    "read_outward_actions",
    "utc_today",
]
