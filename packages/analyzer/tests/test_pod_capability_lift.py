"""tests/test_pod_capability_lift.py — pin the cross-bot
Signal-aggregating generator.

Spec: internal/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".

The fourth Phase 2 piece. Reads firing app_suggester_gap and
engagement_amplification_opportunity Signals across bots; emits ONE
pod-wide Investigation Proposal per (category) or (noun, verb) group
that hits the cross-bot threshold.

These tests pin:
  1. Threshold gate: < MIN_BOTS_FOR_LIFT bots → no proposal.
  2. Gap aggregation: 3 bots with the same category → one pod-wide
     proposal naming all three.
  3. Amplification aggregation: 3 bots with the same (noun, verb) →
     one pod-wide proposal naming all three.
  4. Phase A operator-first content: summary cites bot list, count,
     and category/pattern name.
  5. Determinism: same Signal-store state → identical bot ordering
     and proposal selection.
  6. Per-key dismiss signature: pod-wide fitness gap dismiss doesn't
     suppress pod-wide finance gap (different keys).
  7. End-to-end coexistence: pod-wide proposal fires alongside the
     per-bot proposals; neither suppresses the other.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.pod_capability_lift import (  # noqa: E402
    PodCapabilityLiftContext,
    observe,
)
from generators.pod_capability_lift.observe import (  # noqa: E402
    MIN_BOTS_FOR_LIFT,
    _group_amps,
    _group_gaps,
)
from signals import store as signals_store  # noqa: E402


BOT_A = "team-bot-a"
BOT_B = "team-bot-b"
BOT_C = "team-bot-c"
ALL_BOTS = [BOT_A, BOT_B, BOT_C]


def _drop_gap(
    shared_dir: Path, bot_id: str, category: str, **details
) -> None:
    signals_store.observe(
        shared_dir,
        signature=f"app_suggester_gap:{bot_id}:{category}",
        producer="capability_gap_monitor",
        type="app_suggester_gap",
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id=bot_id,
        title=f"Gap: {category} for {bot_id}",
        body="(test fixture)",
        details={
            "category": category,
            "bot_id": bot_id,
            "domain_tag": "domain:fitness",
            "example_nouns": ["workout", "fitness"],
            "distinct_sessions": 6,
            "distinct_days": 8,
            "engagement_total": 24,
            **details,
        },
    )


def _drop_amp(
    shared_dir: Path,
    bot_id: str,
    noun: str,
    verb: str,
    alignment: str = "confirmed",
    engagement: int = 30,
    **details,
) -> None:
    signals_store.observe(
        shared_dir,
        signature=f"engagement_amplification:{bot_id}:{noun}:{verb}",
        producer="engagement_amplifier_monitor",
        type="engagement_amplification_opportunity",
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id=bot_id,
        title=f"Amplifiable: ({noun}, {verb}) on {bot_id}",
        body="(test fixture)",
        details={
            "bot_id": bot_id,
            "noun": noun,
            "verb": verb,
            "objective_alignment": alignment,
            "engagement_total": engagement,
            "distinct_sessions": 6,
            "distinct_days": 8,
            "frustrated_share": 0.05,
            "positive_mood_share": 0.6,
            "domain_tags": ["domain:fitness"],
            "window_days": 30,
            **details,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Threshold gate
# ─────────────────────────────────────────────────────────────────────────────


def test_threshold_below_min_bots_suppresses_gap_proposal(tmp_path):
    """Only 2 bots with the same gap → no pod-wide proposal. Default
    threshold is MIN_BOTS_FOR_LIFT = 3, which is the smallest defensible
    "this is real cross-bot convergence, not coincidence" number."""
    _drop_gap(tmp_path, BOT_A, "fitness_tracking")
    _drop_gap(tmp_path, BOT_B, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    assert observe(ctx) == []


def test_threshold_at_min_bots_fires_gap_proposal(tmp_path):
    """Exactly MIN_BOTS_FOR_LIFT bots → fire."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    proposals = observe(ctx)
    assert len(proposals) == 1
    assert proposals[0].generator_id == "pod_capability_lift"


