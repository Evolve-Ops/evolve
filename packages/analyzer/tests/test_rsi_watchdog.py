"""tests/test_rsi_watchdog.py — Active Evolve Watchdog + event store."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.evolve_watchdog import (  # noqa: E402
    EvolveWatchdogContext,
    observe,
    read_events_range,
    write_events,
)
from proposal_synthesizer.store import iter_candidates  # noqa: E402
from schema import (  # noqa: E402
    WatchdogEvent,
    new_watchdog_event_id,
)


# Phase 6c cutover: observe() returns []; findings flow as candidates.


def _candidates(shared_dir: Path) -> list:
    return list(iter_candidates(shared_dir, subdirs=("pending",)))


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _ctx(
    tmp_path,
    *,
    history={},
    dominance={},
    pod_stats={},
    active=[],
):
    return EvolveWatchdogContext(
        bot_id=None,
        shared_dir=tmp_path,
        history_reader=lambda gid, days: history.get(gid, {}),
        dominance_reader=lambda: dominance,
        pod_stats_reader=lambda: pod_stats,
        active_generator_ids=list(active),
        now=_NOW,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Event store
# ─────────────────────────────────────────────────────────────────────────────


def test_event_store_write_and_read_roundtrip(tmp_path):
    events = [
        WatchdogEvent(
            id=new_watchdog_event_id(),
            bot_id=None,
            timestamp=_NOW.isoformat(),
            event_type="proposal_volume_deviation",
            severity="warn",
            details={"generator_id": "ae", "ratio": 0.1},
        ),
        WatchdogEvent(
            id=new_watchdog_event_id(),
            bot_id="team_bot_a",
            timestamp=_NOW.isoformat(),
            event_type="calibration_drift",
            severity="info",
            details={"drift_pct": 0.1},
        ),
    ]
    path = write_events(events, shared_dir=tmp_path, day=_NOW)
    assert path is not None

    read_back = list(
        read_events_range(tmp_path, _NOW - timedelta(hours=1), _NOW + timedelta(hours=1))
    )
    assert len(read_back) == 2
    types = {e.event_type for e in read_back}
    assert "proposal_volume_deviation" in types


def test_event_store_no_events_does_not_create_file(tmp_path):
    path = write_events([], shared_dir=tmp_path, day=_NOW)
    assert path is None


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────


def test_quiet_when_all_readers_empty(tmp_path):
    ctx = _ctx(tmp_path)
    assert observe(ctx) == []


def test_volume_spike_emits_alert(tmp_path):
    history = {
        "gen_a": {
            "proposals_emitted": 30,  # spike
            "baseline_volume": 5.0,
        }
    }
    ctx = _ctx(tmp_path, history=history, active=["gen_a"])
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert cands[0].draft_approval_audience == "pod_operator"


def test_auto_revert_spike_throttles_generator(tmp_path):
    history = {
        "gen_a": {
            "auto_revert_rate": 0.60,
            "auto_revert_rate_baseline": 0.30,
        }
    }
    ctx = _ctx(tmp_path, history=history, active=["gen_a"])
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert cands[0].draft_action.kind == "ThrottleGenerator"
    assert cands[0].draft_action.generator_id == "gen_a"
    assert cands[0].draft_action.throttle_type == "reduce_cadence"


def test_dominance_emits_lower_confidence_throttle(tmp_path):
    dominance = {"adj_explorer": 0.75}
    ctx = _ctx(tmp_path, dominance=dominance)
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert cands[0].draft_action.kind == "ThrottleGenerator"
    assert cands[0].draft_action.throttle_type == "lower_confidence"


def test_verification_reliability_drop_throttles(tmp_path):
    history = {
        "gen_a": {
            "verification_reliability": 0.50,
            "verification_reliability_baseline": 0.85,
        }
    }
    ctx = _ctx(tmp_path, history=history, active=["gen_a"])
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert cands[0].draft_action.kind == "ThrottleGenerator"


def test_cost_spike_emits_investigation(tmp_path):
    pod_stats = {
        "meta_cost_7d_usd": 15.0,
        "meta_cost_baseline_usd": 5.0,
    }
    ctx = _ctx(tmp_path, pod_stats=pod_stats)
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert cands[0].draft_action.kind == "Investigation"
    assert cands[0].draft_urgency == "operational_urgent"


def test_extraction_drift_emits_investigation(tmp_path):
    pod_stats = {
        "tuple_count_per_session_now": 0.5,
        "tuple_count_per_session_baseline": 3.0,
    }
    ctx = _ctx(tmp_path, pod_stats=pod_stats)
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert "extraction" in cands[0].draft_problem.lower()


def test_events_persisted_even_at_info_severity(tmp_path):
    # Calibration drift of 0.10 doesn't cross threshold → no event
    pod_stats = {"calibration_drift_pct": 0.10}
    ctx = _ctx(tmp_path, pod_stats=pod_stats)
    assert observe(ctx) == []
    assert _candidates(tmp_path) == []


def test_multiple_detectors_dont_duplicate_proposals(tmp_path):
    # Single generator with BOTH auto-revert spike AND verification drop
    history = {
        "gen_a": {
            "auto_revert_rate": 0.60,
            "auto_revert_rate_baseline": 0.30,
            "verification_reliability": 0.50,
            "verification_reliability_baseline": 0.85,
        }
    }
    ctx = _ctx(tmp_path, history=history, active=["gen_a"])
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    # Two distinct event types but same throttle intent — deduped to 1
    # candidate per (generator_id, event_type). Two event types → two
    # candidates.
    assert len(cands) == 2
    for c in cands:
        assert c.draft_action.generator_id == "gen_a"


def test_watchdog_always_routes_to_pod_operator(tmp_path):
    pod_stats = {"meta_cost_7d_usd": 100.0, "meta_cost_baseline_usd": 5.0}
    ctx = _ctx(tmp_path, pod_stats=pod_stats)
    assert observe(ctx) == []
    for c in _candidates(tmp_path):
        assert c.draft_approval_audience == "pod_operator"


def test_severity_floor_filters_info_events(tmp_path):
    # calibration_drift fires at "warn" only; raising the floor to
    # "alert" filters it out.
    pod_stats = {"calibration_drift_pct": 0.40}
    ctx = _ctx(tmp_path, pod_stats=pod_stats)
    ctx.proposal_severity_floor = "alert"  # raise the bar
    assert observe(ctx) == []
    assert _candidates(tmp_path) == []


def test_events_written_to_disk_after_observe(tmp_path):
    history = {
        "gen_a": {
            "proposals_emitted": 30,
            "baseline_volume": 5.0,
        }
    }
    ctx = _ctx(tmp_path, history=history, active=["gen_a"])
    observe(ctx)
    # Events should be persisted
    events = list(
        read_events_range(
            tmp_path, _NOW - timedelta(days=1), _NOW + timedelta(days=1)
        )
    )
    assert len(events) >= 1
