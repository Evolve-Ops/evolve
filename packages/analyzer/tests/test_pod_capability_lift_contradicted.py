"""tests/test_pod_capability_lift_contradicted.py — pin the
contradicted-alignment handling in pod_capability_lift.

Spec: internal/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".

Anti-domain detection (PR #2182) added a third alignment value
(``contradicted``) emitted by the engagement_amplifier_monitor when
users keep engaging on a domain the bot's AGENTS.md explicitly marks
as out of scope. This test file pins the cross-bot synthesis layer's
handling of that value:

  - alignment_mix counts contradicted alongside confirmed + emergent
  - contradicted-majority drives a distinct "make a pod-wide scope
    decision" framing
  - mixed groups (some confirmed, some contradicted) keep readable
    framing and surface the contradicted count when non-zero
  - The action_label + problem field reflect the decision shape
  - The "Ways to..." section lists scope-decision options for
    contradicted-majority and rollout options otherwise

Coexists with the existing test_pod_capability_lift.py — that file
pins the confirmed/emergent paths; this one focuses on contradicted.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.pod_capability_lift import (  # noqa: E402
    PodCapabilityLiftContext,
    observe,
)
from signals import store as signals_store  # noqa: E402


BOT_A = "team-bot-a"
BOT_B = "team-bot-b"
BOT_C = "team-bot-c"
ALL_BOTS = [BOT_A, BOT_B, BOT_C]


def _drop_amp(
    shared_dir: Path,
    bot_id: str,
    noun: str,
    verb: str,
    alignment: str,
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
# Counter includes contradicted
# ─────────────────────────────────────────────────────────────────────────────


def test_alignment_mix_counts_contradicted(tmp_path):
    """All three bots flag the pattern contradicted; the proposal's
    provenance.signals.alignment_mix must reflect that."""
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert p.provenance.signals["alignment_mix"] == {"contradicted": 3}


def test_alignment_mix_records_mixed_states(tmp_path):
    _drop_amp(tmp_path, BOT_A, "workout", "tracking", "confirmed")
    _drop_amp(tmp_path, BOT_B, "workout", "tracking", "emergent")
    _drop_amp(tmp_path, BOT_C, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    mix = p.provenance.signals["alignment_mix"]
    assert mix.get("confirmed") == 1
    assert mix.get("emergent") == 1
    assert mix.get("contradicted") == 1


# ─────────────────────────────────────────────────────────────────────────────
# Contradicted-majority framing
# ─────────────────────────────────────────────────────────────────────────────


def test_contradicted_majority_summary_reframes_as_decision(tmp_path):
    """All three bots flag contradicted → summary must lead with
    'pod-wide scope contradiction' / 'make a pod-wide scope decision',
    not the build / deepen framings."""
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "scope contradiction" in p.summary
    assert "scope decision" in p.summary.lower()
    # Build / deepen framing must NOT leak through.
    assert "shared app or pod-wide workflow" not in p.summary


def test_contradicted_majority_explanation_lists_decision_options(tmp_path):
    """The 'Ways to...' section in a contradicted-majority pod proposal
    must list scope-decision options (widen across pod, pod-wide
    redirect, per-bot only), not rollout options (build app, scheduled
    surface)."""
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "Widen scope across the pod" in p.explanation
    assert "Pod-wide redirect" in p.explanation
    # Rollout-shaped options must NOT leak.
    assert "Build a shared app" not in p.explanation
    assert "Pod-wide scheduled surface" not in p.explanation


def test_contradicted_majority_action_label_is_scope_decision(tmp_path):
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert p.action_label == "Make a pod-wide scope decision"


def test_contradicted_majority_why_paragraph_explains_fragmentation(tmp_path):
    """The 'Why this matters' paragraph for contradicted-majority must
    name the user-experience problem (fragmentation: bot sometimes
    answers, sometimes deflects). Without that, the operator might
    interpret the proposal as a config nit."""
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "out of scope" in p.explanation.lower()
    assert "fragmentation" in p.explanation.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Mixed states — tie-break behavior
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmed_beats_contradicted_on_tie(tmp_path):
    """When confirmed and contradicted tie (no majority), the pitch
    leans confirmed (build / deepen framing). Tie-break order:
    confirmed > contradicted > emergent — the most-actionable framing
    wins when the mix is split."""
    _drop_amp(tmp_path, BOT_A, "workout", "tracking", "confirmed")
    _drop_amp(tmp_path, BOT_B, "workout", "tracking", "confirmed")
    _drop_amp(tmp_path, BOT_C, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    # 2 confirmed > 1 contradicted → confirmed framing wins.
    assert "stated-scope working pattern" in p.summary
    assert "scope decision" not in p.summary.lower()


def test_contradicted_majority_overrides_confirmed_when_actual_majority(
    tmp_path,
):
    """When contradicted is strictly the most common, it must drive
    the framing — even with one confirmed in the mix."""
    _drop_amp(tmp_path, BOT_A, "workout", "tracking", "confirmed")
    _drop_amp(tmp_path, BOT_B, "workout", "tracking", "contradicted")
    _drop_amp(tmp_path, BOT_C, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "scope contradiction" in p.summary
    assert p.action_label == "Make a pod-wide scope decision"


# ─────────────────────────────────────────────────────────────────────────────
# Alignment-mix display
# ─────────────────────────────────────────────────────────────────────────────


def test_alignment_mix_line_suppresses_contradicted_count_when_zero(
    tmp_path,
):
    """When no bots flag contradicted, the alignment-mix line must NOT
    surface a '0 contradicted' chunk — keeps the common case readable.
    """
    for bot in ALL_BOTS:
        _drop_amp(tmp_path, bot, "workout", "tracking", "confirmed")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "0 contradicted" not in p.explanation
    assert "Objective alignment mix" in p.explanation
    assert "3 confirmed" in p.explanation


def test_alignment_mix_line_surfaces_contradicted_when_present(tmp_path):
    """When at least one bot flags contradicted, the mix line must
    include the count + the contradicted phrasing for the audit
    trail."""
    _drop_amp(tmp_path, BOT_A, "workout", "tracking", "confirmed")
    _drop_amp(tmp_path, BOT_B, "workout", "tracking", "confirmed")
    _drop_amp(tmp_path, BOT_C, "workout", "tracking", "contradicted")
    ctx = PodCapabilityLiftContext(bot_ids=ALL_BOTS, shared_dir=tmp_path)
    p = observe(ctx)[0]
    assert "1 contradicted" in p.explanation
    assert "out of scope" in p.explanation
