"""tests/test_rsi_budget_hawk_v2.py — Cost ledger + forensics detectors.

Covers the Week-1 MVP of Budget Hawk v2:
  * cost_ledger rollups (per-bot-day, per-trigger-kind, per-cache-state, top_spikes)
  * forensics.detect_summarizer_on_trivial
  * forensics.detect_classifier_noise

Mirror the injection-point pattern from test_rsi_guardians.py: we don't touch
the filesystem — the readers are just callables returning pre-built events.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_ledger  # noqa: E402
from generators.budget_hawk.forensics import (  # noqa: E402
    ForensicsContext,
    detect_app_cost_imbalance,
    detect_classifier_noise,
    detect_summarizer_on_trivial,
)


# ─────────────────────────────────────────────────────────────────────────────
# cost_ledger rollups
# ─────────────────────────────────────────────────────────────────────────────


def _evt(
    *,
    bot="team_bot_a",
    ts="2026-06-01T12:00:00Z",
    session_id="s1",
    trigger_kind="user_turn",
    cache_state="warm",
    cost=0.01,
    model="claude-sonnet-4-5",
    provider="anthropic",
    input_tokens=1000,
    output_tokens=200,
    cache_read=5000,
    cache_write=0,
) -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": ts,
        "bot_id": bot,
        "session_id": session_id,
        "trigger_kind": trigger_kind,
        "model": model,
        "provider": provider,
        "cache_state": cache_state,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost_usd": cost,
    }


def test_rollup_per_bot_day_sums_cost_and_tokens():
    events = [
        _evt(bot="team_bot_a", ts="2026-06-01T10:00:00Z", cost=0.05, input_tokens=1000),
        _evt(bot="team_bot_a", ts="2026-06-01T15:00:00Z", cost=0.03, input_tokens=2000),
        _evt(bot="admin_bot", ts="2026-06-01T10:00:00Z", cost=0.10, input_tokens=5000),
    ]
    rollup = cost_ledger.rollup_per_bot_day(events)

    team_bot_a_key = ("team_bot_a", "2026-06-01")
    admin_bot_key = ("admin_bot", "2026-06-01")
    assert rollup[team_bot_a_key]["event_count"] == 2
    assert rollup[team_bot_a_key]["cost_usd"] == 0.08
    assert rollup[team_bot_a_key]["input_tokens"] == 3000
    assert rollup[admin_bot_key]["event_count"] == 1
    assert rollup[admin_bot_key]["cost_usd"] == 0.10


def test_rollup_per_trigger_kind_separates_summarizer_from_user_turn():
    events = [
        _evt(trigger_kind="user_turn", cost=0.20),
        _evt(trigger_kind="user_turn", cost=0.10),
        _evt(trigger_kind="summarizer", cost=0.005),
        _evt(trigger_kind="classifier", cost=0.002),
    ]
    rollup = cost_ledger.rollup_per_trigger_kind(events)
    assert rollup["user_turn"]["event_count"] == 2
    assert rollup["user_turn"]["cost_usd"] == 0.30
    assert rollup["summarizer"]["cost_usd"] == 0.005
    assert rollup["classifier"]["cost_usd"] == 0.002


def test_rollup_per_cache_state_flags_invalidated():
    """Cache-state rollup is the primary input to the post-key-rotation detector.

    'invalidated' should show up as a distinct bucket; 'unknown' events don't
    get silently absorbed.
    """
    events = [
        _evt(cache_state="warm", cost=0.01),
        _evt(cache_state="warm", cost=0.01),
        _evt(cache_state="invalidated", cost=0.25),  # the spike
        _evt(cache_state="fresh", cost=0.03),
    ]
    rollup = cost_ledger.rollup_per_cache_state(events)
    assert rollup["invalidated"]["event_count"] == 1
    assert rollup["invalidated"]["cost_usd"] == 0.25
    assert rollup["warm"]["event_count"] == 2


def test_top_spikes_returns_n_highest_cost_events():
    events = [
        _evt(cost=0.001),
        _evt(cost=0.250, cache_state="invalidated"),  # the post-rotation spike
        _evt(cost=0.010),
        _evt(cost=0.080),
    ]
    spikes = cost_ledger.top_spikes(events, n=2)
    assert len(spikes) == 2
    assert spikes[0]["cost_usd"] == 0.250
    assert spikes[1]["cost_usd"] == 0.080


def test_rollup_handles_missing_fields_without_crashing():
    # Old records pre-cost_event schema — should flow through as "unknown"
    events = [
        {"type": "cost_event", "bot_id": "team_bot_a", "ts": "2026-06-01T10:00:00Z"},
        _evt(cost=0.05),
    ]
    rollup = cost_ledger.rollup_per_trigger_kind(events)
    assert "unknown" in rollup


# ─────────────────────────────────────────────────────────────────────────────
# forensics.detect_summarizer_on_trivial
# ─────────────────────────────────────────────────────────────────────────────


def _ledger(events):
    return lambda bot_id, days_back: events


def _sessions(summaries):
    return lambda bot_id, days_back: summaries


def test_summarizer_on_trivial_quiet_when_no_trivial_sessions():
    # All sessions have 3 turns — summarizer calls are justified.
    events = [
        _evt(trigger_kind="summarizer", session_id="s1", cost=0.004),
        _evt(trigger_kind="summarizer", session_id="s2", cost=0.004),
    ]
    summaries = [
        {"session_id": "s1", "turn_count": 3},
        {"session_id": "s2", "turn_count": 5},
    ]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_summarizer_on_trivial(ctx) == []


def test_summarizer_on_trivial_fires_when_multiple_trivial_sessions():
    events = [
        _evt(trigger_kind="summarizer", session_id="s1", cost=0.004),
        _evt(trigger_kind="summarizer", session_id="s2", cost=0.004),
        _evt(trigger_kind="summarizer", session_id="s3", cost=0.004),
        _evt(trigger_kind="user_turn", session_id="s1", cost=0.50),  # not summarizer — ignored
    ]
    summaries = [
        {"session_id": "s1", "turn_count": 1},  # trivial
        {"session_id": "s2", "turn_count": 1},  # trivial
        {"session_id": "s3", "turn_count": 3},  # NOT trivial
    ]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    proposals = detect_summarizer_on_trivial(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.generator_id == "budget_hawk"
    assert p.action.kind == "ConfigPatch"
    assert "summarizerMinTurns" in p.action.target_path
    assert p.action.value == 2
    # Provenance should record the offending count (2 trivial sessions)
    assert p.provenance.signals["offending_events"] == 2


def test_summarizer_on_trivial_needs_at_least_two_events():
    # Only one offending event — below the noise floor.
    events = [_evt(trigger_kind="summarizer", session_id="s1", cost=0.004)]
    summaries = [{"session_id": "s1", "turn_count": 1}]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_summarizer_on_trivial(ctx) == []


# ─────────────────────────────────────────────────────────────────────────────
# forensics.detect_classifier_noise
# ─────────────────────────────────────────────────────────────────────────────


def test_classifier_noise_quiet_when_below_call_threshold():
    # 5 classifier calls / 7 days = <1 per day — well under threshold.
    events = [
        _evt(trigger_kind="classifier", cost=0.001, ts=f"2026-06-0{i}T10:00:00Z")
        for i in range(1, 6)
    ]
    summaries = [{"session_id": f"s{i}", "tier_confidence": 0.90} for i in range(20)]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_classifier_noise(ctx) == []


def test_classifier_noise_fires_when_high_volume_and_high_confidence():
    # 30 classifier calls in 1 day, and 90% of sessions resolve at high confidence.
    events = [
        _evt(trigger_kind="classifier", cost=0.002, ts="2026-06-01T10:00:00Z")
        for _ in range(30)
    ]
    summaries = [
        *[{"session_id": f"s{i}", "tier_confidence": 0.90} for i in range(9)],
        {"session_id": "s9", "tier_confidence": 0.55},  # low-conf outlier
    ]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
        classifier_daily_call_threshold=20,
        classifier_keyword_confident_share=0.70,
    )
    proposals = detect_classifier_noise(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.kind == "ConfigPatch"
    assert "classifierKeywordConfidenceFloor" in p.action.target_path
    assert p.action.value == 0.80
    assert p.urgency == "hygiene"


def test_classifier_noise_quiet_when_sessions_are_actually_ambiguous():
    # High volume of calls, but most sessions are genuinely ambiguous — the
    # classifier is earning its keep. Don't propose.
    events = [
        _evt(trigger_kind="classifier", cost=0.002, ts="2026-06-01T10:00:00Z")
        for _ in range(40)
    ]
    summaries = [
        {"session_id": f"s{i}", "tier_confidence": 0.50} for i in range(10)
    ]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
        classifier_daily_call_threshold=20,
        classifier_keyword_confident_share=0.70,
    )
    assert detect_classifier_noise(ctx) == []


# ─────────────────────────────────────────────────────────────────────────────
# cost_ledger.rollup_per_application
# ─────────────────────────────────────────────────────────────────────────────


def test_rollup_per_application_splits_cost_evenly():
    # Session s1 cost $1, invoked [nutrition, calendar] → each gets $0.50.
    # Session s2 cost $0.40, invoked [nutrition] → nutrition gets $0.40.
    # Session s3 cost $0.10, invoked [] → unattributed gets $0.10.
    events = [
        _evt(session_id="s1", cost=1.00),
        _evt(session_id="s2", cost=0.40),
        _evt(session_id="s3", cost=0.10),
    ]
    summaries = [
        {"session_id": "s1", "applications_invoked": ["health-nutrition", "calendar"]},
        {"session_id": "s2", "applications_invoked": ["health-nutrition"]},
        {"session_id": "s3", "applications_invoked": []},
    ]
    rollup = cost_ledger.rollup_per_application(events, summaries)
    assert abs(rollup["health-nutrition"]["cost_usd"] - 0.90) < 1e-9
    assert abs(rollup["calendar"]["cost_usd"] - 0.50) < 1e-9
    assert abs(rollup["(unattributed)"]["cost_usd"] - 0.10) < 1e-9
    # Session counts are distinct-session, not event-count
    assert rollup["health-nutrition"]["session_count"] == 2
    assert rollup["calendar"]["session_count"] == 1


def test_rollup_per_application_handles_unknown_sessions_as_unattributed():
    # Event references a session that has no summary → falls through to
    # (unattributed), not silently dropped.
    events = [_evt(session_id="s_ghost", cost=0.05)]
    summaries: list[dict] = []
    rollup = cost_ledger.rollup_per_application(events, summaries)
    assert "(unattributed)" in rollup
    assert rollup["(unattributed)"]["cost_usd"] == 0.05


# ─────────────────────────────────────────────────────────────────────────────
# forensics.detect_app_cost_imbalance
# ─────────────────────────────────────────────────────────────────────────────


def test_app_imbalance_quiet_when_spend_is_spread():
    # No app dominates; total spend above floor but evenly distributed.
    events = [
        _evt(session_id="s1", cost=0.50),
        _evt(session_id="s2", cost=0.50),
        _evt(session_id="s3", cost=0.50),
    ]
    summaries = [
        {"session_id": "s1", "applications_invoked": ["health-nutrition"]},
        {"session_id": "s2", "applications_invoked": ["calendar"]},
        {"session_id": "s3", "applications_invoked": ["email"]},
    ]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_app_cost_imbalance(ctx) == []


def test_app_imbalance_fires_on_dominance():
    # One app gets 80% of spend, total above floor → Investigation fires.
    events = [
        _evt(session_id="s1", cost=4.00),
        _evt(session_id="s2", cost=0.50),
        _evt(session_id="s3", cost=0.50),
    ]
    summaries = [
        {"session_id": "s1", "applications_invoked": ["health-nutrition"]},
        {"session_id": "s2", "applications_invoked": ["calendar"]},
        {"session_id": "s3", "applications_invoked": ["email"]},
    ]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    proposals = detect_app_cost_imbalance(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.__class__.__name__ == "Investigation"
    assert p.urgency == "hygiene"
    # Phase C-7: app name now lives in Summary + Explanation +
    # provenance, not the (humanized) problem line. Check the
    # operator-visible body and metadata.
    assert (
        "health-nutrition" in p.problem
        or "health-nutrition" in (p.summary or "")
        or "health-nutrition" in (p.explanation or "")
        or "health-nutrition" in p.action.context
        or p.provenance.signals.get("dominant_app") == "health-nutrition"
    )


def test_app_imbalance_fires_on_coverage_gap():
    # Most spend is unattributed → suggests extending applicationPatterns.
    events = [
        _evt(session_id="s1", cost=2.00),
        _evt(session_id="s2", cost=2.00),
        _evt(session_id="s3", cost=0.50),
    ]
    summaries = [
        {"session_id": "s1", "applications_invoked": []},
        {"session_id": "s2", "applications_invoked": []},
        {"session_id": "s3", "applications_invoked": ["calendar"]},
    ]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    proposals = detect_app_cost_imbalance(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.__class__.__name__ == "Investigation"
    assert "unattributed" in p.problem.lower() or "applicationPatterns" in p.action.context


def test_app_imbalance_quiet_below_absolute_floor():
    # Dominance ratio met, but total spend tiny — don't noise the inbox.
    events = [_evt(session_id="s1", cost=0.05)]
    summaries = [{"session_id": "s1", "applications_invoked": ["health-nutrition"]}]
    ctx = ForensicsContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
        app_imbalance_min_usd=1.00,
    )
    assert detect_app_cost_imbalance(ctx) == []


# ─────────────────────────────────────────────────────────────────────────────
# Trigger-key stability — what makes the BE-refresh-loop dedup work
# ─────────────────────────────────────────────────────────────────────────────


def test_warn_cap_trigger_key_is_stable_across_dollar_values():
    """make_warn_cap_crossed must produce the same trigger_observations
    regardless of today's dollar amount.

    The BE refresh re-runs budget_hawk every 15 minutes. As today's spend
    accumulates ($4.23 → $4.51 → $5.02 …), the SAME logical signal
    ("admin_bot crossed the warn cap today") fires repeatedly. If the trigger
    embeds the dollar amount, every cycle produces a fresh fingerprint
    and the queue accumulates duplicates. Stable trigger key → one
    proposal that gets refreshed in place.
    """
    from generators.budget_hawk.proposals import make_warn_cap_crossed

    p1 = make_warn_cap_crossed("admin_bot", current_usd=4.23, cap_usd=2.00)
    p2 = make_warn_cap_crossed("admin_bot", current_usd=7.50, cap_usd=2.00)
    p3 = make_warn_cap_crossed("admin_bot", current_usd=9.99, cap_usd=2.00)
    assert p1.trigger_observations == p2.trigger_observations == p3.trigger_observations
    # Different bot → different key (still per-bot scoped).
    p4 = make_warn_cap_crossed("team_bot_a", current_usd=4.23, cap_usd=2.00)
    assert p4.trigger_observations != p1.trigger_observations
    # The current dollar amount survives in provenance for the UI / refresh.
    assert p2.provenance.signals.get("current_usd") == 7.50


def test_summarizer_on_trivial_trigger_key_is_stable_across_event_counts():
    """Same fingerprint stability invariant for summarizer_on_trivial:
    the count of offending events shifts day-to-day, the trigger key
    must not.
    """
    from generators.budget_hawk.proposals import make_summarizer_min_turns_patch

    p1 = make_summarizer_min_turns_patch(
        "admin_bot", new_value=8, offending_events=3, cost_waste_usd=0.05,
        lookback_days=7, example_session_ids=["s1", "s2"],
    )
    p2 = make_summarizer_min_turns_patch(
        "admin_bot", new_value=8, offending_events=12, cost_waste_usd=0.20,
        lookback_days=7, example_session_ids=["s3"],
    )
    assert p1.trigger_observations == p2.trigger_observations


def test_classifier_noise_trigger_key_is_stable_across_per_day_rates():
    from generators.budget_hawk.proposals import make_classifier_threshold_patch

    p1 = make_classifier_threshold_patch(
        "admin_bot", new_value=0.8, classifier_events=21, per_day=3.0,
        confident_share=0.85, total_cost_usd=0.42, lookback_days=7,
    )
    p2 = make_classifier_threshold_patch(
        "admin_bot", new_value=0.8, classifier_events=49, per_day=7.0,
        confident_share=0.85, total_cost_usd=0.98, lookback_days=7,
    )
    assert p1.trigger_observations == p2.trigger_observations
