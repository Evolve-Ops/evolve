"""tests/test_model_catalog_parity.py — TS<->Python keyed-merge parity.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2.

The default model catalog ships in code on BOTH sides (primary_bot.py and
ModelRouter.ts). The two must resolve a given (pod, bot) override pair to the
SAME five-role primaries. This test pins the Python side to the shared fixture
``packages/plugin/tests/fixtures/model-catalog-parity.json``; the TS test
(``modelRouter.catalogParity.test.mjs``) pins the TS side to the same file.
Edit the fixture once; both sides are held to it.

It also doubles as the bare-pod resolution matrix (spec acceptance):
defaults-only resolves all five roles, pod beats default, bot beats pod, and
``max`` resolves to claude-fable-5 with NO fable config anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from primary_bot import (  # noqa: E402
    merge_model_catalog,
    tier_override_is_broken,
    _resolve_role_to_rung,
    _rung_models,
)

_FIXTURE = (
    _ANALYZER_DIR.parent
    / "plugin"
    / "tests"
    / "fixtures"
    / "model-catalog-parity.json"
)

_ROLES = ("fast", "standard", "power", "max", "judge")


def _load_scenarios() -> list[dict]:
    data = json.loads(_FIXTURE.read_text())
    return data["scenarios"]


def _provider_of(model: str | None) -> str | None:
    if not isinstance(model, str) or "/" not in model:
        return None
    head = model.split("/", 1)[0]
    return head.lower() if head else None


def _resolve_primary(merged: dict, role: str) -> str | None:
    rung = _resolve_role_to_rung(merged, role)
    models = _rung_models(merged, rung) if rung else []
    if not models:
        return None
    # ``judge`` is provider-diversity-constrained (spec §judge
    # provider-diversity; ModelRouter._resolveJudgeModel): the first model in
    # the judge rung whose provider differs from standard's resolved primary,
    # or None if the rung holds no provider-diverse option. Other roles take
    # rung.models[0].
    if role == "judge":
        standard_provider = _provider_of(_resolve_primary(merged, "standard"))
        for m in models:
            if _provider_of(m) != standard_provider:
                return m
        return None
    return models[0]


@pytest.mark.parametrize("scenario", _load_scenarios(), ids=lambda s: s["name"])
def test_python_resolution_matches_shared_fixture(scenario):
    """Each scenario's (pod, bot) merges (defaults <- pod <- bot) and resolves
    the five roles to the fixture's expected primaries."""
    merged = merge_model_catalog(scenario["pod"], scenario["bot"])
    for role in _ROLES:
        expected = scenario["expect"][role]
        actual = _resolve_primary(merged, role)
        assert actual == expected, (
            f"{scenario['name']}: role {role} resolved to {actual!r}, "
            f"fixture expects {expected!r}"
        )


def test_bare_pod_arms_max_from_defaults():
    """Acceptance: a bot with NO fable config anywhere resolves max ->
    claude-fable-5 purely from the code defaults."""
    merged = merge_model_catalog({}, {})
    assert _resolve_primary(merged, "max") == "anthropic/claude-fable-5"


def test_default_max_cap_is_five_on_a_default_armed_pod():
    """The default catalog ships roleCaps.max.maxPerDayPerBot = 5 — the
    cost-safety floor that lets Max ship armed (not dormant)."""
    merged = merge_model_catalog({}, {})
    assert merged["roleCaps"]["max"]["maxPerDayPerBot"] == 5
    assert merged["roleCaps"]["power"]["maxPerDayPerBot"] == 10


# ── empty-rung-is-a-no-op merge + tier_override_is_broken detector helper ──────
# Mirrors the TS modelRouter.keyedMerge.test.mjs cases (#2561 onboarding
# semantics). Keep in lockstep with ModelRouter.ts.


def test_empty_models_override_rung_does_not_shadow_base():
    """An empty-models override rung keeps the base rung's models (no-op for
    models) but applies its other fields — it must NOT brick resolution."""
    base = {"rungs": [{"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"}]}
    over = {"rungs": [{"id": "sonnet-class", "models": [], "costClass": "premium"}]}
    merged = merge_model_catalog(base, over, include_defaults=False)
    rung = next(r for r in merged["rungs"] if r["id"] == "sonnet-class")
    assert rung["models"] == ["anthropic/claude-sonnet-4-6"]
    assert rung["costClass"] == "premium"


def test_tier_override_is_broken_flags_explicit_empty_only():
    """Explicit-but-empty tier flags; an OMITTED tier (defaulted) does not."""
    # Legacy shape.
    assert tier_override_is_broken({"tiers": {"tier2": {"models": []}}}, "tier2") is True
    assert tier_override_is_broken({"tiers": {"tier2": {"models": ["  "]}}}, "tier2") is True
    assert tier_override_is_broken({"tiers": {"tier0": {"models": ["openai/gpt-4o"]}}}, "tier2") is False
    assert tier_override_is_broken({"tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}}, "tier2") is False
    # New shape (tier2 → sonnet-class rung).
    assert tier_override_is_broken({"rungs": [{"id": "sonnet-class", "models": []}]}, "tier2") is True
    assert tier_override_is_broken({"rungs": [{"id": "opus-class", "models": []}]}, "tier2") is False
    assert tier_override_is_broken({}, "tier2") is False
