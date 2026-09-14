"""tests/test_proposal_estimated_savings.py — PR H schema + ranking coverage.

Covers the ``estimated_savings_usd`` Proposal field added by PR H:

  - Schema field present round-trips through to_dict / from_dict.
  - Schema field absent (legacy proposals on disk) loads as None.
  - Schema field None round-trips intact.
  - Ranking includes a savings_bonus that scales with the field, is
    capped, and never pushes an improvement-tier above security_critical
    on its own.
  - Watchlist-style ordering: a $5/wk Proposal beats a fresh null-savings
    Proposal at the same urgency.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.ranking import (  # noqa: E402
    SAVINGS_BONUS_CAP,
    SAVINGS_BONUS_PER_DOLLAR,
    score_proposal,
)
from schema.proposal import Proposal  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ── Schema field round-trip ──────────────────────────────────────────────────


def test_estimated_savings_present_round_trips():
    p = make_investigation_proposal()
    p.estimated_savings_usd = 5.0
    raw = p.to_dict()
    assert raw["estimated_savings_usd"] == 5.0
    p2 = Proposal.from_dict(raw)
    assert p2.estimated_savings_usd == 5.0


def test_estimated_savings_none_round_trips():
    p = make_investigation_proposal()
    # Default is None — most generators leave it that way.
    assert p.estimated_savings_usd is None
    raw = p.to_dict()
    assert raw["estimated_savings_usd"] is None
    p2 = Proposal.from_dict(raw)
    assert p2.estimated_savings_usd is None


def test_legacy_proposal_without_field_loads_as_none():
    """Backward-compat: old Proposals on disk omit the field entirely.

    The arbiter's pending/ subdir contains proposals written before PR H
    shipped — they don't have ``estimated_savings_usd`` at all. The
    schema must still load them, treating the missing key as None.
    """
    p = make_investigation_proposal()
    raw = p.to_dict()
    raw.pop("estimated_savings_usd")  # simulate pre-PR-H on-disk shape
    p2 = Proposal.from_dict(raw)
    assert p2.estimated_savings_usd is None


def test_estimated_savings_float_coerced_from_int():
    """JSON sometimes loses the float type for whole-dollar estimates."""
    p = make_investigation_proposal()
    raw = p.to_dict()
    raw["estimated_savings_usd"] = 5  # int from JSON load
    p2 = Proposal.from_dict(raw)
    assert isinstance(p2.estimated_savings_usd, float)
    assert p2.estimated_savings_usd == 5.0


# ── Ranking: savings_bonus ────────────────────────────────────────────────────


def test_savings_bonus_appears_in_breakdown():
    p = make_investigation_proposal(urgency="improvement")
    p.estimated_savings_usd = 5.0
    bd = score_proposal(p, authority=1.0, now=_NOW)
    assert bd.savings_bonus == 5.0 * SAVINGS_BONUS_PER_DOLLAR


def test_savings_bonus_is_zero_when_none():
    p = make_investigation_proposal(urgency="improvement")
    p.estimated_savings_usd = None
    bd = score_proposal(p, authority=1.0, now=_NOW)
    assert bd.savings_bonus == 0.0


def test_savings_bonus_is_zero_when_non_positive():
    p = make_investigation_proposal(urgency="improvement")
    p.estimated_savings_usd = 0.0
    bd = score_proposal(p, authority=1.0, now=_NOW)
    assert bd.savings_bonus == 0.0
    p.estimated_savings_usd = -3.0
    bd = score_proposal(p, authority=1.0, now=_NOW)
    assert bd.savings_bonus == 0.0


def test_savings_bonus_is_capped():
    """A runaway estimate must not silently outrank urgency tiers above."""
    p = make_investigation_proposal(urgency="improvement")
    p.estimated_savings_usd = 10_000.0  # absurdly large
    bd = score_proposal(p, authority=1.0, now=_NOW)
    assert bd.savings_bonus == SAVINGS_BONUS_CAP


def test_score_breakdown_to_dict_includes_savings_bonus():
    p = make_investigation_proposal(urgency="improvement")
    p.estimated_savings_usd = 5.0
    bd = score_proposal(p, authority=1.0, now=_NOW)
    d = bd.to_dict()
    assert "savings_bonus" in d
    assert d["savings_bonus"] == bd.savings_bonus


# ── Ordering: the team-bot-a $7.67 motivating story ───────────────────────────


def test_proposal_with_savings_beats_recent_null_savings_at_same_urgency():
    """A $5/wk improvement Proposal must outrank a fresh null one.

    This is the test that locks in the motivating story: the operator
    should see "flip team-bot-a's cacheRetention" at the top, not buried
    by a more recent improvement-tier alert with no savings claim.
    """
    with_savings = make_investigation_proposal(urgency="improvement")
    with_savings.estimated_savings_usd = 5.0
    null_savings = make_investigation_proposal(urgency="improvement")
    # Same created_at default → tiebreak is identical → savings_bonus
    # decides.
    s1 = score_proposal(with_savings, authority=1.0, now=_NOW)
    s2 = score_proposal(null_savings, authority=1.0, now=_NOW)
    assert s1.score > s2.score


def test_security_critical_still_outranks_high_savings_improvement():
    """Savings must never silently overtake security_critical."""
    sec = make_investigation_proposal(urgency="security_critical")
    cheap_savings = make_investigation_proposal(urgency="improvement")
    cheap_savings.estimated_savings_usd = 10_000.0  # saturates the cap
    s_sec = score_proposal(sec, authority=1.0, now=_NOW)
    s_cheap = score_proposal(cheap_savings, authority=1.0, now=_NOW)
    assert s_sec.score > s_cheap.score


def test_operational_urgent_still_outranks_capped_savings_improvement():
    """Same guard for operational_urgent."""
    op = make_investigation_proposal(urgency="operational_urgent")
    savings = make_investigation_proposal(urgency="improvement")
    savings.estimated_savings_usd = 10_000.0
    s_op = score_proposal(op, authority=1.0, now=_NOW)
    s_savings = score_proposal(savings, authority=1.0, now=_NOW)
    assert s_op.score > s_savings.score


def test_cost_alert_still_outranks_capped_savings_improvement():
    """A cost_alert urgency outranks even a saturated-savings improvement.

    The cap was sized so that improvement + cap stays strictly below the
    cost_alert urgency score — operators promote tiers via the generator
    catalog, never via dollar amounts.
    """
    cost = make_investigation_proposal(urgency="cost_alert")
    savings = make_investigation_proposal(urgency="improvement")
    savings.estimated_savings_usd = 10_000.0
    s_cost = score_proposal(cost, authority=1.0, now=_NOW)
    s_savings = score_proposal(savings, authority=1.0, now=_NOW)
    assert s_cost.score > s_savings.score
