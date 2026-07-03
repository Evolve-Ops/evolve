"""generators.evolve_watchdog.events — Watchdog event JSONL storage.

Phase 1 of the alerts/signal-store consolidation (spec:
docs/spec-alerts-signal-store-2026-05-07.md): every event written here
is *also* emitted as a Signal via :func:`signals.store.observe`. Dual
write keeps the existing JSONL store intact (heal.py / test_runner
still read it for caller-level dedup) while populating the new Signal
store that the Alerts UI consumes.

Mapping table for event_type → (flavor, default-scope) lives in
:data:`_SIGNAL_FLAVOR_BY_EVENT_TYPE` below.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from schema.watchdog import WatchdogEvent, WatchdogEventType


# ─────────────────────────────────────────────────────────────────────────────
# Signal mapping (Phase 1)
# ─────────────────────────────────────────────────────────────────────────────

# Per spec appendix: which lane of the Alerts UI does each event_type
# land in? Activity = unusual, may not need action; Maintenance = needs
# fix. New event_types added here must be classified — leaving an entry
# off the map deliberately raises so we don't silently drop signals.
_SIGNAL_FLAVOR_BY_EVENT_TYPE: dict[WatchdogEventType, str] = {
    "proposal_volume_deviation": "activity",
    "auto_revert_rate_spike": "activity",
    "rejection_rate_spike": "activity",
    "verification_reliability_drop": "activity",
    "generator_dominance": "activity",
    "calibration_drift": "activity",
    "observation_extraction_drift": "activity",
    "meta_layer_cost_spike": "activity",
    "gateway_instability": "maintenance",
    "config_drift_unexplained": "maintenance",
    "test_failure_pattern": "maintenance",
}

# Producer attribution per event_type. All flow through this single
# write path today (events.write_events is the chokepoint), but the
# upstream emitter differs — split surfaces by producer in Phase 5.
_SIGNAL_PRODUCER_BY_EVENT_TYPE: dict[WatchdogEventType, str] = {
    "proposal_volume_deviation": "evolve_watchdog",
    "auto_revert_rate_spike": "evolve_watchdog",
    "rejection_rate_spike": "evolve_watchdog",
    "verification_reliability_drop": "evolve_watchdog",
    "generator_dominance": "evolve_watchdog",
    "calibration_drift": "evolve_watchdog",
    "observation_extraction_drift": "evolve_watchdog",
    "meta_layer_cost_spike": "evolve_watchdog",
    "gateway_instability": "sysadmin_watchdog",
    "config_drift_unexplained": "sysadmin_watchdog",
    "test_failure_pattern": "test_runner",
}


def _human_title(event_type: str, bot_id: str | None) -> str:
    """One-line UI title for a signal derived from a watchdog event."""
    label = event_type.replace("_", " ").capitalize()
    if bot_id:
        return f"{label} on {bot_id}"
    return label


def _emit_as_signal(event: WatchdogEvent, shared_dir: Path) -> None:
    """Translate a WatchdogEvent into a Signal via signals.store.observe.

    Best-effort: if the signals layer can't be reached or the event_type
    isn't classified, log nothing and return — the JSONL write that
    already happened is the durable record. We never fail the
    upstream emitter on Signal-side problems during the Phase 1
    rollout.
    """
    flavor = _SIGNAL_FLAVOR_BY_EVENT_TYPE.get(event.event_type)
    if flavor is None:
        return
    producer = _SIGNAL_PRODUCER_BY_EVENT_TYPE.get(event.event_type, "watchdog")

    try:
        # Local import: events.py is loaded by the analyzer daemon and
        # by tests via several entry points; keeping this lazy avoids
        # forcing a signals import at module load.
        from signals import store as signals_store
        from schema.signal import make_signature
    except ImportError:
        return

    scope_key = event.bot_id or "pod"
    signature = make_signature(producer, event.event_type, scope_key)
    scope = "bot" if event.bot_id else "pod"

    try:
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=producer,
            type=event.event_type,
            flavor=flavor,
            severity=event.severity,
            scope=scope,
            bot_id=event.bot_id,
            title=_human_title(event.event_type, event.bot_id),
            details=dict(event.details or {}),
        )
    except Exception:
        # Phase 1 best-effort — never break the JSONL emitter.
        return


def _emit_to_observability(event: WatchdogEvent, shared_dir: Path) -> None:
    """Record a watchdog event as an observability span (v1.5-1).

    Dual-emit alongside the JSONL store and the Signal store. The span
    carries the same information the Signal does, but flows into Opik
    so downstream consumers (cost rollups, trace search, future
    cross-bot timelines) can query it. Best-effort — never raises.
    """
    try:
        from datetime import datetime, timezone
        from observability import get_client
    except ImportError:
        return

    try:
        client = get_client({}, shared_dir=shared_dir)
    except Exception:
        return

    flavor = _SIGNAL_FLAVOR_BY_EVENT_TYPE.get(event.event_type) or "maintenance"
    producer = _SIGNAL_PRODUCER_BY_EVENT_TYPE.get(event.event_type, "watchdog")

    # Parse the event timestamp into UTC datetimes; tolerate missing.
    when = datetime.now(timezone.utc)
    raw_ts = getattr(event, "timestamp", "") or ""
    if raw_ts:
        try:
            candidate = raw_ts[:-1] + "+00:00" if raw_ts.endswith("Z") else raw_ts
            parsed = datetime.fromisoformat(candidate)
            when = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    try:
        client.record_event(
            name=f"watchdog.{event.event_type}",
            producer=producer,
            bot_id=event.bot_id,
            start_time=when,
            end_time=when,
            tags=[flavor, producer, event.severity],
            attributes={
                "event_type": event.event_type,
                "severity": event.severity,
                "flavor": flavor,
                "details": dict(event.details or {}),
            },
            error_info=(
                {"type": event.event_type}
                if event.severity == "alert" else None
            ),
        )
    except Exception:
        return


def signal_id_for_event(event: WatchdogEvent, shared_dir: Path) -> str | None:
    """Return the Signal id mirroring this WatchdogEvent, if any.

    Used by the proposal builders to populate
    :attr:`schema.proposal.Proposal.motivating_signals`. Looks up by
    the same signature the dual-write would have produced.

    Returns ``None`` if the event_type is unmapped, the signals layer
    isn't importable, or no active Signal exists with the expected
    signature.
    """
    flavor = _SIGNAL_FLAVOR_BY_EVENT_TYPE.get(event.event_type)
    if flavor is None:
        return None
    producer = _SIGNAL_PRODUCER_BY_EVENT_TYPE.get(event.event_type, "watchdog")
    try:
        from signals import store as signals_store
        from schema.signal import make_signature
    except ImportError:
        return None
    scope_key = event.bot_id or "pod"
    signature = make_signature(producer, event.event_type, scope_key)
    try:
        found = signals_store.find_active_by_signature(shared_dir, signature)
    except Exception:
        return None
    return found.id if found else None


def _daily_path(shared_dir: Path, day_iso: str) -> Path:
    return shared_dir / "watchdog" / f"{day_iso}.jsonl"


def write_events(
    events: Iterable[WatchdogEvent],
    *,
    shared_dir: Path,
    day: datetime | None = None,
) -> Path | None:
    """Append events to today's JSONL. Returns path, or None if no events."""
    events_list = list(events)
    if not events_list:
        return None
    if day is None:
        day = datetime.now(timezone.utc)
    day_iso = day.astimezone(timezone.utc).date().isoformat()
    path = _daily_path(shared_dir, day_iso)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing
    existing: list[str] = []
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing = []

    new_lines = [json.dumps(e.to_dict()) for e in events_list]
    combined = "\n".join(existing + new_lines) + "\n"

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(combined)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # Phase 1: dual-write to the Signal store. Best-effort — the JSONL
    # is the durable record and existing readers (heal, test_runner) keep
    # working off it.
    for event in events_list:
        _emit_as_signal(event, shared_dir)

    # v1.5-1: triple-write to observability (Opik / JSONL fallback).
    # Same best-effort posture — the JSONL store and the Signal store
    # remain authoritative; observability is additive.
    for event in events_list:
        _emit_to_observability(event, shared_dir)

    return path


