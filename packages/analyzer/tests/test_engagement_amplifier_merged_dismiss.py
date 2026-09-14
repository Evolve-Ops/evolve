"""tests/test_engagement_amplifier_merged_dismiss.py — pin the P2 fix
that merges the redundant Tolerate/Dismiss options in the
contradicted-alignment branch.

Bug (from the 2026-06-05 audit of PR #2198): the contradicted-
alignment proposal listed both:

  - "Tolerate as-is: dismiss this proposal. The exclusion stays..."
  - "Dismiss if the pattern is a passing fad..."

Functionally identical — both dismiss the proposal. The split was
confusing UX (operator wondering "what's the difference?"). Fix
merges into one item that covers both intents.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.engagement_amplifier import EngagementAmplifierContext  # noqa: E402
from generators.engagement_amplifier.observe import (  # noqa: E402
    _build_proposal as amp_build_proposal,
)


BOT_ID = "team-bot-a"


class _StubSignal:
    def __init__(self, **details):
        self.id = "sig-test"
        self.bot_id = BOT_ID
        self.type = "engagement_amplification_opportunity"
        self.details = {"bot_id": BOT_ID, **details}


def _contradicted_signal(**overrides):
    base = dict(
        noun="workout",
        verb="tracking",
        objective_alignment="contradicted",
        engagement_total=42,
        distinct_sessions=8,
        distinct_days=12,
        frustrated_share=0.05,
        positive_mood_share=0.6,
        domain_tags=["domain:fitness"],
        window_days=30,
    )
    base.update(overrides)
    return _StubSignal(**base)


def test_contradicted_branch_lists_three_options_not_four():
    """The contradicted branch should have 3 distinct decision
    options (Widen / Redirect / Dismiss-tolerate), not 4. The
    redundant separate Tolerate + Dismiss got merged."""
    p = amp_build_proposal(
        _contradicted_signal(),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    # Count distinct bullet items starting with "- **" in the
    # "Ways to make this decision" section.
    lines = p.explanation.splitlines()
    decision_section_start = next(
        (i for i, line in enumerate(lines) if "Ways to make this decision" in line),
        None,
    )
    assert decision_section_start is not None, (
        "contradicted branch missing 'Ways to make this decision' section"
    )
    # Count items below the header (until end of section).
    items_after_header = [
        line for line in lines[decision_section_start + 1:]
        if line.startswith("- **")
    ]
    assert len(items_after_header) == 3, (
        f"Expected exactly 3 decision options (Widen / Redirect / "
        f"Dismiss-tolerate). Got {len(items_after_header)}: "
        f"{[line[:60] for line in items_after_header]}"
    )


def test_dismiss_tolerate_item_covers_both_intents():
    """The merged item should explicitly name both the 'passing fad'
    case (dismiss intent) AND the 'accept fragmentation' case
    (tolerate intent) so the operator sees the option applies to
    both situations."""
    p = amp_build_proposal(
        _contradicted_signal(),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    e = p.explanation
    # The merged option's header
    assert "Dismiss / tolerate as-is" in e
    # Both intents named
    assert "passing fad" in e
    assert "fragmentation" in e or "current behavior is acceptable" in e


def test_widen_and_redirect_unchanged():
    """The other two options aren't affected by this change."""
    p = amp_build_proposal(
        _contradicted_signal(),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert "Widen the scope" in p.explanation
    assert "Redirect users" in p.explanation


def test_confirmed_branch_options_untouched():
    """The non-contradicted branches have a single Dismiss item which
    isn't affected by this fix. Regression guard."""
    p = amp_build_proposal(
        _StubSignal(
            noun="workout",
            verb="tracking",
            objective_alignment="confirmed",
            engagement_total=42,
            distinct_sessions=8,
            distinct_days=12,
            frustrated_share=0.05,
            positive_mood_share=0.6,
            domain_tags=["domain:fitness"],
            window_days=30,
        ),
        EngagementAmplifierContext(bot_ids=[BOT_ID], shared_dir=Path("/tmp")),
    )
    assert p is not None
    assert "Schedule a proactive surface" in p.explanation
    assert "Formalize as an app" in p.explanation
    # Single Dismiss option, unchanged shape.
    assert "Dismiss" in p.explanation
