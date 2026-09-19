"""
forge_sessions.py — Annotations linking forge dispatches to OC sessions.

Forge dispatches the bot's own LLM via ``sudo -H -u <bot> openclaw agent
--local --agent main --message "You have a forge build job…"``. From OC's
perspective that's a regular user prompt to the main agent — the turn lands
in ``turns-<date>.jsonl`` with ``source: "user"`` and ``channel: "unknown"``,
which then renders on the Usage tab as Human (channel:unknown). Operators
see $1–$5 of "human chat" on a bot they never talked to.

The fix is structural: the dispatcher (in ``bot_forge._dispatch_agent``)
writes a small annotation per dispatch capturing a conservative time
window for the OC session. Downstream consumers (cost_event_converter
and usage_analytics) match each turn against the open windows for that
bot+date and, when a turn falls inside a window with channel=unknown and
source ∈ {user, human}, retag ``source="forge"`` so it leaves the Human
bucket. The Usage tab's by_source rollup then surfaces a distinct
"forge" row, and cost_event records get ``trigger_kind: "forge"``.

Annotation layout:

    {shared_dir}/forge_sessions/{bot_id}/{YYYY-MM-DD}.jsonl

One JSON line per dispatch. ``date`` is the UTC date of ``start_ts``.
Each line records the *conservative* window — start_ts is the wall-clock
time the dispatcher spawned the openclaw subprocess; end_ts is
``start_ts + timeout_sec + buffer``. The actual session is shorter, but
matching on a superset is safe: only forge sessions write channel=unknown
turns at this point (other autonomous paths carry source=heartbeat or
source=subagent).

The directory tree is evolve-owned; files are written mode 0o644 so the
cost_event_converter (which runs as the bot user) can read them. Putting
this in its own subtree avoids ownership conflicts with the existing
``{shared_dir}/annotations/{bot}/`` dir, which is bot-owned for the
per-bot cost_event_converter writes.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


FORGE_ANNOTATION_SCHEMA_VERSION = 2  # v2 (2026-06-03) adds optional trigger_subkind

# Channel values written by OC for direct ``--local --agent main`` sessions
# (which is how every forge dispatch invokes the agent). When the turn-
# collector writes a turn record it carries this channel value unchanged.
_LOCAL_CHANNEL_VALUES = ("unknown", "", None)

# Source values that mean "human user prompt" before retag — these are
# the only source values eligible for forge retagging. Other autonomous
# paths (heartbeat, cron, subagent) carry distinct source values that we
# leave alone even if they happen to overlap a forge window.
_RETAGGABLE_SOURCE_VALUES = ("user", "human")


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────


def forge_sessions_path(shared_dir: Path, bot_id: str, target_date: date) -> Path:
    """Annotation file path for one (bot, UTC-date)."""
    return (
        Path(shared_dir) / "forge_sessions" / bot_id
        / f"{target_date.isoformat()}.jsonl"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Window model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ForgeWindow:
    """Time window for one forge dispatch — used to retag turns."""

    start: datetime
    end: datetime
    job_id: str
    kind: str            # "build" | "critique" | "refine"
    suffix: str          # "" for build, "-c1" for critique r1, "-r1" for refine r1
    trigger_subkind: str | None = None  # e.g. "operator_confirmed_install" when
                                        # the dispatch was opcoded as operator-
                                        # confirmed (so daily_cap_usd can exempt it
                                        # while still capping background runaway)


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 string into an aware UTC datetime, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    candidate = ts
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Writer (called from bot_forge at dispatch time)
# ─────────────────────────────────────────────────────────────────────────────


def write_dispatch_annotation(
    shared_dir: Path,
    bot_id: str,
    job_id: str,
    suffix: str,
    kind: str,
    start_ts: datetime,
    timeout_sec: int,
    buffer_sec: int = 60,
    *,
    trigger_subkind: str | None = None,
) -> Path:
    """Append one forge-dispatch annotation. Returns the file written to.

    ``start_ts`` should be the moment the dispatcher called Popen on the
    openclaw subprocess; ``timeout_sec`` is the ``--timeout`` value handed
    to that subprocess. The on-disk ``end_ts`` is ``start_ts + timeout_sec
    + buffer_sec`` — a conservative superset of the real session, since
    matching wider than reality is safe (other autonomous paths don't
    write channel=unknown turns).

    ``trigger_subkind`` is an optional tag propagated onto every retagged
    turn (and through to cost_event records). The current consumer is
    spend_alert's daily-cap exemption for ``operator_confirmed_install`` —
    operator-confirmed installs shouldn't trip a tight daily cap because
    the operator already saw the projected cost.

    Idempotency: callers may invoke this multiple times for the same
    (job_id, suffix); duplicate lines are harmless because the consumer's
    matcher checks "any window contains this ts" — duplicate windows are
    silently absorbed.
    """
    start_utc = start_ts.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(seconds=int(timeout_sec) + int(buffer_sec))

    record: dict = {
        "schema_version": FORGE_ANNOTATION_SCHEMA_VERSION,
        "job_id":         job_id,
        "suffix":         suffix or "",
        "kind":           kind,
        "bot_id":         bot_id,
        "start_ts":       _iso_z(start_utc),
        "end_ts":         _iso_z(end_utc),
    }
    if trigger_subkind:
        record["trigger_subkind"] = trigger_subkind

    out = forge_sessions_path(shared_dir, bot_id, start_utc.date())
    out.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record, separators=(",", ":")) + "\n"
    _atomic_append_text(out, line)
    return out


def _iso_z(dt: datetime) -> str:
    """ISO-8601 UTC with 'Z' suffix (matches the existing cost_event format)."""
    s = dt.astimezone(timezone.utc).isoformat()
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    return s


def _atomic_append_text(path: Path, text: str) -> None:
    """Append `text` to `path` via tempfile + concat + rename.

    Plain mode-'a' open is line-atomic on POSIX, but a concurrent writer
    could race. The cost_event_converter uses the same pattern for the
    sibling cost_events file. mode 0o644 so the bot user (running the
    converter) can read what evolve writes here.
    """
    existing = b""
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError:
            existing = b""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".forge-sessions-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(existing)
            f.write(text.encode("utf-8"))
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Reader (consumed by cost_event_converter and usage_analytics)
# ─────────────────────────────────────────────────────────────────────────────


def load_windows(
    shared_dir: Path,
    bot_id: str,
    target_date: date,
    *,
    include_prev_day: bool = True,
) -> list[ForgeWindow]:
    """Read forge-dispatch annotations for one (bot, UTC-date).

    When ``include_prev_day`` is True (default), the previous day's file
    is also read so dispatches that cross UTC midnight are matched.
    A dispatch starting at 23:50 with a 20-minute timeout has windows
    that extend into the next day; the next day's turns need to see
    that window even though the annotation lives in yesterday's file.
    """
    windows: list[ForgeWindow] = []
    dates: list[date] = [target_date]
    if include_prev_day:
        dates.append(target_date - timedelta(days=1))
    for d in dates:
        path = forge_sessions_path(shared_dir, bot_id, d)
        windows.extend(_read_windows(path))
    return windows


def _read_windows(path: Path) -> Iterable[ForgeWindow]:
    try:
        text = path.read_text(encoding="utf-8")
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
        if not isinstance(rec, dict):
            continue
        start = _parse_iso(rec.get("start_ts"))
        end = _parse_iso(rec.get("end_ts"))
        if start is None or end is None:
            continue
        subkind = rec.get("trigger_subkind")
        yield ForgeWindow(
            start=start,
            end=end,
            job_id=str(rec.get("job_id") or ""),
            kind=str(rec.get("kind") or ""),
            suffix=str(rec.get("suffix") or ""),
            trigger_subkind=str(subkind) if isinstance(subkind, str) and subkind else None,
        )


def is_forge_turn(
    windows: list[ForgeWindow],
    ts_iso: str | None,
    channel: str | None,
    source: str | None,
) -> bool:
    """True if a turn record should be retagged as a forge dispatch.

    Requires: turn's channel is the local-agent default (unknown / empty /
    null), turn's source is in the retaggable set (user / human — the
    pre-retag forms that real human input also takes), and the turn's
    end-time falls within any open forge window.

    The channel + source filter is important: heartbeat / cron / subagent
    paths carry distinct values that we must not overwrite even if their
    timestamp happens to overlap a forge window.
    """
    if not windows:
        return False
    if channel not in _LOCAL_CHANNEL_VALUES:
        return False
    if (source or "").lower() not in _RETAGGABLE_SOURCE_VALUES:
        return False
    ts = _parse_iso(ts_iso)
    if ts is None:
        return False
    for w in windows:
        if w.start <= ts <= w.end:
            return True
    return False


def _matching_window(
    windows: list[ForgeWindow], ts_iso: str | None
) -> ForgeWindow | None:
    """Return the first forge window containing ``ts_iso``, or None."""
    ts = _parse_iso(ts_iso)
    if ts is None:
        return None
    for w in windows:
        if w.start <= ts <= w.end:
            return w
    return None


def retag_turn_source(turn: dict, windows: list[ForgeWindow]) -> dict:
    """Return ``turn`` with ``source`` rewritten to ``"forge"`` if it
    matches a window; otherwise return the input unchanged.

    When the matched window carries a ``trigger_subkind``, the turn's
    ``forge_subkind`` field is set to that value — the downstream
    cost_event_converter copies it into the emitted cost_event record,
    where spend_alert reads it to honour the daily-cap exemption.

    Does not copy when no retag is needed (avoids per-turn dict copy in
    the hot path of usage_analytics, which can chew through 50k+ rows
    on multi-bot week views).
    """
    if not is_forge_turn(
        windows, turn.get("ts"), turn.get("channel"), turn.get("source")
    ):
        return turn
    out = dict(turn)
    out["source"] = "forge"
    matched = _matching_window(windows, turn.get("ts"))
    if matched is not None and matched.trigger_subkind:
        out["forge_subkind"] = matched.trigger_subkind
    return out