def read_events_range(
    shared_dir: Path, start: datetime, end: datetime
) -> Iterator[WatchdogEvent]:
    """Stream WatchdogEvents in [start, end]."""
    start_utc = start.astimezone(timezone.utc) if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end_utc = end.astimezone(timezone.utc) if end.tzinfo else end.replace(tzinfo=timezone.utc)
    cur = start_utc.date()
    end_date = end_utc.date()
    while cur <= end_date:
        path = _daily_path(shared_dir, cur.isoformat())
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    event = WatchdogEvent.from_dict(data)
                except (KeyError, ValueError, TypeError):
                    continue
                # Best-effort timestamp filter
                ts_raw = event.timestamp
                ts = _parse_iso(ts_raw)
                if ts is not None and (ts < start_utc or ts > end_utc):
                    continue
                yield event
        cur = cur + timedelta(days=1)


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class WatchdogEventStore:
    """Thin wrapper for reading + writing events during observe()."""

    shared_dir: Path
    day: datetime | None = None  # clock override for tests
    _buffer: list[WatchdogEvent] = field(default_factory=list)

    def record(self, event: WatchdogEvent) -> None:
        self._buffer.append(event)

    def flush(self) -> None:
        if self._buffer:
            write_events(
                self._buffer, shared_dir=self.shared_dir, day=self.day
            )
            self._buffer.clear()