def test_threshold_below_min_bots_suppresses_amp_proposal(tmp_path):
    """Same threshold logic for amplification opportunities."""
    _drop_amp(tmp_path, BOT_A, "workout", "tracking")
    _drop_amp(tmp_path, BOT_B, "workout", "tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    assert observe(ctx) == []


def test_custom_min_bots_override(tmp_path):
    """Operator override: ctx.min_bots=2 lowers the threshold so
    smaller pods (or dev pods) can exercise the path."""
    _drop_gap(tmp_path, BOT_A, "fitness_tracking")
    _drop_gap(tmp_path, BOT_B, "fitness_tracking")
    ctx = PodCapabilityLiftContext(
        bot_ids=ALL_BOTS, shared_dir=tmp_path, min_bots=2
    )
    proposals = observe(ctx)
    assert len(proposals) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Gap aggregation
# ─────────────────────────────────────────────────────────────────────────────


def test_gap_proposal_names_all_bots_in_summary(tmp_path):
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    for bot in ALL_BOTS:
        assert bot in p.summary, f"{bot} missing from summary {p.summary!r}"
    assert "3" in p.summary  # bot count


def test_gap_proposal_carries_catalog_title_when_available(tmp_path):
    """When the catalog entry exists for the category, the proposal
    quotes its operator-friendly title in headline + summary."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    # The fitness_tracking catalog entry's title contains "workout"
    # (current value: "Workout and activity log"). Asserting on the
    # substring keeps the test robust to catalog title polish without
    # losing the contract: when the catalog has a title, the proposal
    # uses it (not the bare category slug "fitness_tracking").
    assert "workout" in p.admin_surface_summary.lower(), (
        f"Expected catalog title in headline; got {p.admin_surface_summary!r}"
    )
    assert "fitness_tracking" not in p.admin_surface_summary, (
        "Headline must use the catalog title, not the raw category slug"
    )


def test_gap_proposal_provenance_records_bot_ids_and_grounding_ids(tmp_path):
    """The audit trail must record every bot the proposal speaks for
    plus the Signal IDs it aggregated, so a later reviewer can answer
    'what did the system see when it emitted this?'."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    ps = p.provenance.signals
    assert set(ps["bot_ids"]) == set(ALL_BOTS)
    assert ps["n_bots"] == 3
    assert ps["category"] == "fitness_tracking"
    assert len(ps["grounding_signal_ids"]) >= 3


def test_gap_proposal_blast_radius_is_pod(tmp_path):
    """A pod-wide proposal must declare blast_radius=pod so the
    arbiter routes it through pod-scope review rather than per-bot
    review. Differentiates the pod-wide pitch from the per-bot ones."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert p.risk_tag.blast_radius == "pod"


# ─────────────────────────────────────────────────────────────────────────────
# Amplification aggregation
# ─────────────────────────────────────────────────────────────────────────────


def test_amp_proposal_fires_for_cross_bot_pattern(tmp_path):
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking", alignment="confirmed")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    proposals = observe(ctx)
    assert len(proposals) == 1
    assert "workout" in proposals[0].summary
    assert "tracks" in proposals[0].summary  # friendly verb conjugation


def test_amp_alignment_mix_drives_framing(tmp_path):
    """When confirmed >= emergent → 'stated-scope working pattern'
    framing. When emergent > confirmed → 'organic cross-bot
    convergence' framing. The Why-this-matters paragraph differs."""
    # All emergent.
    for bot in ALL_BOTS:
        _drop_amp(
            tmp_path, bot, "workout", "tracking", alignment="emergent"
        )
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "organic" in p.summary, (
        f"emergent-majority pattern must read as organic; got {p.summary!r}"
    )
    assert "organically converged" in p.explanation


def test_amp_alignment_mix_counts_in_explanation(tmp_path):
    _drop_amp(tmp_path, BOT_A, "workout", "tracking", alignment="confirmed")
    _drop_amp(tmp_path, BOT_B, "workout", "tracking", alignment="confirmed")
    _drop_amp(tmp_path, BOT_C, "workout", "tracking", alignment="emergent")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    # The explanation must surface the alignment mix.
    assert "2 confirmed" in p.explanation
    assert "1 emergent" in p.explanation


def test_amp_proposal_lists_concrete_rollout_options(tmp_path):
    """The 'Ways to roll this out' section must surface 3+ concrete
    options so 'Pick a pod-wide rollout path' is meaningful."""
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "shared app" in p.explanation
    assert "scheduled surface" in p.explanation.lower()
    assert "Dismiss" in p.explanation


# ─────────────────────────────────────────────────────────────────────────────
# Dismiss signature granularity
# ─────────────────────────────────────────────────────────────────────────────


def test_dismiss_signatures_are_per_key(tmp_path):
    """Dismissing the pod-wide fitness_tracking gap must NOT suppress
    a separately-firing pod-wide personal_finance gap. Per-key
    granularity preserves the operator's other decisions."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
        _drop_gap(tmp_path, bot, "personal_finance")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    proposals = observe(ctx)
    assert len(proposals) == 2
    sigs = {p.dismiss_signature for p in proposals}
    assert "pod_capability_lift:gap:fitness_tracking" in sigs
    assert "pod_capability_lift:gap:personal_finance" in sigs


def test_dismiss_signatures_distinguish_gap_vs_amp(tmp_path):
    """Even with the same noun (e.g. workout), a gap and an
    amplification must NOT share a dismiss signature. They're
    different proposal shapes."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
        _drop_amp(tmp_path, bot, "workout", "tracking")
    ctx = PodCapabilityLiftContext(
        bot_ids=ALL_BOTS, shared_dir=tmp_path, max_per_run=5
    )
    proposals = observe(ctx)
    sigs = [p.dismiss_signature for p in proposals]
    assert any(s.startswith("pod_capability_lift:gap:") for s in sigs)
    assert any(s.startswith("pod_capability_lift:amp:") for s in sigs)


# ─────────────────────────────────────────────────────────────────────────────
# Determinism + bot filter
# ─────────────────────────────────────────────────────────────────────────────


def test_repeat_run_produces_same_proposals(tmp_path):
    """Same Signal-store state must produce identical proposal
    structure across runs — same bot ordering, same dismiss signature,
    same summary text. Stable output makes lineage tracking work."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p1 = observe(ctx)[0]
    p2 = observe(ctx)[0]
    assert p1.summary == p2.summary
    assert p1.dismiss_signature == p2.dismiss_signature
    assert p1.provenance.signals["bot_ids"] == (
        p2.provenance.signals["bot_ids"]
    )


def test_bot_filter_excludes_retired_bot_signals(tmp_path):
    """A Signal from a bot not in ctx.bot_ids (retired bot whose
    signals haven't been swept yet) doesn't count toward the lift
    threshold. Without this guard a deprecated bot's stale signals
    would inflate the cross-bot count."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    # Also drop a Signal for a bot the pod no longer includes.
    _drop_gap(tmp_path, "retired-bot", "fitness_tracking")
    # ctx scoped to just the three current bots.
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "retired-bot" not in p.summary
    assert p.provenance.signals["n_bots"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# max_per_run cap
# ─────────────────────────────────────────────────────────────────────────────


def test_max_per_run_caps_proposal_count(tmp_path):
    """When many groups exceed threshold, max_per_run limits the
    queue. Prevents flooding the operator if the pod has converged
    across many categories at once."""
    for category in (
        "fitness_tracking",
        "personal_finance",
        "learning_log",
        "health_tracking",
    ):
        for bot in ALL_BOTS:
            _drop_gap(tmp_path, bot, category)
    ctx = PodCapabilityLiftContext(
        bot_ids=ALL_BOTS, shared_dir=tmp_path, max_per_run=2
    )
    proposals = observe(ctx)
    assert len(proposals) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Coexistence
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_lift_proposal_explains_per_bot_relationship(tmp_path):
    """The proposal narrative must explicitly tell the operator that
    per-bot proposals from app_suggester / engagement_amplifier
    continue independently. Without this note, an operator might
    assume dismissing the pod-wide one cleans up the per-bot ones."""
    for bot in ALL_BOTS:
        _drop_gap(tmp_path, bot, "fitness_tracking")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    # The "_Note: ..._" disclaimer must be present.
    assert "Dismissing this one doesn't suppress the per-bot" in p.explanation
