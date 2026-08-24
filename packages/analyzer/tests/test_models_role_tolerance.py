"""Tests for role-ID tolerance in the engine-side tier resolver.

spec-model-rungs-and-roles-2026-06-09 Phase 1: ``packages/analyzer/models.py``
keeps its tier-keyed storage but resolves a role ID (fast/standard/power/
judge/max) to its legacy tier before lookup, and ``COST_CLASS_ORDER`` gains
``premium`` for the Fable-class rung.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from models import (  # noqa: E402
    COST_CLASS_ORDER,
    ROLE_TO_TIER,
    get_tier_models,
    is_role_ref,
    is_tier_ref,
    normalize_to_tier,
    resolve_tier,
)


def test_normalize_to_tier_maps_roles():
    assert normalize_to_tier("fast") == "tier3"
    assert normalize_to_tier("standard") == "tier2"
    assert normalize_to_tier("power") == "tier1"
    assert normalize_to_tier("judge") == "tier0"
    # max has no legacy tier above power → falls back to tier1.
    assert normalize_to_tier("max") == "tier1"


def test_normalize_passes_tier_keys_through():
    for t in ("tier0", "tier1", "tier2", "tier3"):
        assert normalize_to_tier(t) == t


def test_resolve_tier_accepts_role_id():
    cfg = {
        "models": {},
    }
    # No bot config; resolves from DEFAULT_TIERS. A role ID resolves the
    # same model its legacy tier would.
    by_role = resolve_tier("fast", cfg, bot_id="x")
    by_tier = resolve_tier("tier3", cfg, bot_id="x")
    assert by_role == by_tier


def test_get_tier_models_accepts_role_id():
    cfg = {"models": {}}
    assert get_tier_models("standard", cfg, bot_id="x") == get_tier_models("tier2", cfg, bot_id="x")


def test_is_tier_ref_accepts_roles_and_tiers():
    assert is_tier_ref("tier1")
    assert is_tier_ref("power")
    assert is_tier_ref("max")
    assert not is_tier_ref("nonsense")


def test_is_role_ref():
    assert is_role_ref("standard")
    assert not is_role_ref("tier2")


def test_cost_class_order_has_premium():
    assert "premium" in COST_CLASS_ORDER
    # premium sorts above high.
    assert COST_CLASS_ORDER.index("premium") > COST_CLASS_ORDER.index("high")


def test_role_to_tier_complete():
    assert set(ROLE_TO_TIER) == {"fast", "standard", "power", "judge", "max"}
