"""tests/test_signals_backfill.py — Phase 1 backfill of historical events."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from signals import backfill, store as signals_store  # noqa: E402


def _write_jsonl(shared_dir: Path, day: str, events: list[dict]) -> None:
    """Write events to a daily JSONL the same shape watchdog uses."""
    p = shared_dir / "watchdog" / f"{day}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _ev(
    *,
    event_type: str,
    severity: str = "warn",
    bot_id: str | None = None,
    timestamp: str = "2026-04-15T12:00:00+00:00",
    details: dict | None = None,
) -> dict:
    return {
        "id": f"evt-{event_type}-{timestamp[:10]}",
        "bot_id": bot_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "severity": severity,
        "details": details or {},
    }


def test_backfill_creates_resolved_signals(tmp_path):
    _write_jsonl(tmp_path, "2026-04-15", [
        _ev(event_type="proposal_volume_deviation",
            timestamp="2026-04-15T08:00:00+00:00",
            details={"ratio": 4.0}),
        _ev(event_type="gateway_instability",
            bot_id="admin_bot",
            severity="alert",
            timestamp="2026-04-15T09:00:00+00:00",
            details={"flap_count": 5}),
    ])

    result = backfill.backfill_watchdog_events(tmp_path)
    assert result.scanned == 2
    assert result.created == 2
    assert result.skipped_existing == 0

    # Both signals are in archived/ (state=resolved)
    archived = list((tmp_path / "signals" / "archived").glob("*.json"))
    assert len(archived) == 2

    # No active signals
    assert list(signals_store.iter_active(tmp_path)) == []


def test_backfill_dedups_same_signature_into_one_signal(tmp_path):
    """Three observations of the same condition over different days
    collapse to one resolved signal with observation_count=3."""
    _write_jsonl(tmp_path, "2026-04-13", [
        _ev(event_type="calibration_drift",
            timestamp="2026-04-13T12:00:00+00:00",
            details={"drift": 0.31}),
    ])
    _write_jsonl(tmp_path, "2026-04-14", [
        _ev(event_type="calibration_drift",
            timestamp="2026-04-14T12:00:00+00:00",
            details={"drift": 0.42}),
    ])
    _write_jsonl(tmp_path, "2026-04-15", [
        _ev(event_type="calibration_drift",
            timestamp="2026-04-15T12:00:00+00:00",
            severity="alert",
            details={"drift": 0.55}),
    ])

    result = backfill.backfill_watchdog_events(tmp_path)
    assert result.scanned == 3
    assert result.created == 1

    archived = list((tmp_path / "signals" / "archived").glob("*.json"))
    assert len(archived) == 1
    sig = signals_store.load_signal_file(archived[0])
    assert sig is not None
    assert sig.observation_count == 3
    # Latest details + severity win
    assert sig.severity == "alert"
    assert sig.details["drift"] == 0.55
    # Created_at = earliest, resolved_at = latest
    assert sig.created_at.startswith("2026-04-13")
    assert sig.resolved_at and sig.resolved_at.startswith("2026-04-15")


def test_backfill_keeps_recently_firing_events_as_firing(tmp_path):
    """Events from the latest watchdog cycle (within firing_window_hours)
    should land as ``firing`` so they show in Activity / Maintenance
    lanes — not silently archived as ``resolved``."""
    now = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=4)).isoformat(timespec="seconds")
    older = (now - timedelta(days=5)).isoformat(timespec="seconds")

    _write_jsonl(tmp_path, "2026-05-02", [
        _ev(event_type="calibration_drift",
            timestamp=older,
            details={"drift": 0.31}),
    ])
    _write_jsonl(tmp_path, "2026-05-07", [
        _ev(event_type="gateway_instability",
            bot_id="admin_bot",
            severity="alert",
            timestamp=recent,
            details={"flap_count": 7}),
    ])

    result = backfill.backfill_watchdog_events(tmp_path, now=now)
    assert result.created == 2

    actives = list(signals_store.iter_active(tmp_path))
    archived = list((tmp_path / "signals" / "archived").glob("*.json"))

    # The recent gateway_instability stays firing; the 5-day-old
    # calibration_drift gets archived.
    assert len(actives) == 1
    assert actives[0].type == "gateway_instability"
    assert actives[0].state == "firing"
    assert actives[0].severity == "alert"

    assert len(archived) == 1
    archived_sig = signals_store.load_signal_file(archived[0])
    assert archived_sig is not None
    assert archived_sig.type == "calibration_drift"
    assert archived_sig.state == "resolved"


def test_backfill_reopens_misarchived_recent_signal(tmp_path):
    """An earlier backfill (pre-firing-window) may have left a signal
    archived even though its latest observation is recent. The next
    backfill must reopen it (resolved → firing) so the user sees it
    in Activity / Maintenance instead of History."""
    now = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=4)).isoformat(timespec="seconds")

    # Simulate the pre-fix state: signal exists in archived/resolved
    # state with a recent last_observed_at.
    sig = signals_store.observe(
        tmp_path,
        signature="sysadmin_watchdog:gateway_instability:pod",
        producer="sysadmin_watchdog",
        type="gateway_instability",
        flavor="maintenance",
        severity="alert",
        scope="pod",
        title="gateway_instability",
    )
    signals_store.apply_transition(
        sig, "resolved", tmp_path, actor="old_backfill",
        reason="pre-firing-window backfill",
    )
    sig.last_observed_at = recent
    signals_store.write_signal(sig, tmp_path)

    # JSONL still has a recent event for this signature
    _write_jsonl(tmp_path, "2026-05-07", [
        _ev(event_type="gateway_instability", bot_id=None,
            severity="alert", timestamp=recent),
    ])

    result = backfill.backfill_watchdog_events(tmp_path, now=now)
    assert result.reopened == 1
    assert result.created == 0
    assert result.skipped_existing == 0

    actives = list(signals_store.iter_active(tmp_path))
    assert len(actives) == 1
    assert actives[0].state == "firing"
    assert actives[0].signature == "sysadmin_watchdog:gateway_instability:pod"


def test_backfill_does_not_reopen_old_archived_signals(tmp_path):
    """If a signal was correctly archived (latest observation outside
    the firing window), don't disturb it on re-run."""
    now = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    old = (now - timedelta(days=10)).isoformat(timespec="seconds")

    sig = signals_store.observe(
        tmp_path,
        signature="evolve_watchdog:calibration_drift:pod",
        producer="evolve_watchdog",
        type="calibration_drift",
        flavor="activity",
        severity="warn",
        scope="pod",
        title="calibration drift",
    )
    signals_store.apply_transition(sig, "resolved", tmp_path, actor="old_backfill")
    sig.last_observed_at = old
    signals_store.write_signal(sig, tmp_path)

    _write_jsonl(tmp_path, "2026-04-27", [
        _ev(event_type="calibration_drift", timestamp=old),
    ])

    result = backfill.backfill_watchdog_events(tmp_path, now=now)
    assert result.reopened == 0
    assert result.skipped_existing == 1
    assert list(signals_store.iter_active(tmp_path)) == []


