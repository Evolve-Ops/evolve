"""tests/test_tier_severity_classify.py — Phase 12c §C tier-severity split.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 10 §C.

``classify_role_severity`` / ``classify_bot_tier_severities`` distinguish a
genuinely broken tier (no credentialed model anywhere in the chain → won't
route) from an inert deep-chain fallback (resolves fine, just carries an
uncredentialed entry that auto-activates when a key is added).

Acceptance matrix (from the spawn brief):
  - a fully-uncredentialed tier classifies hard_break;
  - a tier with a credentialed model + an inert non-cred fallback classifies
    dormant (not hard_break);
  - judge-only-has-standard-provider is hard_break.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from primary_bot import (  # noqa: E402
    classify_role_severity,
    classify_bot_tier_severities,
    SEVERITY_HARD_BREAK,
    SEVERITY_DORMANT,
    SEVERITY_OK,
    SEVERITY_ADVISORY,
)


# ── classify_role_severity (pure) ────────────────────────────────────────────


def test_fully_uncredentialed_chain_is_hard_break():
    """A role whose whole degrade chain is from an uncredentialed provider has
    no working model → hard_break, model None, reason uncredentialed."""
    cat = {
        "rungs": [
            {"id": "opus-class", "models": ["xai/grok-4"], "costClass": "high"},
            {"id": "sonnet-class", "models": ["xai/grok-4"], "costClass": "medium"},
            {"id": "haiku-class", "models": ["xai/grok-4-mini"], "costClass": "low"},
        ],
        "roles": {
            "power": "opus-class",
            "standard": "sonnet-class",
            "fast": "haiku-class",
        },
    }
    res = classify_role_severity(cat, "power", {"anthropic"})
    assert res["severity"] == SEVERITY_HARD_BREAK
    assert res["model"] is None
    assert res["reason"] == "uncredentialed"
    assert res["dormant_models"] == []


def test_credentialed_model_plus_inert_fallback_is_dormant():
    """A role that resolves to a credentialed model but whose rung ALSO names an
    uncredentialed model is dormant — the fallback is inert, not a break."""
    cat = {
        "rungs": [{
            "id": "opus-class",
            "models": ["anthropic/claude-opus-4-8", "xai/grok-4"],
            "costClass": "high",
        }],
        "roles": {"power": "opus-class"},
    }
    res = classify_role_severity(cat, "power", {"anthropic"})
    assert res["severity"] == SEVERITY_DORMANT
    assert res["model"] == "anthropic/claude-opus-4-8"
    assert res["dormant_models"] == ["xai/grok-4"]


def test_judge_with_only_standard_provider_is_advisory_not_break():
    """judge PREFERS a provider other than standard's, but diversity is a
    recommendation (2026-06-19), not a requirement. A bot credentialed only for
    standard's provider still ROUTES the judge on that same vendor → severity
    advisory (NOT hard_break), with reason same_vendor_as_standard and the
    doubled-up provider in advisory_provider."""
    cat = {
        "rungs": [{
            "id": "sonnet-class",
            "models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4.1"],
            "costClass": "medium",
        }],
        "roles": {
            "standard": "sonnet-class",
            "judge": {"rung": "sonnet-class", "provider": "not-standard"},
        },
    }
    res = classify_role_severity(cat, "judge", {"anthropic"})
    assert res["severity"] == SEVERITY_ADVISORY
    assert res["model"] == "anthropic/claude-sonnet-4-6"   # routes, same vendor
    assert res["reason"] == "same_vendor_as_standard"
    assert res["advisory_provider"] == "anthropic"


def test_judge_no_credentialed_model_at_all_is_hard_break():
    """The TRUE hard break: no model in the judge rung has a credentialed
    provider at all → model None, reason uncredentialed, severity hard_break."""
    cat = {
        "rungs": [{
            "id": "sonnet-class",
            "models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4.1"],
            "costClass": "medium",
        }],
        "roles": {
            "standard": "sonnet-class",
            "judge": {"rung": "sonnet-class", "provider": "not-standard"},
        },
    }
    # google credentialed but names no model in the rung → nothing routes.
    res = classify_role_severity(cat, "judge", {"google"})
    assert res["severity"] == SEVERITY_HARD_BREAK
    assert res["model"] is None
    assert res["reason"] == "uncredentialed"


def test_resolves_with_no_fallback_is_ok():
    cat = {
        "rungs": [{"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"}],
        "roles": {"power": "opus-class"},
    }
    res = classify_role_severity(cat, "power", {"anthropic"})
    assert res["severity"] == SEVERITY_OK
    assert res["model"] == "anthropic/claude-opus-4-8"
    assert res["dormant_models"] == []


def test_unknown_credentials_fail_open_to_ok():
    """available_providers=None (reader can't see keys) never flags a break or
    dormant — the role resolves as configured and reports ok."""
    cat = {
        "rungs": [{
            "id": "opus-class",
            "models": ["anthropic/claude-opus-4-8", "xai/grok-4"],
            "costClass": "high",
        }],
        "roles": {"power": "opus-class"},
    }
    res = classify_role_severity(cat, "power", None)
    assert res["severity"] == SEVERITY_OK
    assert res["dormant_models"] == []


def test_judge_dormant_when_diverse_provider_credentialed():
    """Two credentialed providers → judge resolves to the non-standard one; the
    remaining uncredentialed rung members are dormant fallbacks, not a break."""
    cat = {
        "rungs": [{
            "id": "sonnet-class",
            "models": [
                "anthropic/claude-sonnet-4-6", "openai/gpt-4.1",
                "google/gemini-2.5-pro", "xai/grok-4",
            ],
            "costClass": "medium",
        }],
        "roles": {
            "standard": "sonnet-class",
            "judge": {"rung": "sonnet-class", "provider": "not-standard"},
        },
    }
    res = classify_role_severity(cat, "judge", {"anthropic", "openai"})
    assert res["severity"] == SEVERITY_DORMANT
    assert res["model"] == "openai/gpt-4.1"   # first non-standard credentialed
    # google + xai (uncredentialed) are inert fallbacks; anthropic is the
    # credentialed standard provider, diversity-excluded but NOT flagged dormant.
    assert set(res["dormant_models"]) == {"google/gemini-2.5-pro", "xai/grok-4"}


# ── classify_bot_tier_severities (legacy tierN map → merged catalog) ──────────


def test_bot_tier_severities_from_legacy_tiers_anthropic_only():
    """An anthropic-only bot on pod defaults (evo's live shape): ladder roles
    resolve to anthropic (ok/dormant); judge can't reach a diverse provider but
    STILL ROUTES same-vendor → severity advisory (NOT hard_break). The legacy
    tierN map is folded over the code defaults, matching the gateway."""
    sev = classify_bot_tier_severities(
        pod_models={},
        bot_tiers={"tier1": {"models": ["anthropic/claude-opus-4-8", "xai/grok-4"]}},
        credentialed_providers={"anthropic"},
    )
    # power (tier1) resolves to anthropic but carries the inert xai fallback.
    assert sev["power"]["severity"] == SEVERITY_DORMANT
    assert "xai/grok-4" in sev["power"]["dormant_models"]
    # judge routes on anthropic (same vendor as standard) — soft advisory, not
    # a hard break: it still routes, diversity is just a recommendation.
    assert sev["judge"]["severity"] == SEVERITY_ADVISORY
    assert sev["judge"]["model"] is not None
    assert sev["judge"]["reason"] == "same_vendor_as_standard"
    # Every role in ROLE_ORDER is classified.
    assert set(sev) == {"fast", "standard", "power", "max", "judge"}


def test_bot_tier_severities_two_providers_judge_ok():
    """With a second credentialed provider, judge resolves and is no longer a
    hard_break."""
    sev = classify_bot_tier_severities(
        pod_models={},
        bot_tiers={},
        credentialed_providers={"anthropic", "openai"},
    )
    assert sev["judge"]["severity"] != SEVERITY_HARD_BREAK
    assert sev["judge"]["model"] is not None
