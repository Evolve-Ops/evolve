"""tests/test_oc_model_fallback_provider_affinity.py — degrade in-provider first.

Regression lock for the 2026-08-31 PoC-bot incident. The sonnet-class rung
cluster doubles as the failover chain, and its order put every cross-provider
same-rung peer ahead of the within-provider rung below. So a single sonnet
API error failed the personal-assistant bot over to grok-4 MID-CONVERSATION,
where it re-issued the same gmail_send call ~16 times while claiming
tool-schema compliance in prose (below-LLM containment landed as #3907; this
is the routing-layer fix).

A cross-provider hop swaps the behavioral dialect the session was built on —
tool-schema strictness, sentinel/silence conventions (the 2026-08-14
team-bot-a NO_REPLY incident showed even an in-family swap breaks those) —
while a hop
DOWN the primary provider's own ladder degrades only capability, the axis the
house doctrine already accepts ("fallback must degrade, never escalate").

Locked here:
  1. The incident shape: sonnet primary's first fallback is the same
     provider's fast rung, not the cross-provider peer.
  2. `primary` NEVER moves (PR #1765 lesson, again).
  3. The affinity partition is stable — an operator's within-tier reorder
     survives as relative order on both sides of the partition.
  4. Affinity cannot resurrect a model the never-escalate ceiling dropped.
  5. A bare, un-prefixed primary skips the partition (affinity unknowable).
  6. A power-first operator cascade walks the primary provider's ladder
     top-to-bottom before any other provider.

Design: internal/design-failover-provider-affinity-2026-08-31.md
Spec:   internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 18
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


# The incident pod's live cluster shape as of 2026-08-31: sonnet-class had
# drifted (via freshness adopts) to lead with sonnet-5 and hold three
# cross-provider peers; the anthropic fast rung sat a whole tier later.
_INCIDENT_TIERS = {
    "tier2": {"models": [
        "anthropic/claude-sonnet-5",
        "xai/grok-4",
        "openai/gpt-5.5",
        "google/gemini-3.1-pro-preview",
    ]},
    "tier3": {"models": [
        "anthropic/claude-haiku-4-5",
        "openai/gpt-4.1-mini",
        "google/gemini-2.0-flash",
        "xai/grok-4-mini",
    ]},
    "tier1": {"models": ["anthropic/claude-opus-4-8"]},
}


def test_incident_shape_first_fallback_is_same_provider_not_peer():
    """The exact 2026-08-31 chain: one sonnet-5 error must land on haiku
    (same provider, rung below), not on grok-4 (cross-provider peer)."""
    flat = oc_model.generate_fallback_list(
        _INCIDENT_TIERS, oc_model.DEFAULT_TIER_CASCADE
    )

    assert flat[0] == "anthropic/claude-sonnet-5"
    assert flat[1] == "anthropic/claude-haiku-4-5", (
        f"first failover hop crossed providers: {flat[:3]}"
    )
    assert flat.index("anthropic/claude-haiku-4-5") < flat.index("xai/grok-4")


def test_primary_is_never_moved_by_affinity():
    """The partition reorders the TAIL only."""
    flat = oc_model.generate_fallback_list(
        _INCIDENT_TIERS, oc_model.DEFAULT_TIER_CASCADE
    )

    assert flat[0] == "anthropic/claude-sonnet-5"


def test_partition_is_stable_on_both_sides():
    """Operator within-tier order survives: the cross-provider peers keep
    their tier2 order (grok before gpt before gemini), then the tier3
    non-affine models keep theirs."""
    flat = oc_model.generate_fallback_list(
        _INCIDENT_TIERS, oc_model.DEFAULT_TIER_CASCADE
    )

    assert flat == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4-5",
        "xai/grok-4",
        "openai/gpt-5.5",
        "google/gemini-3.1-pro-preview",
        "openai/gpt-4.1-mini",
        "google/gemini-2.0-flash",
        "xai/grok-4-mini",
    ]


def test_affinity_does_not_resurrect_the_ceiling_filtered_tier():
    """opus (anthropic, high) shares the primary's provider but sits ABOVE
    the medium primary — the never-escalate filter runs first and affinity
    must not bring it back."""
    flat = oc_model.generate_fallback_list(
        _INCIDENT_TIERS, oc_model.DEFAULT_TIER_CASCADE
    )

    assert "anthropic/claude-opus-4-8" not in flat


def test_bare_primary_skips_the_partition():
    """A hand-rolled tiers file with un-prefixed ids has no affinity to
    honor — the chain keeps plain cascade order."""
    tiers = {
        "tier2": {"models": ["sonnet", "openai/gpt-5.5"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5", "cheap-model"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier2", "tier3"])

    assert flat == [
        "sonnet",
        "openai/gpt-5.5",
        "anthropic/claude-haiku-4-5",
        "cheap-model",
    ]


def test_bare_tail_ids_stay_in_the_non_affine_block():
    """Un-prefixed tail entries group with no provider: they keep their
    relative order among the non-affine models rather than riding the
    primary's affinity."""
    tiers = {
        "tier2": {"models": ["anthropic/claude-sonnet-5", "bare-peer"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5", "bare-cheap"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier2", "tier3"])

    assert flat == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4-5",
        "bare-peer",
        "bare-cheap",
    ]