def test_backfill_does_not_reopen_dismissed_signals(tmp_path):
    """User-dismissed signals must never be re-opened by backfill —
    'don't tell me again' is sticky regardless of recency."""
    now = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=2)).isoformat(timespec="seconds")

    sig = signals_store.observe(
        tmp_path,
        signature="sysadmin_watchdog:config_drift_unexplained:evolve",
        producer="sysadmin_watchdog",
        type="config_drift_unexplained",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id="evolve",
        title="config drift",
    )
    signals_store.apply_transition(
        sig, "dismissed", tmp_path, actor="user", reason="known issue",
    )
    sig.last_observed_at = recent
    signals_store.write_signal(sig, tmp_path)

    _write_jsonl(tmp_path, "2026-05-07", [
        _ev(event_type="config_drift_unexplained", bot_id="evolve",
            severity="alert", timestamp=recent),
    ])

    result = backfill.backfill_watchdog_events(tmp_path, now=now)
    assert result.reopened == 0
    assert result.skipped_existing == 1

    located = signals_store.find_signal(tmp_path, sig.id)
    assert located is not None
    assert located[0].state == "dismissed"  # untouched


def test_backfill_firing_window_zero_archives_everything(tmp_path):
    """firing_window_hours=0 disables the recency carve-out — original
    behaviour, useful when the user is intentionally resetting state."""
    now = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    _write_jsonl(tmp_path, "2026-05-07", [
        _ev(event_type="gateway_instability", bot_id="admin_bot",
            timestamp=recent),
    ])

    backfill.backfill_watchdog_events(tmp_path, now=now, firing_window_hours=0)
    assert list(signals_store.iter_active(tmp_path)) == []
    archived = list((tmp_path / "signals" / "archived").glob("*.json"))
    assert len(archived) == 1


