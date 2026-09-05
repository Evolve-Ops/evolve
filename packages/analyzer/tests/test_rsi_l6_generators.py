"""tests/test_rsi_l6_generators.py — Persona Tuner, Efficiency Hawk."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.efficiency_hawk import (  # noqa: E402
    EfficiencyHawkContext,
    observe as eh_observe,
)
from generators.efficiency_hawk.cost_efficiency import (  # noqa: E402
    CostEfficiencyContext,
    detect_background_dominance,
    detect_tier_misrouting,
)
from generators.persona_tuner import (  # noqa: E402
    PersonaTunerContext,
    observe as pt_observe,
)
from metrics import known, resolve  # noqa: E402
from metrics.resolvers import cost_metrics as cm_cost  # noqa: E402
from observations.access import window  # noqa: E402
from observations.tuples import write_tuples  # noqa: E402
from schema import ObservationTuple, new_tuple_id  # noqa: E402


_DAY = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _seed(tmp_path, *, moods=("neutral",), n=10, noun="fitness", verb="tracking", bot_id="team_bot_a", engagement=3):
    tuples = []
    for i in range(n):
        mood = moods[i % len(moods)]
        tuples.append(
            ObservationTuple(
                id=new_tuple_id(),
                bot_id=bot_id,
                session_id=f"s{i}",
                segment_id="seg",
                noun=noun,
                verb=verb,
                mood=mood,
                engagement=engagement,
                timestamp_start=_DAY.isoformat(),
                timestamp_end=_DAY.isoformat(),
                source_hash=f"sh-{i}",
            )
        )
    write_tuples(tuples, shared_dir=tmp_path, bot_id=bot_id, day=_DAY)


# ─────────────────────────────────────────────────────────────────────────────
# Persona Tuner
# ─────────────────────────────────────────────────────────────────────────────


def test_persona_tuner_silent_on_healthy_moods(tmp_path):
    _seed(tmp_path, moods=("enthusiastic", "neutral"), n=10)
    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = PersonaTunerContext(bot_id="team_bot_a", window=w)
    assert pt_observe(ctx) == []


def test_persona_tuner_fires_on_frustration_pattern(tmp_path):
    # 40% frustrated → above 25% default threshold
    _seed(tmp_path, moods=("frustrated", "neutral", "neutral", "frustrated", "neutral"), n=10, engagement=5)
    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    # Phase 6c: observe() returns []; inspect candidates store.
    ctx = PersonaTunerContext(bot_id="team_bot_a", window=w, shared_dir=tmp_path)
    assert pt_observe(ctx) == []
    from proposal_synthesizer.store import iter_candidates as _iter

    cands = list(_iter(tmp_path, subdirs=("pending",)))
    assert len(cands) == 1
    c = cands[0]
    assert c.draft_action.kind == "AgentsAppend"
    assert c.draft_urgency == "improvement"


def test_persona_tuner_requires_min_engagement(tmp_path):
    # Lots of frustration but low engagement → skip
    _seed(tmp_path, moods=("frustrated",), n=3, engagement=1)
    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = PersonaTunerContext(bot_id="team_bot_a", window=w, min_cluster_engagement=10)
    assert pt_observe(ctx) == []


def test_persona_tuner_produces_evidence_observations(tmp_path):
    _seed(tmp_path, moods=("frustrated", "neutral", "neutral", "frustrated"), n=10, engagement=5)
    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = PersonaTunerContext(bot_id="team_bot_a", window=w, shared_dir=tmp_path)
    assert pt_observe(ctx) == []
    from proposal_synthesizer.store import iter_candidates as _iter

    cands = list(_iter(tmp_path, subdirs=("pending",)))
    # Charter requires ≥ 3 trigger observations — verify
    assert len(cands[0].trigger_observations) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# Efficiency Hawk
# ─────────────────────────────────────────────────────────────────────────────


def test_efficiency_hawk_silent_on_uniform_efficiency(tmp_path):
    # Seed multiple clusters with similar ratios
    for noun, verb in (("fitness", "tracking"), ("email", "drafting"), ("code", "reviewing")):
        tuples = [
            ObservationTuple(
                id=new_tuple_id(),
                bot_id="team_bot_a",
                session_id=f"s-{noun}-{i}",
                segment_id="seg",
                noun=noun,
                verb=verb,
                mood="neutral",
                engagement=3,
                timestamp_start=_DAY.isoformat(),
                timestamp_end=_DAY.isoformat(),
                source_hash=f"sh-{noun}-{i}",
            )
            for i in range(6)
        ]
        write_tuples(tuples, shared_dir=tmp_path, bot_id="team_bot_a", day=_DAY)

    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = EfficiencyHawkContext(bot_id="team_bot_a", window=w)
    assert eh_observe(ctx) == []


def test_efficiency_hawk_fires_on_outlier_cluster(tmp_path):
    # Baseline clusters: 3 turns/session
    for noun, verb in (("email", "drafting"), ("code", "reviewing"), ("news", "summarizing")):
        tuples = [
            ObservationTuple(
                id=new_tuple_id(),
                bot_id="team_bot_a",
                session_id=f"s-{noun}-{i}",
                segment_id="seg",
                noun=noun,
                verb=verb,
                mood="neutral",
                engagement=3,
                timestamp_start=_DAY.isoformat(),
                timestamp_end=_DAY.isoformat(),
                source_hash=f"sh-{noun}-{i}",
            )
            for i in range(6)
        ]
        write_tuples(tuples, shared_dir=tmp_path, bot_id="team_bot_a", day=_DAY)

    # Outlier: troubleshooting uses 15 turns/session
    tuples = [
        ObservationTuple(
            id=new_tuple_id(),
            bot_id="team_bot_a",
            session_id=f"s-tshoot-{i}",
            segment_id="seg",
            noun="config",
            verb="troubleshooting",
            mood="neutral",
            engagement=25,
            timestamp_start=_DAY.isoformat(),
            timestamp_end=_DAY.isoformat(),
            source_hash=f"sh-tshoot-{i}",
        )
        for i in range(5)
    ]
    write_tuples(tuples, shared_dir=tmp_path, bot_id="team_bot_a", day=_DAY)

    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    # Lower stdev_threshold — with only 4 clusters, a single big outlier
    # pulls mean + stdev up together. Real-world runs have many more
    # clusters; the default 1.5 works there.
    # Phase 2 cutover: efficiency_hawk emits CandidateProposals, not
    # Proposals. Set shared_dir so the candidate write lands somewhere
    # we can inspect, then read from candidates/pending/ instead of
    # observe()'s (now empty) return list.
    ctx = EfficiencyHawkContext(
        bot_id="team_bot_a",
        window=w,
        stdev_threshold=0.8,
        shared_dir=tmp_path,
    )
    assert eh_observe(ctx) == []

    from proposal_synthesizer.store import iter_candidates as iter_candidates_store

    candidates = list(iter_candidates_store(tmp_path, subdirs=("pending",)))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.draft_action.kind == "AgentsAppend"
    assert "troubleshooting" in (c.provenance.signals.get("verb", "") if c.provenance else "")


def test_efficiency_hawk_needs_enough_clusters(tmp_path):
    # Only 2 clusters — not enough to compute meaningful stdev
    for noun in ("fitness", "email"):
        tuples = [
            ObservationTuple(
                id=new_tuple_id(),
                bot_id="team_bot_a",
                session_id=f"s-{noun}-{i}",
                segment_id="seg",
                noun=noun,
                verb="tracking",
                mood="neutral",
                engagement=5,
                timestamp_start=_DAY.isoformat(),
                timestamp_end=_DAY.isoformat(),
                source_hash=f"sh-{noun}-{i}",
            )
            for i in range(6)
        ]
        write_tuples(tuples, shared_dir=tmp_path, bot_id="team_bot_a", day=_DAY)

    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = EfficiencyHawkContext(bot_id="team_bot_a", window=w)
    assert eh_observe(ctx) == []


# ─────────────────────────────────────────────────────────────────────────────
# Efficiency Hawk — background-dominance detector + cost.background_share_trend
# ─────────────────────────────────────────────────────────────────────────────


def _evt(
    *,
    bot="team_bot_a",
    ts="2026-05-01T12:00:00Z",
    session_id="s1",
    trigger_kind="user_turn",
    cost=0.01,
    model="claude-sonnet-4-6",
):
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": ts,
        "bot_id": bot,
        "session_id": session_id,
        "trigger_kind": trigger_kind,
        "cost_usd": cost,
        "model": model,
    }


def _ledger(events):
    return lambda bot_id, days_back: events


def _sessions(summaries):
    return lambda bot_id, days_back: summaries


def _spread_days(n_days, base_iso="2026-05-01"):
    """Yield ISO timestamps spanning ``n_days`` distinct days."""
    base = datetime.fromisoformat(base_iso).replace(tzinfo=timezone.utc)
    return [(base + timedelta(days=i)).isoformat() for i in range(n_days)]


def test_background_dominance_fires_when_majority_is_background():
    # 7 days of activity. Background spend (heartbeat + classifier) dominates
    # foreground (user_turn). Background share should land well above 0.40.
    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="heartbeat", cost=0.50, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="classifier", cost=0.21, ts=ts, session_id=f"cl-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.14, ts=ts, session_id=f"ut-{i}"))
    ctx = CostEfficiencyContext(bot_id="team_bot_a", ledger_reader=_ledger(events))
    proposals = detect_background_dominance(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.generator_id == "efficiency_hawk"
    assert p.action.kind == "Investigation"
    assert p.claim is not None
    assert p.claim.metric == "cost.background_share_trend"
    assert p.claim.direction == "down"
    assert p.claim.fallback == "flag"
    assert p.provenance.signals["background_share"] >= 0.40
    top = p.provenance.signals["top_kinds"]
    assert any(k["kind"] == "heartbeat" for k in top)
    # Headline is action-led; problem stays the symptom.
    assert p.admin_surface_summary.startswith("Reduce team_bot_a background spend share")
    assert p.admin_surface_summary != p.problem
    assert len(p.admin_surface_summary) <= 120


def test_background_dominance_silent_on_balanced_bot():
    # 20% heartbeat / 80% user_turn — well under the 0.40 dominance bar.
    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="heartbeat", cost=0.10, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.40, ts=ts, session_id=f"ut-{i}"))
    ctx = CostEfficiencyContext(bot_id="team_bot_a", ledger_reader=_ledger(events))
    assert detect_background_dominance(ctx) == []


def test_background_dominance_silent_under_min_total_cost():
    # 90% heartbeat but total cost is well below $1 — quiet bot.
    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="heartbeat", cost=0.01, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.001, ts=ts, session_id=f"ut-{i}"))
    ctx = CostEfficiencyContext(bot_id="team_bot_a", ledger_reader=_ledger(events))
    assert detect_background_dominance(ctx) == []


def test_background_dominance_silent_on_first_week_bot():
    # Heavy heartbeat, total over $1, but only 3 distinct days of data.
    events = []
    for i, ts in enumerate(_spread_days(3)):
        events.append(_evt(trigger_kind="heartbeat", cost=0.80, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.10, ts=ts, session_id=f"ut-{i}"))
    ctx = CostEfficiencyContext(bot_id="team_bot_a", ledger_reader=_ledger(events))
    assert detect_background_dominance(ctx) == []


def test_background_dominance_silent_when_mostly_unattributed():
    # Most events lack a trigger_kind we recognise — can't trust the split.
    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="", cost=0.70, ts=ts, session_id=f"un-{i}"))
        events.append(_evt(trigger_kind="heartbeat", cost=0.20, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.10, ts=ts, session_id=f"ut-{i}"))
    ctx = CostEfficiencyContext(bot_id="team_bot_a", ledger_reader=_ledger(events))
    assert detect_background_dominance(ctx) == []


def test_background_dominance_respects_disabled_via_high_threshold():
    # Operator-style opt-out: bumping the threshold above 1.0 silences the
    # detector even with majority-background spend (Sandbox / Security
    # bots whose role IS background).
    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="heartbeat", cost=0.50, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.10, ts=ts, session_id=f"ut-{i}"))
    ctx = CostEfficiencyContext(
        bot_id="security_bot",
        ledger_reader=_ledger(events),
        background_share_threshold=1.01,  # impossible to exceed
    )
    assert detect_background_dominance(ctx) == []


# ─── Resolver: cost.background_share_trend ──────────────────────────────────


def test_cost_background_share_trend_registered():
    assert "cost.background_share_trend" in known()


def _write_cost_events(tmp_path, bot_id, events_by_date):
    """Write cost_events-{date}.jsonl files under
    {tmp_path}/annotations/{bot_id}/ — the on-disk layout the
    cost_event_converter writes."""
    import json

    base = tmp_path / "annotations" / bot_id
    base.mkdir(parents=True, exist_ok=True)
    for date_iso, events in events_by_date.items():
        path = base / f"cost_events-{date_iso}.jsonl"
        with path.open("w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")


def test_cost_background_share_resolver_matches_disk_state(tmp_path):
    cm_cost.set_shared_dir(tmp_path)
    try:
        events_by_date = {}
        for i, ts in enumerate(_spread_days(4)):
            date_iso = ts[:10]
            events_by_date[date_iso] = [
                _evt(trigger_kind="heartbeat", cost=0.30, ts=ts, session_id=f"hb-{i}"),
                _evt(trigger_kind="user_turn", cost=0.10, ts=ts, session_id=f"ut-{i}"),
            ]
        _write_cost_events(tmp_path, "team_bot_a", events_by_date)

        # as_of just past the last event so the trailing 14d window covers them all
        as_of = datetime.fromisoformat("2026-05-04").replace(tzinfo=timezone.utc) + timedelta(days=1)
        v = resolve("cost.background_share_trend", "team_bot_a", as_of)
        # background = 4 × 0.30 = 1.20, classified = 1.20 + 0.40 = 1.60
        # share = 1.20 / 1.60 = 0.75
        assert abs(v.value - 0.75) < 1e-6
        assert v.confidence == 1.0
    finally:
        cm_cost.set_shared_dir(Path("/Users/Shared/evolve"))


def test_cost_background_share_resolver_low_confidence_on_no_data(tmp_path):
    cm_cost.set_shared_dir(tmp_path)
    try:
        as_of = datetime(2026, 5, 1, tzinfo=timezone.utc)
        v = resolve("cost.background_share_trend", "ghost_bot", as_of)
        assert v.value == 0.0
        assert v.confidence < 0.5
    finally:
        cm_cost.set_shared_dir(Path("/Users/Shared/evolve"))


def test_fallback_counts_as_foreground_not_background():
    # ``fallback`` is a user-induced gateway retry — must not inflate
    # the background share. Without this rule, a bot with a flaky
    # gateway would look background-dominated.
    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="fallback", cost=0.50, ts=ts, session_id=f"fb-{i}"))
        events.append(_evt(trigger_kind="heartbeat", cost=0.20, ts=ts, session_id=f"hb-{i}"))
    # 70/30 share IF fallback were background; with fallback as
    # foreground, share is 20/(20+50) = 0.286 → silent.
    ctx = CostEfficiencyContext(bot_id="flaky_bot", ledger_reader=_ledger(events))
    assert detect_background_dominance(ctx) == []


def test_efficiency_hawk_dispatcher_skips_cost_when_disabled(tmp_path):
    # cost_detectors_disabled=True silences the cost detector even when
    # a ledger_reader is wired and the events would otherwise fire.
    from generators.efficiency_hawk import EfficiencyHawkContext

    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="heartbeat", cost=0.50, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.10, ts=ts, session_id=f"ut-{i}"))
    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = EfficiencyHawkContext(
        bot_id="team_bot_a",
        window=w,
        ledger_reader=_ledger(events),
        cost_detectors_disabled=True,
    )
    # No tuples seeded → cluster detector silent. Cost detector disabled
    # → no proposals at all.
    assert eh_observe(ctx) == []


def test_efficiency_hawk_dispatcher_applies_cost_overrides(tmp_path):
    # cost_overrides on the outer context should be propagated to
    # CostEfficiencyContext fields. Setting an impossible threshold
    # silences a would-otherwise-fire detector.
    from generators.efficiency_hawk import EfficiencyHawkContext

    events = []
    for i, ts in enumerate(_spread_days(7)):
        events.append(_evt(trigger_kind="heartbeat", cost=0.50, ts=ts, session_id=f"hb-{i}"))
        events.append(_evt(trigger_kind="user_turn", cost=0.10, ts=ts, session_id=f"ut-{i}"))
    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = EfficiencyHawkContext(
        bot_id="team_bot_a",
        window=w,
        ledger_reader=_ledger(events),
        cost_overrides={"background_share_threshold": 1.01},
    )
    assert eh_observe(ctx) == []


def test_runner_does_not_disable_cost_detectors_for_member_role(tmp_path):
    from generator_runner import _make_efficiency_hawk_ctx

    network = {"bots": {"team_bot_a": {"role": "member"}}}
    ctx = _make_efficiency_hawk_ctx(
        shared_dir=tmp_path,
        network_config=network,
        bot_id="team_bot_a",
        gen_config={},
        now=_DAY,
    )
    assert ctx.cost_detectors_disabled is False


def test_runner_propagates_cost_overrides_from_gen_config(tmp_path):
    # Operator overrides under gen_config["cost"] should reach the
    # EfficiencyHawkContext as cost_overrides — and only known fields
    # survive the filter (so a typo doesn't silently land on the ctx).
    from generator_runner import _make_efficiency_hawk_ctx

    network = {"bots": {"team_bot_a": {"role": "member"}}}
    gen_config = {
        "cost": {
            "background_share_threshold": 0.55,
            "min_total_cost_usd": 5.0,
            "not_a_real_field": "ignored",
        }
    }
    ctx = _make_efficiency_hawk_ctx(
        shared_dir=tmp_path,
        network_config=network,
        bot_id="team_bot_a",
        gen_config=gen_config,
        now=_DAY,
    )
    assert ctx.cost_overrides == {
        "background_share_threshold": 0.55,
        "min_total_cost_usd": 5.0,
    }


def test_runner_explicit_cost_disabled_via_gen_config(tmp_path):
    # gen_config["cost_disabled"]=True silences cost detectors even on
    # a member-role bot (operator escape hatch).
    from generator_runner import _make_efficiency_hawk_ctx

    network = {"bots": {"team_bot_a": {"role": "member"}}}
    ctx = _make_efficiency_hawk_ctx(
        shared_dir=tmp_path,
        network_config=network,
        bot_id="team_bot_a",
        gen_config={"cost_disabled": True},
        now=_DAY,
    )
    assert ctx.cost_detectors_disabled is True


# ─────────────────────────────────────────────────────────────────────────────
# Efficiency Hawk — tier-misrouting detector + maintenance_high_tier_share
# ─────────────────────────────────────────────────────────────────────────────


def _maintenance_summaries(session_ids):
    """Build session_summary dicts for sessions classified as maintenance."""
    return [
        {"session_id": sid, "session_class": "maintenance"}
        for sid in session_ids
    ]


def test_tier_misrouting_fires_when_maintenance_runs_on_sonnet():
    # 7 days, 6 maintenance sessions, all running on Sonnet. Total
    # maintenance cost > $0.50, share = 100% → fire.
    sessions = [f"sess-{i}" for i in range(6)]
    summaries = _maintenance_summaries(sessions)
    events = []
    for i, ts in enumerate(_spread_days(7)):
        for j, sid in enumerate(sessions):
            events.append(
                _evt(
                    trigger_kind="user_turn",
                    cost=0.10,
                    ts=ts,
                    session_id=sid,
                    model="claude-sonnet-4-6",
                )
            )
    ctx = CostEfficiencyContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    proposals = detect_tier_misrouting(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.generator_id == "efficiency_hawk"
    assert p.action.kind == "TierAdjustment"
    assert p.action.target_class == "maintenance"
    assert p.action.new_tier == "haiku"
    assert p.claim is not None
    assert p.claim.metric == "cost.maintenance_high_tier_share"
    assert p.claim.direction == "down"
    assert p.claim.fallback == "revert"  # TierAdjustment is auto-revertable
    assert p.urgency == "hygiene"
    assert p.approval_audience == "pod_operator"
    # Provenance should record the dominant high-tier model
    assert p.provenance.signals["high_tier_share"] >= 0.50
    assert p.provenance.signals["maintenance_session_count"] == 6
    top = p.provenance.signals["top_models"]
    assert any("sonnet" in m["model"].lower() for m in top)
    # Headline is action-led; "— downgrade to haiku" no longer in problem.
    assert p.admin_surface_summary.startswith("Downgrade team_bot_a maintenance sessions to haiku")
    assert "downgrade" not in p.problem.lower()
    assert p.admin_surface_summary != p.problem
    assert len(p.admin_surface_summary) <= 120


def test_tier_misrouting_silent_when_maintenance_runs_on_haiku():
    # Same shape but on the right tier — nothing to propose.
    sessions = [f"sess-{i}" for i in range(6)]
    summaries = _maintenance_summaries(sessions)
    events = []
    for i, ts in enumerate(_spread_days(7)):
        for sid in sessions:
            events.append(
                _evt(
                    cost=0.10,
                    ts=ts,
                    session_id=sid,
                    model="claude-haiku-4-5",
                )
            )
    ctx = CostEfficiencyContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_tier_misrouting(ctx) == []


def test_tier_misrouting_silent_below_min_sessions():
    # Heavy Sonnet-on-maintenance use, but only 3 maintenance sessions
    # in the window — below the statistical floor.
    sessions = [f"sess-{i}" for i in range(3)]
    summaries = _maintenance_summaries(sessions)
    events = []
    for i, ts in enumerate(_spread_days(7)):
        for sid in sessions:
            events.append(
                _evt(cost=0.50, ts=ts, session_id=sid, model="claude-sonnet-4-6")
            )
    ctx = CostEfficiencyContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_tier_misrouting(ctx) == []


def test_tier_misrouting_silent_below_savings_floor():
    # Enough sessions and the right share, but the dollar savings are
    # below the floor — pennies aren't worth pestering for.
    sessions = [f"sess-{i}" for i in range(6)]
    summaries = _maintenance_summaries(sessions)
    events = []
    for i, ts in enumerate(_spread_days(7)):
        for sid in sessions:
            # $0.001 × 6 sessions × 7 days = $0.042 — well under $0.50.
            events.append(
                _evt(cost=0.001, ts=ts, session_id=sid, model="claude-sonnet-4-6")
            )
    ctx = CostEfficiencyContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_tier_misrouting(ctx) == []


def test_tier_misrouting_silent_when_session_reader_missing():
    # Without a session_reader the detector cannot identify maintenance
    # sessions; should skip silently rather than crash.
    events = [
        _evt(cost=1.00, ts=ts, session_id=f"s-{i}", model="claude-sonnet-4-6")
        for i, ts in enumerate(_spread_days(7))
    ]
    ctx = CostEfficiencyContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=None,
    )
    assert detect_tier_misrouting(ctx) == []


def test_tier_misrouting_silent_when_mostly_unknown_models():
    # Most of the maintenance cost has unparseable model strings — we
    # can't classify by tier, so don't fire.
    sessions = [f"sess-{i}" for i in range(6)]
    summaries = _maintenance_summaries(sessions)
    events = []
    for i, ts in enumerate(_spread_days(7)):
        for sid in sessions:
            # 80% on a model that won't classify, 20% on Sonnet.
            events.append(_evt(cost=0.40, ts=ts, session_id=sid, model="custom-llm-v3"))
            events.append(_evt(cost=0.10, ts=ts, session_id=sid, model="claude-sonnet-4-6"))
    ctx = CostEfficiencyContext(
        bot_id="team_bot_a",
        ledger_reader=_ledger(events),
        session_reader=_sessions(summaries),
    )
    assert detect_tier_misrouting(ctx) == []


# ─── Resolver: cost.maintenance_high_tier_share ─────────────────────────────


def test_cost_maintenance_high_tier_share_registered():
    assert "cost.maintenance_high_tier_share" in known()


def test_cost_maintenance_high_tier_resolver_matches_disk_state(tmp_path):
    cm_cost.set_shared_dir(tmp_path)
    try:
        sessions = [f"sess-{i}" for i in range(4)]
        # Write session_summary records (one per session) and cost
        # events, all under shared_dir/annotations/{bot}.
        import json

        ann_dir = tmp_path / "annotations" / "team_bot_a"
        ann_dir.mkdir(parents=True, exist_ok=True)

        days = _spread_days(4)
        for i, ts in enumerate(days):
            date_iso = ts[:10]
            # session_summary records go in {date}.jsonl (regular type-tagged stream)
            sum_path = ann_dir / f"{date_iso}.jsonl"
            with sum_path.open("w") as f:
                f.write(json.dumps({
                    "schema_version": 1,
                    "type": "session_summary",
                    "ts": ts,
                    "bot_id": "team_bot_a",
                    "session_id": sessions[i],
                    "session_class": "maintenance",
                    "turn_count": 2,
                }) + "\n")
            # cost events go in cost_events-{date}.jsonl
            cost_path = ann_dir / f"cost_events-{date_iso}.jsonl"
            with cost_path.open("w") as f:
                # 0.30 on Sonnet (high) + 0.10 on Haiku (low) per day,
                # all on the maintenance session.
                f.write(json.dumps(_evt(
                    ts=ts, session_id=sessions[i], cost=0.30,
                    model="claude-sonnet-4-6"
                )) + "\n")
                f.write(json.dumps(_evt(
                    ts=ts, session_id=sessions[i], cost=0.10,
                    model="claude-haiku-4-5"
                )) + "\n")

        as_of = datetime.fromisoformat("2026-05-04").replace(tzinfo=timezone.utc) + timedelta(days=1)
        v = resolve("cost.maintenance_high_tier_share", "team_bot_a", as_of)
        # high = 4 × 0.30 = 1.20, low = 4 × 0.10 = 0.40
        # share = 1.20 / 1.60 = 0.75
        assert abs(v.value - 0.75) < 1e-6
        assert v.confidence == 1.0
    finally:
        cm_cost.set_shared_dir(Path("/Users/Shared/evolve"))
