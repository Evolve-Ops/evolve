"""tests/test_oc_model_fallback_no_escalation.py — fallback degrades, never escalates.

Regression lock for the 2026-07-31 cost runaway. A personal-bot on the
reference pod walked its full fallback chain on a long-report turn:

    sonnet-4-6  → stopReason=length   (output ceiling, not a model defect)
    gemini-3.1-pro-preview → HTTP 401 (provider auth misrouted)
    haiku-4-5   → stopReason=length   (same ceiling)
    gemini-3-flash-preview → HTTP 401
    opus-4-8    → SUCCEEDED — $11.32 for one turn

The terminal Opus turn was $11.32 of a $14.06 session (2.57M cache-read +
144k cache-write + 159k input + 31k output). The same turn on the Sonnet
primary would have been ~$2.26 — a 5x premium paid at the worst possible
moment, because every prior rung re-primed the cache from cold.

Neither triggering failure is one a costlier model fixes: an output
ceiling and a 401 are both `candidate_failed` in OC's fallback taxonomy.
So the tail must never contain a tier costlier than the one supplying
`primary`.

Locked here:
  1. The default workhorse-first cascade drops the high-cost tier tail.
  2. `primary` is UNCHANGED — this is not the PR #1765 floor-first revert.
  3. An explicit power-first cascade keeps the full chain (operator wins).
  4. Empty / fully-duplicate leading tiers don't pin the ceiling.
  5. The real reference-pod tier file produces an Opus-free chain end to end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


# The tier shape every pod bot resolves to, matching the rungs/roles file
# deployed fleet-wide as of 2026-07-31.
_FLEET_TIERS = {
    "tier3": {"models": ["anthropic/claude-haiku-4-5", "google/gemini-3-flash-preview"]},
    "tier2": {"models": ["anthropic/claude-sonnet-4-6", "google/gemini-3.1-pro-preview"]},
    "tier1": {"models": ["anthropic/claude-opus-4-8", "google/gemini-3.1-pro-preview"]},
}


def test_default_cascade_drops_costlier_tail():
    """The workhorse-first default must not terminate in the power tier.

    This is the exact chain the reference pod's personal-bot walked to
    reach a $11.32 turn.
    """
    flat = oc_model.generate_fallback_list(_FLEET_TIERS, oc_model.DEFAULT_TIER_CASCADE)

    assert flat == [
        "anthropic/claude-sonnet-4-6",
        "google/gemini-3.1-pro-preview",
        "anthropic/claude-haiku-4-5",
        "google/gemini-3-flash-preview",
    ]
    assert not any("opus" in m for m in flat), (
        f"power-tier model reachable by fallback from a medium primary: {flat}"
    )


def test_primary_is_unchanged_by_the_filter():
    """Guard the PR #1765 lesson: this changes the TAIL, never the primary.

    PR #1765 made the default cascade role-aware, silently demoting
    human-facing chat on member bots to Haiku with no in-channel escalation
    path, and was reverted. A tail filter that moved `primary` would
    reintroduce exactly that regression.
    """
    flat = oc_model.generate_fallback_list(_FLEET_TIERS, oc_model.DEFAULT_TIER_CASCADE)

    assert flat[0] == "anthropic/claude-sonnet-4-6"


def test_operator_power_first_cascade_keeps_full_chain():
    """The ceiling is the PRIMARY's tier, not a hardcoded rung.

    A bot the operator explicitly pointed at tier1 still falls all the way
    down — nothing outranks a "high" primary.
    """
    flat = oc_model.generate_fallback_list(_FLEET_TIERS, ["tier1", "tier2", "tier3"])

    assert flat[0] == "anthropic/claude-opus-4-8"
    assert "anthropic/claude-sonnet-4-6" in flat
    assert "anthropic/claude-haiku-4-5" in flat


def test_fast_primary_admits_no_costlier_rung():
    """A tier3 primary bounds the tail at "low" — neither sonnet nor opus."""
    flat = oc_model.generate_fallback_list(_FLEET_TIERS, ["tier3", "tier2", "tier1"])

    assert flat == [
        "anthropic/claude-haiku-4-5",
        "google/gemini-3-flash-preview",
    ]


def test_empty_leading_tier_does_not_pin_the_ceiling():
    """An empty leading tier contributes no primary, so it must not bound
    the tail below the model that actually lands at result[0]."""
    tiers = {
        "tier2": {"models": []},
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
        "tier1": {"models": ["anthropic/claude-opus-4-8"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier2", "tier3", "tier1"])

    # tier3 supplies primary (rank low) → tier1 (high) is correctly dropped.
    assert flat == ["anthropic/claude-haiku-4-5"]


def test_fully_duplicate_tier_does_not_pin_the_ceiling():
    """A tier whose models were all already emitted contributes nothing new
    and must be transparent to the ceiling calculation."""
    tiers = {
        "tier1": {"models": ["shared-model"]},
        "tier2": {"models": ["shared-model"]},   # nothing fresh
        "tier3": {"models": ["cheap-model"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier1", "tier2", "tier3"])

    # tier1 (high) is primary; tier2 adds nothing; tier3 (low) still admitted.
    assert flat == ["shared-model", "cheap-model"]


def test_tier0_judge_still_excluded():
    """Pre-existing contract: the judge tier never joins the cascade."""
    tiers = {
        "tier0": {"models": ["judge-model"]},
        "tier2": {"models": ["sonnet"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier0", "tier2"])

    assert "judge-model" not in flat
    assert flat == ["sonnet"]


def test_malformed_tier_entry_is_skipped():
    """A scalar where a tier dict is expected must not crash derivation —
    this runs on every deploy across every bot."""
    tiers = {
        "tier2": "not-a-dict",
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier2", "tier3"])

    assert flat == ["anthropic/claude-haiku-4-5"]


def test_real_pod_tiers_file_yields_opus_free_chain(tmp_path):
    """End-to-end through the deploy-time entrypoint, using the reference
    pod's actual on-disk evolve-tiers.json as captured on 2026-07-31."""
    tiers_path = tmp_path / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({
        "cascade": {"enabled": True},
        "rungs": [
            {"id": "haiku-class", "costClass": "low", "models": [
                "anthropic/claude-haiku-4-5", "google/gemini-3-flash-preview"]},
            {"id": "sonnet-class", "costClass": "medium", "models": [
                "anthropic/claude-sonnet-4-6", "google/gemini-3.1-pro-preview"]},
            {"id": "opus-class", "costClass": "high", "models": [
                "anthropic/claude-opus-4-8", "google/gemini-3.1-pro-preview"]},
        ],
        "roles": {
            "fast": "haiku-class",
            "standard": "sonnet-class",
            "power": "opus-class",
            "judge": {"rung": "sonnet-class", "provider": "not-standard"},
        },
    }))

    derived = oc_model.compute_primary_from_tiers_file(tiers_path, role="member")
    assert derived is not None
    primary, fallbacks = derived

    assert primary == "anthropic/claude-sonnet-4-6"
    assert "anthropic/claude-opus-4-8" not in fallbacks
    assert fallbacks == [
        "google/gemini-3.1-pro-preview",
        "anthropic/claude-haiku-4-5",
        "google/gemini-3-flash-preview",
    ]


@pytest.mark.parametrize("cascade", [
    oc_model.DEFAULT_TIER_CASCADE,
    ["tier2", "tier3"],
    ["tier3", "tier2", "tier1"],
])
def test_no_medium_or_low_primary_ever_reaches_the_power_tier(cascade):
    """Property form of the runaway: unless the operator makes tier1 the
    primary, opus must be unreachable by automatic failover."""
    flat = oc_model.generate_fallback_list(_FLEET_TIERS, cascade)

    assert flat, "cascade resolved to nothing"
    if flat[0] != "anthropic/claude-opus-4-8":
        assert "anthropic/claude-opus-4-8" not in flat
