"""Tests for credential-aware default tier picks + engine-default-tier override.

Two related 2026-06-07 additions to ``packages/analyzer/models.py``:

  1. ``derive_default_tiers(available_providers)`` — replaces the
     hardcoded ``DEFAULT_TIERS`` constant as the fallback when no
     primary-bot tier config exists. Picks model names from whichever
     providers the pod actually has credentials for, so an OpenAI-only
     pod doesn't get Anthropic-pinned fallbacks (the old footgun).

  2. ``engine_default_tier_from_network(network)`` + integration into
     ``resolve_tier()``: when ``network.json::cascade.engine_default_tier``
     is set, engine-side (no-bot_id) ``resolve_tier`` calls collapse
     to that tier — cost-control knob for background Evolve LLM work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from models import (  # noqa: E402
    DEFAULT_TIERS,
    derive_default_tiers,
    engine_default_tier_from_network,
    resolve_tier,
)


# ── derive_default_tiers ────────────────────────────────────────────────────


def test_derive_with_anthropic_only_picks_anthropic_models():
    out = derive_default_tiers({"anthropic"})
    assert out["tier1"]["models"] == ["anthropic/claude-opus-4-6"]
    assert out["tier2"]["models"] == ["anthropic/claude-sonnet-4-6"]
    assert out["tier3"]["models"] == ["anthropic/claude-haiku-4-5"]
    # tier0 falls back to anthropic when no other provider available
    # (anti-Goodhart heuristic can't apply with one provider)
    assert out["tier0"]["models"][0].startswith("anthropic/")


def test_derive_with_openai_only_picks_openai_models():
    """The historical footgun — pod without Anthropic credential. The
    pre-2026-06-07 DEFAULT_TIERS had Anthropic-pinned fallbacks, so
    resolution silently produced unusable model strings."""
    out = derive_default_tiers({"openai"})
    assert out["tier1"]["models"] == ["openai/gpt-4o"]
    assert out["tier2"]["models"] == ["openai/gpt-4o"]
    assert out["tier3"]["models"] == ["openai/gpt-4o-mini"]
    # No anthropic/* in any tier
    for tier_id in ("tier0", "tier1", "tier2", "tier3"):
        for m in out[tier_id]["models"]:
            assert not m.startswith("anthropic/"), (
                f"OpenAI-only pod should not have anthropic models, "
                f"got {m} for {tier_id}"
            )


def test_derive_with_google_only_picks_google_models():
    out = derive_default_tiers({"google"})
    assert out["tier2"]["models"] == ["google/gemini-2.5-pro"]
    assert out["tier3"]["models"] == ["google/gemini-2.0-flash"]


def test_derive_with_multi_provider_picks_anti_goodhart_judge():
    """Tier0 (Judge) prefers a provider that differs from tier2's pick
    when multiple providers are available — the anti-Goodhart heuristic.
    """
    out = derive_default_tiers({"anthropic", "openai"})
    # tier2 is anthropic (first in tier2 picks for anthropic+openai)
    assert out["tier2"]["models"][0].startswith("anthropic/")
    # tier0 should be openai (different provider than tier2)
    assert out["tier0"]["models"][0].startswith("openai/")


def test_derive_with_empty_providers_falls_back_to_DEFAULT_TIERS():
    """Brand-new pod with no readable auth-profiles — must return
    something rather than crash. Falls back to the hardcoded
    DEFAULT_TIERS — better to attempt a known-good Anthropic call
    than to refuse the resolution entirely."""
    out_empty = derive_default_tiers(set())
    out_none = derive_default_tiers(None)
    assert out_empty == out_none
    # Same shape as DEFAULT_TIERS (model lists may differ but the
    # tier IDs and metadata fields are identical)
    assert set(out_empty.keys()) == {"tier0", "tier1", "tier2", "tier3"}
    # Falls back to the known-good Anthropic picks
    assert out_empty["tier2"]["models"] == DEFAULT_TIERS["tier2"]["models"]


def test_derive_preserves_tier_metadata():
    """The derived defaults must include the per-tier name / policy /
    costClass — engine code may read these for display + policy
    enforcement (e.g., check_tier_policy)."""
    out = derive_default_tiers({"anthropic"})
    assert out["tier1"]["name"] == "Power"
    assert "Explicit user request only" in out["tier1"]["policy"]
    assert out["tier1"]["costClass"] == "high"
    assert out["tier3"]["name"] == "Grunt"
    assert out["tier3"]["costClass"] == "low"


# ── engine_default_tier_from_network ────────────────────────────────────────


def test_engine_default_tier_none_when_unset():
    assert engine_default_tier_from_network({}) is None
    assert engine_default_tier_from_network({"cascade": {}}) is None
    assert engine_default_tier_from_network(
        {"cascade": {"engine_default_tier": None}}
    ) is None


def test_engine_default_tier_validates_value():
    """Only known tier IDs are accepted. Garbage values return None
    (fail-safe: better to honor the caller's tier than to silently
    apply a junk override)."""
    assert engine_default_tier_from_network(
        {"cascade": {"engine_default_tier": "tier3"}}
    ) == "tier3"
    assert engine_default_tier_from_network(
        {"cascade": {"engine_default_tier": "tier1"}}
    ) == "tier1"
    # Invalid values reject
    assert engine_default_tier_from_network(
        {"cascade": {"engine_default_tier": "haiku"}}
    ) is None
    assert engine_default_tier_from_network(
        {"cascade": {"engine_default_tier": "tier99"}}
    ) is None
    assert engine_default_tier_from_network(
        {"cascade": {"engine_default_tier": 3}}
    ) is None


def test_engine_default_tier_safe_on_malformed_network():
    """Defensive: any non-dict network or non-dict cascade returns None
    without raising. Production code must not crash on this read path."""
    # Type: ignore — intentional bad shapes
    assert engine_default_tier_from_network(None) is None  # type: ignore
    assert engine_default_tier_from_network("not a dict") is None  # type: ignore
    assert engine_default_tier_from_network({"cascade": "not a dict"}) is None


# ── resolve_tier integration ────────────────────────────────────────────────


def test_resolve_tier_engine_override_collapses_tier1_to_tier3():
    """When engine_default_tier=tier3 is set, an engine-side (no
    bot_id) request for tier1 collapses to tier3 — the cost-control
    knob doing its job."""
    network = {
        "cascade": {"engine_default_tier": "tier3"},
    }
    model = resolve_tier("tier1", network, bot_id=None)
    # The actual tier3 model depends on DEFAULT_TIERS fallback;
    # just confirm it's a tier3 pick (haiku family)
    assert "haiku" in model.lower() or "mini" in model.lower() or "flash" in model.lower()


def test_resolve_tier_engine_override_does_NOT_affect_bot_calls():
    """The engine override is for ENGINE calls only — calls with a
    bot_id are user-facing bot routing and must NOT be redirected.
    Even if cascade.engine_default_tier=tier3, a bot asking for tier1
    gets tier1."""
    network = {
        "cascade": {"engine_default_tier": "tier3"},
    }
    model = resolve_tier("tier1", network, bot_id="some_bot")
    # Should still be a tier1 (Opus / GPT-4 / etc.) model
    assert (
        "opus" in model.lower()
        or "gpt-4" in model.lower()
        or "gemini-2.5-pro" in model.lower()
    )


def test_resolve_tier_no_override_honors_caller_tier():
    """When engine_default_tier is unset, engine-side calls honor the
    tier the caller asked for (back-compat — no behavior change for
    pods that haven't opted into the override)."""
    model = resolve_tier("tier1", {}, bot_id=None)
    # Should be a tier1 model (DEFAULT_TIERS fallback)
    assert "opus" in model.lower() or "gpt-4" in model.lower()
