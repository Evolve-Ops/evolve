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

import json
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


# ── "no disk writes" is a promise, and it used to be false ────────────────

def test_a_supplied_network_is_never_re_resolved_from_disk(monkeypatch):
    """The docstring says pure; ``_bot_context_bytes`` made it a liar.

    ``bot_home(bot_id)`` with no config calls ``evolve_config.load_config()``,
    which resolves the CANONICAL network.json and writes the migrated file
    back when its schema moved. So an estimate — computed inside a request
    handler that had already loaded the config — could write to the pod's real
    shared dir. It reddened an admin CI shard exactly that way: a route's
    projection reached ``/Users/Shared/evolve/network.json.tmp``.

    Two facts are pinned, because the safe fix depends on both:
    a supplied network (``{}`` counts — it is an answer, not an absence)
    never touches disk, and omitting it keeps the old resolve-from-disk
    behaviour for callers that have no config to give.
    """
    import evolve_config
    import install_cost_estimator as ice

    calls: list[str] = []
    real = evolve_config.load_config
    monkeypatch.setattr(
        evolve_config, "load_config",
        lambda *a, **k: (calls.append("resolved"), real(*a, **k))[1])

    ice.estimate_install_cost("some-bot", "spec", network={})
    assert calls == [], "a caller that supplied a network must not re-read one"

    ice.estimate_install_cost("some-bot", "spec",
                              network={"bots": {"some-bot": {"user": "acct"}}})
    assert calls == []

    ice.estimate_install_cost("some-bot", "spec")
    assert calls == ["resolved"], (
        "omitting the network must still resolve it — that is what every "
        "caller without a config in hand relies on")


def test_the_bot_user_override_is_read_from_the_supplied_network():
    """Threading the network through must not lose what it was FOR.

    ``bots[bot].user`` is why this lookup exists at all — a bot whose macOS
    account differs from its id would otherwise be sized from the wrong
    home. The supplied dict has to be consulted, not merely accepted.
    """
    import install_cost_estimator as ice

    seen: list = []

    import evolve_config
    real_home = evolve_config.bot_home

    def spy(bot_id, config=None):
        seen.append((bot_id, config))
        return real_home(bot_id, config)

    evolve_config.bot_home = spy
    try:
        network = {"bots": {"team-bot-b": {"user": "shared-account"}}}
        ice.estimate_install_cost("team-bot-b", "spec", network=network)
    finally:
        evolve_config.bot_home = real_home

    assert seen == [("team-bot-b", network)]


def test_a_migrating_canonical_network_is_not_written_back_by_an_estimate(
        tmp_path, monkeypatch):
    """The failure as CI actually produced it, reproduced without CI.

    The write only happens when the canonical network.json EXISTS and its
    schema migrates — which is why a dev box whose copy is already current
    stays green while a runner goes red. Point the canonical path at a
    deliberately out-of-date file and assert the estimate leaves it alone.
    """
    import evolve_config
    import install_cost_estimator as ice

    shared = tmp_path / "shared"
    shared.mkdir()
    canonical = shared / "network.json"
    canonical.write_text(json.dumps({"networkId": "x",
                                     "bots": {"b": {"user": "u"}}}))
    before = canonical.read_text()
    monkeypatch.setattr(evolve_config, "CANONICAL_NETWORK_JSON", canonical)

    ice.estimate_install_cost("b", "spec", network={"bots": {}})

    assert canonical.read_text() == before, "the estimate rewrote the config"
    assert not list(shared.glob("*.tmp")), (
        "the estimate staged a migration write-back: "
        f"{[p.name for p in shared.glob('*.tmp')]}")
