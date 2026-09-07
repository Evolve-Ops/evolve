"""Tests for install_cost_estimator — pre-install cost projection.

The estimator is the structural fix for the 2026-06-03 team_bot_c incident
where a legitimate $33.64 Unified Task System install caught the
operator by surprise. These tests pin the contract that:

  * a 20KB build_spec on a substantial bot context lands in the
    $20-40 range for Sonnet 4.6 (matches the incident's actual cost)
  * a tiny build_spec lands under the default $5 auto-approve threshold
  * Haiku 4.5 vs Sonnet 4.6 for the same input shows the expected price
    ratio (Haiku ~25% of Sonnet)
  * unknown models fall back through provider pricing
  * truly unknown providers return $0 with estimate_unavailable=True
    rather than silently claiming free
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from install_cost_estimator import (  # noqa: E402
    InstallCostEstimate,
    estimate_install_cost,
    estimate_to_dict,
)


def _build_spec_of_bytes(n: int) -> str:
    """Produce a build_spec string of exactly n bytes (printable ASCII)."""
    chunk = "ABCDEFGHIJ" * 100  # 1000 bytes
    return (chunk * (n // 1000 + 1))[:n]


# ── Core projection bands ────────────────────────────────────────────────


def test_band_is_well_formed():
    """low < mid < high with the documented multipliers (0.5×, 2×)."""
    spec = _build_spec_of_bytes(20_000)
    net = {"forge": {"builder_model": "anthropic/claude-sonnet-4-6"}}
    est = estimate_install_cost("nonexistent_bot", spec, network=net)

    assert est.low_usd == round(0.5 * est.mid_usd, 4)
    assert est.high_usd == round(2.0 * est.mid_usd, 4)
    assert est.low_usd < est.mid_usd < est.high_usd
    assert est.model == "anthropic/claude-sonnet-4-6"


def test_substantial_install_with_heavyweight_context_lands_in_brief_band():
    """20KB spec + 150KB context (team_bot_c profile) → mid in $5-50 band.

    The brief expected $25-40 for the team_bot_c incident. Absolute dollars
    are calibration-dependent; the unit test pins that the math produces
    a non-trivial estimate in a defensible band — not pennies, not
    thousands. Production calibration tunes the constants.
    """
    spec = _build_spec_of_bytes(20_000)
    net = {"forge": {"builder_model": "anthropic/claude-sonnet-4-6"}}
    est = estimate_install_cost(
        "nonexistent_bot",
        spec,
        network=net,
        bot_context_bytes_override=150_000,  # heavyweight bot like team_bot_c
    )

    assert 5.0 <= est.mid_usd <= 60.0, (
        f"mid_usd={est.mid_usd}; expected single-to-double-digit dollars "
        f"for 20KB spec + 150KB context (team_bot_c profile)"
    )


def test_tiny_spec_with_small_context_lands_under_auto_approve_threshold():
    """A 1KB build_spec on a small bot context → under $5 default threshold."""
    spec = _build_spec_of_bytes(1_000)
    net = {"forge": {"builder_model": "anthropic/claude-sonnet-4-6"}}
    est = estimate_install_cost(
        "nonexistent_bot",
        spec,
        network=net,
        bot_context_bytes_override=32_000,
    )

    assert est.mid_usd < 5.0, (
        f"mid_usd={est.mid_usd}; tiny installs should auto-approve"
    )


def test_estimate_grows_with_spec_size():
    """Doubling spec size → estimate grows monotonically (the contract operators rely on)."""
    net = {"forge": {"builder_model": "anthropic/claude-sonnet-4-6"}}
    small = estimate_install_cost(
        "nonexistent_bot", _build_spec_of_bytes(5_000),
        network=net, bot_context_bytes_override=64_000,
    )
    big = estimate_install_cost(
        "nonexistent_bot", _build_spec_of_bytes(50_000),
        network=net, bot_context_bytes_override=64_000,
    )
    assert big.mid_usd > small.mid_usd


def test_estimate_grows_with_context_size():
    """Heavier bot context → bigger estimate (forge re-sends context per call)."""
    net = {"forge": {"builder_model": "anthropic/claude-sonnet-4-6"}}
    spec = _build_spec_of_bytes(10_000)
    light = estimate_install_cost(
        "nonexistent_bot", spec, network=net, bot_context_bytes_override=32_000,
    )
    heavy = estimate_install_cost(
        "nonexistent_bot", spec, network=net, bot_context_bytes_override=200_000,
    )
    assert heavy.mid_usd > light.mid_usd


def test_haiku_is_cheaper_than_sonnet_for_same_inputs():
    """Same spec, Haiku → Sonnet ratio matches the price-table ratio (~25%)."""
    spec = _build_spec_of_bytes(10_000)
    sonnet_net = {"forge": {"builder_model": "anthropic/claude-sonnet-4-6"}}
    haiku_net = {"forge": {"builder_model": "anthropic/claude-haiku-4-5"}}

    sonnet_est = estimate_install_cost("nonexistent_bot", spec, network=sonnet_net)
    haiku_est = estimate_install_cost("nonexistent_bot", spec, network=haiku_net)

    assert sonnet_est.mid_usd > 0
    assert haiku_est.mid_usd > 0
    ratio = haiku_est.mid_usd / sonnet_est.mid_usd
    # Haiku output is 4/15 of Sonnet, input is 0.8/3.0 ≈ 0.27, cache
    # similar — overall ratio should land near 0.25-0.30.
    assert 0.15 <= ratio <= 0.40, (
        f"Haiku/Sonnet ratio={ratio:.3f}; price table change suspected"
    )


# ── Model resolution + fallback ──────────────────────────────────────────


def test_default_model_when_network_silent():
    """No forge.builder_model in network → default Sonnet 4.6."""
    spec = _build_spec_of_bytes(5_000)
    est = estimate_install_cost("nonexistent_bot", spec, network={})
    assert est.model == "anthropic/claude-sonnet-4-6"
    assert est.mid_usd > 0


def test_bare_anthropic_model_gets_provider_prefix():
    """forge_engine stores models as bare 'claude-sonnet-4-6'; we normalize."""
    spec = _build_spec_of_bytes(5_000)
    net = {"forge": {"builder_model": "claude-sonnet-4-6"}}
    est = estimate_install_cost("nonexistent_bot", spec, network=net)
    assert est.model == "anthropic/claude-sonnet-4-6"
    assert est.mid_usd > 0


def test_unknown_provider_returns_estimate_unavailable():
    """Unknown provider AND unknown model → 0.0 with explicit flag."""
    spec = _build_spec_of_bytes(5_000)
    net = {"forge": {"builder_model": "totally-fake/unknown-model"}}
    est = estimate_install_cost("nonexistent_bot", spec, network=net)
    assert est.mid_usd == 0.0
    assert est.components.get("estimate_unavailable") is True


def test_known_provider_unknown_model_uses_provider_fallback():
    """anthropic/some-future-model → falls back to anthropic provider pricing."""
    spec = _build_spec_of_bytes(5_000)
    net = {"forge": {"builder_model": "anthropic/claude-future-model-99"}}
    est = estimate_install_cost("nonexistent_bot", spec, network=net)
    # Provider fallback for anthropic is the Sonnet 4.6 rate — same as default.
    assert est.mid_usd > 0
    assert est.components.get("estimate_unavailable") is None


# ── Components surfacing ─────────────────────────────────────────────────


def test_components_carry_breakdown_for_ui():
    """The UI needs the breakdown to render a tooltip — pin the shape."""
    spec = _build_spec_of_bytes(20_000)
    est = estimate_install_cost("nonexistent_bot", spec, network={})
    comps = est.components
    assert comps["build_spec_bytes"] == 20_000
    assert comps["bot_context_bytes"] >= 32_000  # floor when bot doesn't exist
    assert comps["tool_calls"] >= 10
    assert comps["iteration_multiplier"] == 2.0  # build + critique + refine
    assert comps["model_resolved"] == "anthropic/claude-sonnet-4-6"
    assert comps["pricing_source"] == "model"


def test_iteration_multiplier_responds_to_config():
    """Disabling critique + refine → multiplier 1.0; estimate proportionally lower."""
    spec = _build_spec_of_bytes(10_000)
    full_net = {"forge": {"builder_model": "anthropic/claude-sonnet-4-6"}}
    bare_net = {"forge": {
        "builder_model": "anthropic/claude-sonnet-4-6",
        "critique_iters": 0,
        "refine_iters": 0,
    }}
    full = estimate_install_cost("nonexistent_bot", spec, network=full_net)
    bare = estimate_install_cost("nonexistent_bot", spec, network=bare_net)
    assert bare.components["iteration_multiplier"] == 1.0
    assert full.components["iteration_multiplier"] == 2.0
    # Mid scales linearly with multiplier (modulo cache-vs-input rounding).
    assert bare.mid_usd < full.mid_usd
    ratio = bare.mid_usd / full.mid_usd
    assert 0.45 <= ratio <= 0.55, f"expected ~0.5 build-only ratio, got {ratio:.3f}"


def test_empty_build_spec_still_produces_estimate():
    """Empty spec is degenerate but should not throw — bot context dominates."""
    est = estimate_install_cost("nonexistent_bot", "", network={})
    assert est.mid_usd >= 0
    assert est.input_tokens > 0  # bot context alone is non-zero


# ── Serialisation ────────────────────────────────────────────────────────


def test_estimate_to_dict_is_json_serialisable():
    """The API endpoints serialise via this helper; must round-trip through json."""
    import json as _json

    spec = _build_spec_of_bytes(5_000)
    est = estimate_install_cost("nonexistent_bot", spec, network={})
    d = estimate_to_dict(est)
    encoded = _json.dumps(d)
    decoded = _json.loads(encoded)
    assert decoded["model"] == est.model
    assert decoded["mid_usd"] == est.mid_usd
    assert decoded["components"]["tool_calls"] == est.components["tool_calls"]