def test_power_first_cascade_walks_own_ladder_before_peers():
    """An operator power-first cascade gets the full chain (ceiling rule)
    AND walks anthropic's ladder top-to-bottom before other providers."""
    flat = oc_model.generate_fallback_list(
        _INCIDENT_TIERS, ["tier1", "tier2", "tier3"]
    )

    assert flat[:3] == [
        "anthropic/claude-opus-4-8",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4-5",
    ]
    # Cross-provider models all present, after the anthropic ladder.
    for m in ("xai/grok-4", "openai/gpt-5.5", "google/gemini-3.1-pro-preview"):
        assert m in flat[3:]


def test_rehosted_refs_group_by_hosted_vendor_not_transport():
    """Pass-2 review finding: a re-hosted ref's first segment is transport.
    Affinity must group ``openrouter/anthropic/*`` with native ``anthropic/*``
    (same dialect), and must NOT hoist ``openrouter/xai/*`` (cross-family,
    lower rung) over the operator's same-rung peers."""
    tiers = {
        "tier2": {"models": [
            "openrouter/anthropic/claude-sonnet-5",
            "openai/gpt-5.5",
        ]},
        "tier3": {"models": [
            "anthropic/claude-haiku-4-5",
            "openrouter/xai/grok-4-mini",
        ]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier2", "tier3"])

    assert flat == [
        "openrouter/anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4-5",       # affine: hosted vendor matches
        "openai/gpt-5.5",
        "openrouter/xai/grok-4-mini",       # foreign: xai, despite shared transport
    ]


def test_two_segment_gateway_ref_keys_on_its_first_segment():
    """A two-segment ref has no hosted-vendor tail — its first segment IS
    the affinity key, so gateway house models group with each other."""
    tiers = {
        "tier2": {"models": ["openrouter/auto", "openai/gpt-5.5"]},
        "tier3": {"models": ["openrouter/cheap", "openai/gpt-4.1-mini"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier2", "tier3"])

    assert flat == [
        "openrouter/auto",
        "openrouter/cheap",
        "openai/gpt-5.5",
        "openai/gpt-4.1-mini",
    ]


def test_malformed_entries_do_not_crash_and_group_with_no_vendor():
    """Hand-edited legacy tiers can carry truthy non-strings or an
    empty-prefix ref — the affinity key is None for those (no crash on a
    deploy pass; they keep their order in the non-affine block)."""
    tiers = {
        "tier2": {"models": ["anthropic/claude-sonnet-5", 5, "/x"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    }
    flat = oc_model.generate_fallback_list(tiers, ["tier2", "tier3"])

    assert flat == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4-5",
        5,
        "/x",
    ]


def test_all_affine_or_all_foreign_tail_is_untouched():
    """Degenerate partitions (every tail model affine, or none) change
    nothing — including the two-model chain where there is no tail to
    reorder."""
    all_affine = {
        "tier2": {"models": ["anthropic/claude-sonnet-5"]},
        "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    }
    assert oc_model.generate_fallback_list(all_affine, ["tier2", "tier3"]) == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4-5",
    ]

    none_affine = {
        "tier2": {"models": ["anthropic/claude-sonnet-5"]},
        "tier3": {"models": ["openai/gpt-4.1-mini", "google/gemini-2.0-flash"]},
    }
    assert oc_model.generate_fallback_list(none_affine, ["tier2", "tier3"]) == [
        "anthropic/claude-sonnet-5",
        "openai/gpt-4.1-mini",
        "google/gemini-2.0-flash",
    ]
