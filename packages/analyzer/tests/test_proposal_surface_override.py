"""tests/test_proposal_surface_override.py — pin the per-finding surface
override field on the Proposal schema (RSI eligibility, 2026-06-05).

Spec: internal/spec-rsi-proposal-eligibility-2026-06-05.md.

The override lets a generator emit findings of different shape — some
RSI (route to Recommendations) and some anomaly (route to Alerts) — from
the same module. The schema piece: ``Proposal.surface`` is an optional
field that wins over ``charter.surface`` at routing time.

These tests pin:
  1. ``Proposal.surface`` defaults to None (backward-compat — every
     pre-spec proposal loads with no value and routes by charter).
  2. The field round-trips through to_dict/from_dict.
  3. Each of the four legal Surface values is acceptable.
  4. The serialized payload includes ``surface`` as a key even when
     None, so the admin server can branch on its presence.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from schema.proposal import (  # noqa: E402
    AgentsAppend,
    Proposal,
    RiskTag,
)
from schema.provenance import Provenance  # noqa: E402


def _new_proposal(**overrides) -> Proposal:
    """Build a minimal valid Proposal with the override fields applied."""
    base = dict(
        id="p-1",
        bot_id="team-bot-a",
        generator_id="efficiency_hawk",
        dimension="efficiency",
        trigger_observations=["t"],
        provenance=Provenance(technique="x"),
        problem="problem",
        action=AgentsAppend(bot_id="team-bot-a", section="X", content="y"),
        risk_tag=RiskTag(blast_radius="bot", reversibility="auto", touches=[]),
    )
    base.update(overrides)
    return Proposal(**base)


def test_surface_defaults_to_none():
    """Pre-2026-06-05 proposals didn't have a surface field; new
    proposals default to None so the charter wins (today's behavior)."""
    p = _new_proposal()
    assert p.surface is None, (
        "Proposal.surface must default to None — the override is "
        "opt-in. Pre-spec proposals route by charter.surface."
    )


def test_surface_round_trips_via_to_dict():
    """A proposal with surface=firing must round-trip through
    serialization. Without this, the disk-stored proposal forgets the
    override and the next page render routes it back to the charter
    default — i.e. the screenshot bug returns."""
    p = _new_proposal(surface="firing")
    assert p.to_dict()["surface"] == "firing"
    reloaded = Proposal.from_dict(p.to_dict())
    assert reloaded.surface == "firing", (
        "Surface override lost in to_dict→from_dict round-trip"
    )


@pytest.mark.parametrize(
    "surface", ["improvement", "firing", "drift", "cleanup"]
)
def test_all_four_surface_values_round_trip(surface):
    """Each of the four legal Surface literal values must round-trip.
    This guards against a future schema change that narrows the type
    silently (e.g. dropping 'cleanup')."""
    p = _new_proposal(surface=surface)
    reloaded = Proposal.from_dict(p.to_dict())
    assert reloaded.surface == surface


def test_surface_key_present_when_none():
    """to_dict must always include the ``surface`` key, even when
    None. The admin server reads p.surface unconditionally; a missing
    key would force a defensive null check the renderer doesn't have."""
    payload = _new_proposal().to_dict()
    assert "surface" in payload
    assert payload["surface"] is None


def test_unknown_surface_in_payload_loads_through():
    """from_dict should tolerate a future-added surface value without
    raising — the schema validator at the registry level enforces
    legal values; from_dict is permissive so an upgrade in flight (new
    surface enum, old reader) doesn't drop proposals on the floor."""
    # The Literal type isn't enforced at runtime by dataclasses, so
    # an unrecognized string passes through. This documents the
    # intentional non-strict behavior — if we ever want strict
    # validation, this test fails and the choice gets reconsidered.
    payload = _new_proposal().to_dict()
    payload["surface"] = "future_new_value"
    reloaded = Proposal.from_dict(payload)
    assert reloaded.surface == "future_new_value"
