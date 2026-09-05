"""tests/test_12c_inert_providers.py — Phase 12c §C server-side.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 10 §C —
hard-break vs soft-dormant distinction.

resolve_roles_with_provenance adds ``inertProviders`` to each role entry:
  - Non-empty when credentialed_providers is known AND the rung carries models
    from providers the bot has no key for (soft-dormant case).
  - Empty when all rung providers are credentialed (normal case).
  - Empty list when credentials are unknown (fail-open — no false alarms).
  - Hard-break (fully uncredentialed rung) produces available=False +
    unavailableReason="uncredentialed" + non-empty inertProviders (the rung's
    providers are still listed so the UI can name them in the remedy message;
    the hard-break card fires on ``!available`` before inspecting inert contents).

These tests verify the data shape; the rendering distinction lives in the JS.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import primary_bot as pb  # noqa: E402


def _minimal_catalog(extra_models: list[str] | None = None) -> dict:
    """A minimal anthropic-only default catalog (code defaults), optionally
    enriched with additional models in the standard rung."""
    models = list(extra_models or [])
    return {
        "models": {
            "rungs": [
                {"id": "haiku-class", "costClass": "low",
                 "models": ["anthropic/claude-haiku-4-5"]},
                {"id": "sonnet-class", "costClass": "medium",
                 "models": ["anthropic/claude-sonnet-4-6"] + models},
                {"id": "opus-class", "costClass": "high",
                 "models": ["anthropic/claude-opus-4-8"]},
                {"id": "fable-class", "costClass": "premium",
                 "models": ["anthropic/claude-fable-5"]},
            ],
            "roles": {
                "fast": "haiku-class",
                "standard": "sonnet-class",
                "power": "opus-class",
                "max": "fable-class",
                
            },
        },
        "bots": {"bot-a": {"user": "bot-a"}},
        "sharedDir": "/tmp/test-12c",
    }


def _resolve(network, bot_id="bot-a", creds=None, monkeypatch_pb=None):
    """Call resolve_roles_with_provenance with a no-op bot-tiers path."""
    import unittest.mock as mock
    with mock.patch.object(pb, "_bot_evolve_tiers_path", return_value="/nonexistent"), \
         mock.patch.object(pb, "_read_bot_owned_json", return_value=({}, False)):
        return pb.resolve_roles_with_provenance(network, bot_id=bot_id,
                                                 credentialed_providers=creds)


# ── (a) hard-break: role with NO credentialed model ──────────────────────────

def test_hard_break_available_false_uncredentialed():
    """A role whose ENTIRE degradation chain is non-credentialed is hard-break:
    available=False, unavailableReason=uncredentialed, inertProviders=[]."""
    # ALL rungs have only openai; bot-a has only openai creds. So anthropic
    # roles (fast/standard/power/max all point at anthropic-only rungs) but
    # we test a catalog where ALL rungs are xai-only and bot has only openai.
    # Simpler: build a catalog with a single-rung custom role chain where
    # the target role degrades to None immediately (fast → None in DEGRADE_CHAIN).
    # fast (haiku-class) has only xai/grok; bot has only anthropic → hard break.
    net = _minimal_catalog()
    net["models"]["rungs"][0]["models"] = ["xai/grok-4-mini"]  # haiku-class = xai-only
    out = _resolve(net, creds={"anthropic"})
    fast = out["fast"]
    # fast degrades to None (end of chain) → available=False, reason=uncredentialed.
    assert fast["available"] is False
    assert fast["unavailableReason"] == "uncredentialed"
    # inertProviders carries the non-credentialed providers even for hard-break
    # (the rung's xai model IS inert from the bot's perspective). The UI
    # renders the hard-break row (not dormant) because available=False is checked
    # first — inertProviders here gives the operator names for the remedy message.
    assert "xai" in fast["inertProviders"]


def test_hard_break_does_not_fire_for_credentialed_roles():
    """Roles that resolve normally have available=True and empty inertProviders."""
    net = _minimal_catalog()
    out = _resolve(net, creds={"anthropic"})
    for role in ("fast", "power"):
        r = out[role]
        assert r["available"] is True, role
        assert r["inertProviders"] == [], role


# ── (b) soft-dormant: resolves fine + inert non-cred fallbacks ───────────────

def test_soft_dormant_inert_providers_listed():
    """A role that resolves (anthropic model exists) but whose rung ALSO has
    an openai fallback model appears as soft-dormant: available=True,
    inertProviders=['openai']."""
    # standard rung has anthropic first + openai; bot-a has only anthropic creds.
    net = _minimal_catalog(extra_models=["openai/gpt-4.1"])
    out = _resolve(net, creds={"anthropic"})
    std = out["standard"]
    assert std["available"] is True
    assert std["resolvedModel"] is not None
    assert pb.provider_of(std["resolvedModel"]) == "anthropic"
    assert "openai" in std["inertProviders"]


def test_soft_dormant_inert_sorted():
    """inertProviders list is sorted (stable, deterministic output)."""
    net = _minimal_catalog(extra_models=["xai/grok-4", "openai/gpt-4.1"])
    out = _resolve(net, creds={"anthropic"})
    std = out["standard"]
    assert std["available"] is True
    assert std["inertProviders"] == sorted(std["inertProviders"])
    assert "openai" in std["inertProviders"]
    assert "xai" in std["inertProviders"]


# ── (c) all credentialed: normal case ────────────────────────────────────────

def test_all_credentialed_empty_inert():
    """When the bot has keys for ALL providers in the rung, inertProviders=[].
    This is the normal-operating case; no dormant row should appear."""
    net = _minimal_catalog(extra_models=["openai/gpt-4.1"])
    out = _resolve(net, creds={"anthropic", "openai"})
    std = out["standard"]
    assert std["available"] is True
    assert std["inertProviders"] == []


def test_unknown_creds_fail_open_empty_inert():
    """credentialed_providers=None → inertProviders=[] for every role (fail-open:
    no spurious dormant advisories when credentials can't be determined)."""
    net = _minimal_catalog(extra_models=["openai/gpt-4.1"])
    out = _resolve(net, creds=None)
    for role in ("fast", "standard", "power", "max"):
        assert out[role]["inertProviders"] == [], role


# ── field is present on all roles ────────────────────────────────────────────

def test_inert_providers_present_on_all_roles():
    """inertProviders key is present on every role entry (never missing)."""
    net = _minimal_catalog()
    out = _resolve(net, creds={"anthropic"})
    for role in ("fast", "standard", "power", "max"):
        assert "inertProviders" in out[role], role
        assert isinstance(out[role]["inertProviders"], list), role
