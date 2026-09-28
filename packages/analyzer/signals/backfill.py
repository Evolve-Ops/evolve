"""signals.backfill — Replay historical watchdog JSONL into the Signal store.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 1).

The watchdog has been writing JSONL events to ``{shared_dir}/watchdog/``
since L6 shipped. Phase 1 dual-writes new events to the Signal store but
leaves the historical backlog dark — the Alerts page History tab would
look empty until enough new events accumulated.

This module reads the JSONL backlog and creates *resolved* Signals (one
per unique signature) so the History tab has context from day one. New
firing signals come through the live dual-write path; backfill never
revives a condition that's already cleared.

Idempotent: signatures that already have a Signal in any state are
skipped on subsequent runs.

CLI:

    python3 -m signals.backfill --shared-dir /Users/Shared/evolve

(or invoke ``backfill_watchdog_events`` directly from a one-shot
admin task — there's no daemon for this.)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from schema.signal import (
    Delivery,  # noqa: F401  (kept for future hook)
    Signal,
    StateTransition,
    new_signal_id,
)
from signals import store as signals_store


# Local copies of the maps to avoid creating a circular import on
# evolve_watchdog. These must stay in sync with
# generators/evolve_watchdog/events.py — kept under the same spec.
_FLAVOR_BY_EVENT_TYPE: dict[str, str] = {
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
    # test_failure_pattern: retired 2026-06-08 — kept here so historical
    # JSONL events still backfill cleanly. No new events fire (see
    # internal/decision-app-tests-2026-06-08.md).
    "test_failure_pattern": "maintenance",
}

_PRODUCER_BY_EVENT_TYPE: dict[str, str] = {
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


@dataclass
class BackfillResult:
    scanned: int          # JSONL events read
    created: int          # new Signals created
    skipped_existing: int  # signatures that already had a Signal in the right state
    skipped_unmapped: int  # event_types not in the flavor map
    reopened: int = 0     # archived signals re-opened because the latest
                          # observation is within the firing window. Self-
                          # corrects from earlier backfills that always
                          # marked everything resolved.


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _iter_jsonl(shared_dir: Path) -> Iterator[dict]:
    """Yield raw event dicts from every {shared_dir}/watchdog/*.jsonl file."""
    root = shared_dir / "watchdog"
    if not root.exists():
        return
    for path in sorted(root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _signature_for(event_type: str, bot_id: str | None) -> str | None:
    """Compute the canonical Signal signature for a watchdog event."""
    producer = _PRODUCER_BY_EVENT_TYPE.get(event_type)
    if producer is None:
        return None
    scope_key = bot_id or "pod"
    return f"{producer}:{event_type}:{scope_key}"


def _existing_signal_for_signature(shared_dir: Path, signature: str):
    """Return the existing Signal record (active or archived) for this
    signature, or ``None``. Used by backfill to decide whether the
    signature is already represented and what state it's in.
    """
    found = signals_store.find_active_by_signature(shared_dir, signature)
    if found is not None:
        return found
    archived_dir = signals_store.signals_root(shared_dir) / "archived"  # store-access-lint: store-internal backfill (per-path load_signal_file scan by signature)
    if not archived_dir.exists():
        return None
    for path in archived_dir.glob("*.json"):
        sig = signals_store.load_signal_file(path)
        if sig is not None and sig.signature == signature:
            return sig
    return None


def _signature_already_present(shared_dir: Path, signature: str) -> bool:
    """True if any Signal (active or archived) has this signature."""
    return _existing_signal_for_signature(shared_dir, signature) is not None


def backfill_watchdog_events(
    shared_dir: Path,
    *,
    since: datetime | None = None,
    firing_window_hours: float = 25.0,
    now: datetime | None = None,
) -> BackfillResult:
    """Read historical watchdog JSONL and create Signals.

    For each unique signature, creates ONE Signal using the most recent
    event's payload. State assignment:

      - signature whose latest event is within ``firing_window_hours``
        of ``now`` → state=firing (the condition was still firing on
        the most recent watchdog cycle, so it should be visible in the
        Activity / Maintenance lanes)
      - older signatures → state=resolved (the condition cleared at
        some point; show in History tab as historical context)

    Default ``firing_window_hours=25`` covers the daily watchdog cadence
    plus clock drift. Older events for the same signature contribute to
    ``observation_count`` but don't create separate Signals.

    Args:
        shared_dir: pod root (where ``watchdog/`` lives).
        since: only ingest events newer than this timestamp. ``None``
            means "all available history."
        firing_window_hours: signatures whose latest observation falls
            inside this window are marked firing rather than resolved.
        now: clock override for tests; defaults to ``datetime.now(UTC)``.

    Returns a :class:`BackfillResult` with counts. Idempotent:
    signatures that already have a Signal (in any state) are skipped.
    """
    scanned = 0
    skipped_unmapped = 0

    # Group events by signature; keep the LATEST event per signature
    # plus a count for observation_count.
    by_signature: dict[str, dict] = {}

    for raw in _iter_jsonl(shared_dir):
        scanned += 1
        event_type = raw.get("event_type") or ""
        if event_type not in _FLAVOR_BY_EVENT_TYPE:
            skipped_unmapped += 1
            continue
        bot_id = raw.get("bot_id")
        signature = _signature_for(event_type, bot_id)
        if signature is None:
            skipped_unmapped += 1
            continue
        ts = _parse_iso(raw.get("timestamp") or "")
        if since is not None and ts is not None and ts < since:
            continue

        slot = by_signature.get(signature)
        if slot is None:
            by_signature[signature] = {
                "event": raw,
                "ts": ts,
                "count": 1,
                "first_ts": ts,
            }
        else:
            slot["count"] += 1
            if ts is not None and (slot["ts"] is None or ts > slot["ts"]):
                slot["ts"] = ts
                slot["event"] = raw
            if ts is not None and (
                slot["first_ts"] is None or ts < slot["first_ts"]
            ):
                slot["first_ts"] = ts

    created = 0
    skipped_existing = 0
    reopened = 0
    cutoff_time = (now or datetime.now(timezone.utc)) - timedelta(
        hours=firing_window_hours
    )

    for signature, slot in by_signature.items():
        existing = _existing_signal_for_signature(shared_dir, signature)
        if existing is not None:
            # Self-correct: a prior backfill (before the firing-window
            # logic existed) may have archived a signature whose latest
            # observation is actually still inside the firing window.
            # Re-open it so it shows up in Activity / Maintenance.
            latest_ts = slot["ts"]
            should_be_firing = (
                firing_window_hours > 0
                and latest_ts is not None
                and latest_ts >= cutoff_time
            )
            if (
                existing.state == "resolved"
                and should_be_firing
            ):
                try:
                    signals_store.apply_transition(
                        existing,
                        "firing",
                        shared_dir,
                        actor="backfill",
                        reason=(
                            "re-open mis-archived signal: latest "
                            "observation within firing window"
                        ),
                    )
                    reopened += 1
                    continue
                except Exception:
                    pass
            skipped_existing += 1
            continue

        raw = slot["event"]
        event_type = raw["event_type"]
        bot_id = raw.get("bot_id")
        producer = _PRODUCER_BY_EVENT_TYPE[event_type]
        flavor = _FLAVOR_BY_EVENT_TYPE[event_type]
        ts_iso = (slot["ts"] or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"
        )
        first_ts_iso = (slot["first_ts"] or slot["ts"] or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"
        )

        # Whether to mark this signature firing (recently-observed) or
        # resolved (historical). The "firing window" is the cadence of
        # the producing daemon plus a buffer.
        latest_ts = slot["ts"]
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            hours=firing_window_hours
        )
        is_recently_firing = latest_ts is not None and latest_ts >= cutoff

        if is_recently_firing:
            state = "firing"
            resolved_at = None
            history = [
                StateTransition(
                    from_state=None,
                    to_state="firing",
                    at=first_ts_iso,
                    actor="backfill",
                    reason=(
                        "historical replay from watchdog JSONL "
                        "(latest observation within firing window)"
                    ),
                ),
            ]
        else:
            state = "resolved"
            resolved_at = ts_iso
            history = [
                StateTransition(
                    from_state=None,
                    to_state="firing",
                    at=first_ts_iso,
                    actor="backfill",
                    reason="historical replay from watchdog JSONL",
                ),
                StateTransition(
                    from_state="firing",
                    to_state="resolved",
                    at=ts_iso,
                    actor="backfill",
                    reason="condition cleared (historical, no live observation)",
                ),
            ]

        sig = Signal(
            id=new_signal_id(),
            signature=signature,
            producer=producer,
            type=event_type,
            flavor=flavor,  # type: ignore[arg-type]
            severity=raw.get("severity", "info"),
            scope="bot" if bot_id else "pod",
            bot_id=bot_id,
            title=_human_title(event_type, bot_id),
            details=dict(raw.get("details") or {}),
            state=state,  # type: ignore[arg-type]
            created_at=first_ts_iso,
            last_observed_at=ts_iso,
            observation_count=slot["count"],
            resolved_at=resolved_at,
            state_history=history,
        )
        signals_store.write_signal(sig, shared_dir)
        created += 1

    return BackfillResult(
        scanned=scanned,
        created=created,
        skipped_existing=skipped_existing,
        skipped_unmapped=skipped_unmapped,
        reopened=reopened,
    )


def _human_title(event_type: str, bot_id: str | None) -> str:
    label = event_type.replace("_", " ").capitalize()
    if bot_id:
        return f"{label} on {bot_id}"
    return label


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Replay watchdog JSONL into the Signal store as resolved signals."
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Only ingest events from the last N days (default: all history)",
    )
    parser.add_argument(
        "--firing-window-hours",
        type=float,
        default=25.0,
        help=(
            "Signatures whose latest observation is within this window land "
            "as state=firing (still active); older signatures resolve. "
            "Default 25h covers a daily watchdog cycle plus drift. Set to 0 "
            "to archive everything as historical."
        ),
    )
    args = parser.parse_args(argv)

    since = None
    if args.since_days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=args.since_days)

    result = backfill_watchdog_events(
        args.shared_dir,
        since=since,
        firing_window_hours=args.firing_window_hours,
    )
    print(
        f"backfill: scanned={result.scanned} created={result.created} "
        f"reopened={result.reopened} "
        f"skipped_existing={result.skipped_existing} "
        f"skipped_unmapped={result.skipped_unmapped}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
