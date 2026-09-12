"""tests/test_cascade_pressure_watchdog.py — pod-wide pressure flags.

Per spec § 2.6 watchdog-reliability section + round-3 #7.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from cascade.pressure_watchdog import (  # noqa: E402
    DEFAULT_WATCHDOG_CONFIG,
    PressureFlags,
    WatchdogConfig,
    compute_pressure_flags,
    read_in_process_tier1_counts,
    write_pressure_flags,
)


NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


def _span(*, session_id, bot_id="team_bot_a", tier_used="tier2", escalation_event=None,
          end_time=None, total_cost=0.0, tier_chosen_by=None):
    """Synthesize a cascade telemetry span dict for watchdog tests.

    When `escalation_event` is set and `tier_chosen_by` isn't passed
    explicitly, default chosen_by to "cascade" — preserves the old
    test semantic that "an escalation event happened" implies the
    controller was the driver. Tests for shadow-mode (where cascade
    decided but didn't drive routing) should pass `tier_chosen_by`
    explicitly (e.g., "classifier").
    """
    end_time = end_time or NOW
    attrs = {
        "session_id": session_id,
        "cascade.tier_used": tier_used,
    }
    if escalation_event is not None:
        attrs["cascade.escalation_event"] = escalation_event
        if tier_chosen_by is None:
            tier_chosen_by = "cascade"
    if tier_chosen_by is not None:
        attrs["cascade.tier_chosen_by"] = tier_chosen_by
    return {
        "bot_id": bot_id,
        "end_time": end_time.isoformat(),
        "start_time": end_time.isoformat(),
        "total_cost": total_cost,
        "attributes": attrs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat contract
# ─────────────────────────────────────────────────────────────────────────────


def test_heartbeat_always_written():
    """Per spec § 2.6: every poll writes watchdog_heartbeat regardless
    of flag state."""
    flags = compute_pressure_flags([], now=NOW)
    assert flags.watchdog_heartbeat == NOW.isoformat()
    assert flags.watchdog_ttl_seconds == 180  # spec default


def test_heartbeat_present_when_flags_fire():
    spans = [_span(session_id=f"s{i}", tier_used="tier1") for i in range(10)]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.watchdog_heartbeat == NOW.isoformat()  # heartbeat still set


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent tier1 sessions
# ─────────────────────────────────────────────────────────────────────────────


def test_concurrency_under_cap_no_flag():
    # 2 active tier1 sessions, cap is 3 → no flag.
    spans = [
        _span(session_id="s1", tier_used="tier1"),
        _span(session_id="s2", tier_used="tier1"),
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.pod_tier1_active_sessions == 2
    assert flags.pod_tier1_concurrency_cap is False


def test_concurrency_over_cap_flag_fires():
    # 4 active tier1 sessions, cap 3 → flag.
    spans = [_span(session_id=f"s{i}", tier_used="tier1") for i in range(4)]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.pod_tier1_active_sessions == 4
    assert flags.pod_tier1_concurrency_cap is True


def test_concurrency_excludes_old_sessions():
    # Session whose most-recent span is > 30 min old is no longer in-flight.
    old_ts = NOW - timedelta(minutes=45)
    spans = [
        _span(session_id="s_old", tier_used="tier1", end_time=old_ts),
        _span(session_id="s_active", tier_used="tier1", end_time=NOW),
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.pod_tier1_active_sessions == 1


def test_concurrency_per_bot_breakdown():
    spans = [
        _span(session_id="s1", bot_id="team_bot_a", tier_used="tier1"),
        _span(session_id="s2", bot_id="team_bot_a", tier_used="tier1"),
        _span(session_id="s3", bot_id="admin_bot", tier_used="tier1"),
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.by_bot_tier1_active == {"team_bot_a": 2, "admin_bot": 1}


# ─────────────────────────────────────────────────────────────────────────────
# Escalation storm detection
# ─────────────────────────────────────────────────────────────────────────────


def test_escalation_storm_under_threshold():
    # 4 escalations in 15min — under default threshold of 5.
    spans = [
        _span(session_id=f"s{i}", escalation_event="escalated")
        for i in range(4)
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.escalations_in_15min == 4
    assert flags.escalation_storm is False


def test_escalation_storm_over_threshold():
    spans = [
        _span(session_id=f"s{i}", escalation_event="escalated")
        for i in range(6)
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.escalations_in_15min == 6
    assert flags.escalation_storm is True


def test_escalation_storm_only_counts_recent():
    # Escalations older than 15min don't count.
    old_ts = NOW - timedelta(minutes=20)
    spans = [
        _span(session_id="s_old", escalation_event="escalated", end_time=old_ts),
        _span(session_id="s_new", escalation_event="escalated", end_time=NOW),
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.escalations_in_15min == 1


def test_escalation_storm_ignores_shadow_mode_escalations():
    """During the 2-week Phase 2 shadow window, the controller emits
    escalation events but doesn't drive routing — tier_chosen_by
    stays "classifier"/"user_request"/etc. instead of "cascade". The
    storm flag must NOT trip on these shadow-only escalations,
    otherwise the operator sees a "cascade is escalating wildly!"
    alert on day 3 of shadow mode that's actually just the controller
    making its (un-applied) recommendations.
    """
    # 8 shadow-mode escalations (chosen_by="classifier") — well over
    # the default storm threshold. escalations_in_15min reflects the
    # total; live_escalations_in_15min is 0; escalation_storm stays
    # False.
    spans = [
        _span(
            session_id=f"s{i}",
            escalation_event="escalated",
            tier_chosen_by="classifier",
        )
        for i in range(8)
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.escalations_in_15min == 8
    assert flags.live_escalations_in_15min == 0
    assert flags.escalation_storm is False


def test_escalation_storm_fires_only_on_live_escalations():
    """Mix shadow + live escalations: only live ones drive the
    storm flag. live_escalations_in_15min exposed for transparency."""
    spans = [
        # 5 shadow escalations.
        *[
            _span(session_id=f"shadow-{i}", escalation_event="escalated",
                  tier_chosen_by="classifier")
            for i in range(5)
        ],
        # 6 live escalations (above default storm threshold of 5).
        *[
            _span(session_id=f"live-{i}", escalation_event="escalated",
                  tier_chosen_by="cascade")
            for i in range(6)
        ],
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.escalations_in_15min == 11
    assert flags.live_escalations_in_15min == 6
    assert flags.escalation_storm is True


# ─────────────────────────────────────────────────────────────────────────────
# tier1 spend burst
# ─────────────────────────────────────────────────────────────────────────────


def test_tier1_spend_under_threshold_no_flag():
    spans = [
        _span(session_id=f"s{i}", tier_used="tier1", total_cost=1.0)
        for i in range(5)
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.tier1_pod_spend_per_hour_usd == 5.0
    assert flags.tier1_pod_spend_burst is False


def test_tier1_spend_over_threshold():
    spans = [
        _span(session_id=f"s{i}", tier_used="tier1", total_cost=3.0)
        for i in range(5)  # 15 USD/hr — over default $10
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.tier1_pod_spend_per_hour_usd == 15.0
    assert flags.tier1_pod_spend_burst is True


def test_tier2_spend_doesnt_count_toward_tier1_burst():
    # Only tier1 spend counts.
    spans = [
        _span(session_id=f"s{i}", tier_used="tier2", total_cost=10.0)
        for i in range(5)
    ]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.tier1_pod_spend_per_hour_usd == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry-coupled-failure defense (round-2 cost F1)
# ─────────────────────────────────────────────────────────────────────────────


def test_in_process_counts_taken_as_max_with_spans():
    # Spans say 1 tier1 session, in-process counter says 5 — pod-wide
    # count = max(1, 5) = 5.
    spans = [_span(session_id="s1", tier_used="tier1")]
    flags = compute_pressure_flags(
        spans, now=NOW,
        in_process_tier1_counts={"team_bot_a": 3, "admin_bot": 2},
    )
    assert flags.pod_tier1_active_sessions == 5


def test_telemetry_lost_reduces_concurrency_cap():
    # When telemetry_lost=True, effective cap = max(2, default/2) = 2 (with default 3 // 2 = 1, max(2,1)=2)
    spans = [_span(session_id=f"s{i}", tier_used="tier1") for i in range(3)]
    flags = compute_pressure_flags(spans, now=NOW, telemetry_lost=True)
    assert flags.telemetry_partially_lost is True
    assert flags.effective_concurrency_cap == 2
    # With cap=2 and 3 active, the flag fires.
    assert flags.pod_tier1_concurrency_cap is True


def test_telemetry_lost_no_in_process_data_still_caps():
    # Even without in-process data, telemetry_lost reduces the cap.
    flags = compute_pressure_flags([], now=NOW, telemetry_lost=True)
    assert flags.effective_concurrency_cap == 2


# ─────────────────────────────────────────────────────────────────────────────
# Pressure event correlator (round-3 #7)
# ─────────────────────────────────────────────────────────────────────────────


def test_pressure_event_id_set_when_any_flag_fires():
    spans = [_span(session_id=f"s{i}", tier_used="tier1") for i in range(5)]
    flags = compute_pressure_flags(spans, now=NOW)
    assert flags.pod_tier1_concurrency_cap is True
    assert flags.pressure_event_id is not None
    assert "pressure-" in flags.pressure_event_id


def test_pressure_event_id_unset_when_no_flag():
    flags = compute_pressure_flags([], now=NOW)
    assert flags.pressure_event_id is None


def test_pressure_event_id_stable_within_15min_window():
    # Same 15min window → same event id.
    spans1 = [_span(session_id=f"s{i}", tier_used="tier1") for i in range(5)]
    spans2 = [_span(session_id=f"s{i+5}", tier_used="tier1") for i in range(5)]
    flags1 = compute_pressure_flags(spans1, now=NOW)
    flags2 = compute_pressure_flags(
        spans2, now=NOW + timedelta(minutes=5),  # still in same 15-min window
    )
    assert flags1.pressure_event_id == flags2.pressure_event_id


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────


def test_write_pressure_flags_atomic_round_trip(tmp_path: Path):
    flags = PressureFlags(
        pod_tier1_concurrency_cap=True,
        pod_tier1_active_sessions=4,
        watchdog_heartbeat="2026-05-27T12:00:00+00:00",
        pressure_event_id="pressure-test",
    )
    path = write_pressure_flags(flags, tmp_path)
    assert path == tmp_path / "cascade" / "pressure_flags.json"
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["pod_tier1_concurrency_cap"] is True
    assert parsed["pod_tier1_active_sessions"] == 4
    assert parsed["pressure_event_id"] == "pressure-test"


def test_read_in_process_tier1_counts_aggregates_per_bot(tmp_path: Path):
    # Write per-bot files; reader should aggregate them.
    for bot, count in [("team_bot_a", 2), ("admin_bot", 1)]:
        d = tmp_path / bot / "cascade"
        d.mkdir(parents=True)
        (d / "tier1_active.json").write_text(
            json.dumps({"active_count": count}), encoding="utf-8",
        )
    result = read_in_process_tier1_counts(tmp_path)
    assert result == {"team_bot_a": 2, "admin_bot": 1}


def test_read_in_process_tier1_counts_tolerates_malformed(tmp_path: Path):
    # Malformed JSON or missing field — silently skip the bot.
    d = tmp_path / "broken" / "cascade"
    d.mkdir(parents=True)
    (d / "tier1_active.json").write_text("not json", encoding="utf-8")
    result = read_in_process_tier1_counts(tmp_path)
    assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# Spec lock-in
# ─────────────────────────────────────────────────────────────────────────────


def test_default_thresholds_match_spec():
    cfg = DEFAULT_WATCHDOG_CONFIG
    assert cfg.max_concurrent_tier1_sessions == 3
    assert cfg.max_escalations_per_15min == 5
    assert cfg.tier1_pod_spend_per_hour_usd == 10.0
    assert cfg.watchdog_ttl_seconds == 180