def test_backfill_idempotent(tmp_path):
    """Running backfill twice doesn't duplicate signals."""
    _write_jsonl(tmp_path, "2026-04-15", [
        _ev(event_type="proposal_volume_deviation",
            timestamp="2026-04-15T08:00:00+00:00"),
    ])

    first = backfill.backfill_watchdog_events(tmp_path)
    second = backfill.backfill_watchdog_events(tmp_path)

    assert first.created == 1
    assert second.created == 0
    assert second.skipped_existing == 1


def test_backfill_skips_signatures_with_active_signal(tmp_path):
    """If the live dual-write already created an active Signal for a
    signature, backfill leaves it alone — never overwrites firing state
    with historical 'resolved'."""
    # Live write creates a firing signal
    signals_store.observe(
        tmp_path,
        signature="evolve_watchdog:meta_layer_cost_spike:pod",
        producer="evolve_watchdog",
        type="meta_layer_cost_spike",
        flavor="activity",
        severity="warn",
        scope="pod",
        title="Cost spike",
    )

    # Historical JSONL has the same condition
    _write_jsonl(tmp_path, "2026-04-15", [
        _ev(event_type="meta_layer_cost_spike",
            timestamp="2026-04-15T08:00:00+00:00"),
    ])

    result = backfill.backfill_watchdog_events(tmp_path)
    assert result.created == 0
    assert result.skipped_existing == 1

    # Active signal still firing
    actives = list(signals_store.iter_active(tmp_path))
    assert len(actives) == 1
    assert actives[0].state == "firing"


def test_backfill_skips_unmapped_event_types(tmp_path):
    _write_jsonl(tmp_path, "2026-04-15", [
        _ev(event_type="future_unknown_type",
            timestamp="2026-04-15T08:00:00+00:00"),
    ])
    result = backfill.backfill_watchdog_events(tmp_path)
    assert result.scanned == 1
    assert result.skipped_unmapped == 1
    assert result.created == 0


def test_backfill_respects_since_filter(tmp_path):
    _write_jsonl(tmp_path, "2026-03-01", [
        _ev(event_type="calibration_drift",
            timestamp="2026-03-01T12:00:00+00:00"),
    ])
    _write_jsonl(tmp_path, "2026-04-15", [
        _ev(event_type="proposal_volume_deviation",
            timestamp="2026-04-15T12:00:00+00:00"),
    ])

    since = datetime(2026, 4, 1, tzinfo=timezone.utc)
    result = backfill.backfill_watchdog_events(tmp_path, since=since)
    # Only the April event creates a signal
    assert result.created == 1
    archived = list((tmp_path / "signals" / "archived").glob("*.json"))
    assert len(archived) == 1
    sig = signals_store.load_signal_file(archived[0])
    assert sig is not None
    assert sig.type == "proposal_volume_deviation"


def test_backfill_no_jsonl_dir_is_noop(tmp_path):
    result = backfill.backfill_watchdog_events(tmp_path)
    assert result.scanned == 0
    assert result.created == 0


def test_backfill_handles_corrupt_lines(tmp_path):
    p = tmp_path / "watchdog" / "2026-04-15.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"id": "1", "event_type": "calibration_drift", "severity": "warn", '
        '"timestamp": "2026-04-15T12:00:00+00:00", "bot_id": null, "details": {}}\n'
        'not-json-garbage\n'
        '\n'
        '{"id": "2", "event_type": "proposal_volume_deviation", "severity": "warn", '
        '"timestamp": "2026-04-15T13:00:00+00:00", "bot_id": null, "details": {}}\n',
        encoding="utf-8",
    )
    result = backfill.backfill_watchdog_events(tmp_path)
    # 2 valid lines parsed, garbage line skipped
    assert result.scanned == 2
    assert result.created == 2
