"""tests/test_tier_severity_classify.py — Phase 12c §C tier-severity split.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 10 §C.

``classify_role_severity`` / ``classify_bot_tier_severities`` distinguish a
genuinely broken tier (no credentialed model anywhere in the chain → won't
route) from an inert deep-chain fallback (resolves fine, just carries an
uncredentialed entry that auto-activates when a key is added).

Acceptance matrix:
  - a fully-uncredentialed tier classifies hard_break;
  - a tier with a credentialed model + an inert non-cred fallback classifies
    dormant (not hard_break).

(The former judge-only ``advisory`` severity died with the judge role —
design-judge-role-collapse-2026-08-21 §5.4. A stale structured ``roles.judge``
entry in an un-migrated catalog stays parseable and is simply not classified.)
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


def test_stale_structured_judge_entry_is_parseable_and_unclassified():
    """Read-compat (judge-role collapse §6.1): an un-migrated catalog carrying
    the old structured roles.judge entry must not break classification of the
    real roles — and the judge key itself is no longer classified."""
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
    res = classify_role_severity(cat, "standard", {"anthropic"})
    assert res["severity"] == SEVERITY_DORMANT
    assert res["model"] == "anthropic/claude-sonnet-4-6"


# ── classify_bot_tier_severities (legacy tierN map → merged catalog) ──────────


def test_bot_tier_severities_from_legacy_tiers_anthropic_only():
    """An anthropic-only bot on pod defaults: ladder roles resolve to anthropic
    (ok/dormant). The legacy tierN map is folded over the code defaults,
    matching the gateway. No judge row — the role is gone."""
    sev = classify_bot_tier_severities(
        pod_models={},
        bot_tiers={"tier1": {"models": ["anthropic/claude-opus-4-8", "xai/grok-4"]}},
        credentialed_providers={"anthropic"},
    )
    # power (tier1) resolves to anthropic but carries the inert xai fallback.
    assert sev["power"]["severity"] == SEVERITY_DORMANT
    assert "xai/grok-4" in sev["power"]["dormant_models"]
    # Every role in ROLE_ORDER is classified — and ONLY those (judge is gone).
    assert set(sev) == {"fast", "standard", "power", "max"}


def test_bot_tier_severities_two_providers_all_route():
    """With a second credentialed provider, every ladder role still resolves."""
    sev = classify_bot_tier_severities(
        pod_models={},
        bot_tiers={},
        credentialed_providers={"anthropic", "openai"},
    )
    for role in ("fast", "standard", "power", "max"):
        assert sev[role]["severity"] != SEVERITY_HARD_BREAK
        assert sev[role]["model"] is not None
