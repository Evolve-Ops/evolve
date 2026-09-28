"""forge_cost_guard — pre-dispatch projection + message-cap signals.

Background: the 2026-06-03 incident burned $33.65 in a single OC
round-trip — 8.8M cache_write tokens at the end of a 312-message
runaway agent loop. The existing daily_cap breaker (PR #1483) detected
the spend ~3ms after the API returned but couldn't claw back the cost.
forge_cost_guard adds two preventive layers:

  * Guard A — pre-dispatch worst-case cost projection. Refuse a
    dispatch before any LLM tokens are billed when the model + cap
    math allows a per-turn or per-dispatch worst case above the cap.
  * Guard B — OC ``--max-turns`` flag wired into every forge dispatch
    via ``_build_agent_cmd``. Caps the agent loop; emits a Signal
    when the cap is hit.

These tests cover the pure logic in ``forge_cost_guard`` — config
loading, projection math, refusal decisions, and signal payload shape.
The bot_forge integration (subprocess argv + Signal emission on
cap-hit) is covered separately in
``test_bot_forge_model_selection.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import forge_cost_guard as fcg  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# load_caps — config resolution + source tracking
# ─────────────────────────────────────────────────────────────────────────────


def test_load_caps_defaults_when_unset():
    """Empty / missing config → module defaults, each tagged ``default``."""
    caps = fcg.load_caps("team-bot-a", {})
    assert caps.message_cap == fcg.DEFAULT_MESSAGE_CAP
    assert caps.per_turn_cap_usd == pytest.approx(fcg.DEFAULT_PER_TURN_CAP_USD)
    assert caps.per_dispatch_cap_usd == pytest.approx(
        fcg.DEFAULT_PER_DISPATCH_CAP_USD,
    )
    assert caps.message_cap_source == "default"
    assert caps.per_turn_cap_source == "default"
    assert caps.per_dispatch_cap_source == "default"


def test_load_caps_none_network():
    """None network treated identically to empty dict."""
    caps = fcg.load_caps("team-bot-a", None)
    assert caps.message_cap == fcg.DEFAULT_MESSAGE_CAP


def test_load_caps_pod_defaults_apply():
    """``forge.defaults`` is the pod-wide fallback when no per-bot override."""
    net = {
        "forge": {
            "defaults": {
                "message_cap": 30,
                "per_turn_cap_usd": 3.0,
                "per_dispatch_cap_usd": 15.0,
            },
        },
    }
    caps = fcg.load_caps("team-bot-a", net)
    assert caps.message_cap == 30
    assert caps.per_turn_cap_usd == pytest.approx(3.0)
    assert caps.per_dispatch_cap_usd == pytest.approx(15.0)
    assert caps.message_cap_source == "forge.defaults.message_cap"
    assert caps.per_turn_cap_source == "forge.defaults.per_turn_cap_usd"


def test_load_caps_per_bot_overrides_pod_defaults():
    """``bots.<bot>.forge.<key>`` wins over ``forge.defaults.<key>``."""
    net = {
        "forge": {"defaults": {"message_cap": 30, "per_turn_cap_usd": 3.0}},
        "bots": {
            "team-bot-a": {
                "forge": {"message_cap": 10, "per_turn_cap_usd": 1.5},
            },
        },
    }
    caps = fcg.load_caps("team-bot-a", net)
    assert caps.message_cap == 10
    assert caps.per_turn_cap_usd == pytest.approx(1.5)
    assert caps.message_cap_source == "bots.team-bot-a.forge.message_cap"
    assert caps.per_turn_cap_source == "bots.team-bot-a.forge.per_turn_cap_usd"


def test_load_caps_per_bot_partial_override_falls_through():
    """One per-bot key overrides; the other falls through to pod default."""
    net = {
        "forge": {"defaults": {"message_cap": 30}},
        "bots": {"team-bot-a": {"forge": {"per_turn_cap_usd": 2.0}}},
    }
    caps = fcg.load_caps("team-bot-a", net)
    assert caps.message_cap == 30
    assert caps.message_cap_source == "forge.defaults.message_cap"
    assert caps.per_turn_cap_usd == pytest.approx(2.0)
    assert caps.per_turn_cap_source == "bots.team-bot-a.forge.per_turn_cap_usd"


def test_load_caps_rejects_zero_and_negative_values():
    """A zero or negative value falls through as if unset.

    Operators can't accidentally disable a guard by writing ``0`` —
    that would be the worst-case failure mode. Same convention as
    spend_alert._resolve_per_bot_cap (≤ 0 means None).
    """
    net = {
        "bots": {
            "team-bot-a": {
                "forge": {
                    "message_cap": 0,
                    "per_turn_cap_usd": -1.0,
                    "per_dispatch_cap_usd": 0.0,
                },
            },
        },
    }
    caps = fcg.load_caps("team-bot-a", net)
    assert caps.message_cap == fcg.DEFAULT_MESSAGE_CAP
    assert caps.per_turn_cap_usd == pytest.approx(fcg.DEFAULT_PER_TURN_CAP_USD)
    assert caps.per_dispatch_cap_usd == pytest.approx(
        fcg.DEFAULT_PER_DISPATCH_CAP_USD,
    )


def test_load_caps_rejects_non_numeric():
    """Stringy / null values fall through to defaults."""
    net = {
        "bots": {
            "team-bot-a": {
                "forge": {
                    "message_cap": "fifty",
                    "per_turn_cap_usd": None,
                },
            },
        },
    }
    caps = fcg.load_caps("team-bot-a", net)
    assert caps.message_cap == fcg.DEFAULT_MESSAGE_CAP
    assert caps.per_turn_cap_usd == pytest.approx(fcg.DEFAULT_PER_TURN_CAP_USD)


# ─────────────────────────────────────────────────────────────────────────────
# project_worst_case — pricing math
# ─────────────────────────────────────────────────────────────────────────────


def test_projection_sonnet_default_200k_context():
    """Sonnet 4.6 + 50 turns + 8K output = worst per-turn ~$1.47.

    Math: 200K * ($3/MTok input + $3.75/MTok cache_write)
        + 8K * $15/MTok output
        = 0.60 + 0.75 + 0.12 = $1.47/turn
    """
    p = fcg.project_worst_case(
        "anthropic/claude-sonnet-4-6",
        message_cap=50,
        max_output_tokens=8_192,
    )
    assert p is not None
    assert p.max_context_tokens == 200_000
    assert p.per_turn_usd == pytest.approx(0.60 + 0.75 + 0.12288, rel=1e-3)
    assert p.dispatch_total_usd == pytest.approx(p.per_turn_usd * 50)


def test_projection_haiku_cheaper_than_sonnet():
    """Haiku-4-5: cache_write $1/MTok (vs Sonnet $3.75) — ~3.4× cheaper.

    Concrete: 200K * ($0.80 + $1.00) + 8K * $4 = 0.16 + 0.20 + 0.0328 = $0.39/turn
    Locks in the expected ordering across model tiers.
    """
    sonnet = fcg.project_worst_case(
        "anthropic/claude-sonnet-4-6", message_cap=50,
    )
    haiku = fcg.project_worst_case(
        "anthropic/claude-haiku-4-5", message_cap=50,
    )
    assert sonnet is not None and haiku is not None
    assert haiku.per_turn_usd < sonnet.per_turn_usd
    assert haiku.per_turn_usd == pytest.approx(0.16 + 0.20 + 0.032768, rel=1e-3)


def test_projection_opus_above_per_turn_cap():
    """Opus worst-case per-turn exceeds the $5 default — Guard A WILL refuse.

    Math: 200K * ($15 + $18.75) + 8K * $75
        = 3.00 + 3.75 + 0.60 = $7.35/turn > $5
    Demonstrates the guard's intended bite: switching a forge dispatch to
    Opus (or similar high-tier) without raising the cap should fail
    pre-dispatch, surfacing the cost surprise to the operator.
    """
    p = fcg.project_worst_case(
        "anthropic/claude-opus-4-6", message_cap=50,
    )
    assert p is not None
    assert p.per_turn_usd > 5.0


def test_projection_model_none_uses_sonnet_class_fallback():
    """When the caller passes ``model=None`` (use bot's default), the
    guard projects against the anthropic provider fallback (Sonnet rates).

    Forge dispatch_build / dispatch_refine pass model=None to let OC
    inherit the bot's agent default. The guard can't read openclaw.json
    cheaply from here, so it conservatively uses Sonnet pricing — the
    common bot-default tier — for projection. That's correct
    fail-safe direction (assume expensive).
    """
    p_none = fcg.project_worst_case(None, message_cap=50)
    p_sonnet = fcg.project_worst_case(
        "anthropic/claude-sonnet-4-6", message_cap=50,
    )
    assert p_none is not None and p_sonnet is not None
    assert p_none.per_turn_usd == pytest.approx(p_sonnet.per_turn_usd)


def test_projection_unknown_provider_returns_none():
    """Non-Anthropic model with no fallback entry → skip Guard A (return None)."""
    p = fcg.project_worst_case("nonsense/no-such-model", message_cap=50)
    assert p is None


def test_projection_total_scales_linearly_with_message_cap():
    """Dispatch total = per-turn × message_cap — the linear scaling
    is what makes raising message_cap a Guard-A-tripping action.
    """
    p10 = fcg.project_worst_case(
        "anthropic/claude-sonnet-4-6", message_cap=10,
    )
    p100 = fcg.project_worst_case(
        "anthropic/claude-sonnet-4-6", message_cap=100,
    )
    assert p10 is not None and p100 is not None
    assert p10.per_turn_usd == pytest.approx(p100.per_turn_usd)
    assert p100.dispatch_total_usd == pytest.approx(p10.dispatch_total_usd * 10)


# ─────────────────────────────────────────────────────────────────────────────
# evaluate_pre_dispatch — Guard A decision boundary
# ─────────────────────────────────────────────────────────────────────────────


def _net(message_cap=50, per_turn=5.0, per_dispatch=1000.0):
    """Shorthand network.json fragment with per-bot caps for ``team-bot-a``.

    per_dispatch defaults high so per-turn is the dominant lever; tests
    that want to exercise per_dispatch override it explicitly.
    """
    return {
        "bots": {
            "team-bot-a": {
                "forge": {
                    "message_cap": message_cap,
                    "per_turn_cap_usd": per_turn,
                    "per_dispatch_cap_usd": per_dispatch,
                },
            },
        },
    }


def test_evaluate_allows_sonnet_at_default_caps():
    """Sonnet 4.6 at $5/turn cap → allowed (worst per-turn $1.47)."""
    d = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-sonnet-4-6",
        network=_net(),
        job_id="j-1",
        kind="build",
    )
    assert d.allowed is True
    assert d.signal_payload is None
    assert d.projection is not None


def test_evaluate_refuses_opus_at_default_cap():
    """Opus + $5/turn cap → refused (worst per-turn $7.35 > $5).

    This is the load-bearing test: a single configuration change (Opus
    + default cap) trips Guard A without any LLM cost being incurred.
    """
    d = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-opus-4-6",
        network=_net(),
        job_id="j-1",
        kind="build",
    )
    assert d.allowed is False
    assert d.failed_check == "per_turn"
    assert d.signal_payload is not None
    assert d.signal_payload["type"] == "forge_turn_cost_projected_excessive"
    assert "per-turn" in d.reason
    assert "claude-opus-4-6" in d.reason


def test_evaluate_refuses_when_per_dispatch_total_exceeds():
    """Even sub-per-turn dispatches refuse when N × per_turn > dispatch cap.

    Sonnet worst per-turn ≈ $1.47. With message_cap=100, total worst ≈
    $147. Setting per_dispatch_cap_usd=50 trips the per-dispatch arm
    while leaving per-turn ($5) untriggered. Mirrors what a long-running
    refine cycle could do.
    """
    d = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-sonnet-4-6",
        network=_net(message_cap=100, per_turn=5.0, per_dispatch=50.0),
        job_id="j-1",
        kind="refine",
    )
    assert d.allowed is False
    assert d.failed_check == "per_dispatch"
    assert d.signal_payload is not None
    details = d.signal_payload["details"]
    assert details["failed_check"] == "per_dispatch"
    assert details["projected_dispatch_total_usd"] > 50.0


def test_evaluate_allows_unknown_model_skips_guard():
    """Non-priced model → projection None → Guard A skipped (allowed).

    Documents the intentional gap: the guard only refuses dispatches it
    can price. Non-Anthropic / unknown models flow through; the daily
    cap + L1 breaker is the safety net there. As pricing tables grow,
    coverage grows.
    """
    d = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="nonsense/model",
        network=_net(),
        job_id="j-1",
        kind="build",
    )
    assert d.allowed is True
    assert d.projection is None


def test_evaluate_uses_per_bot_cap_when_pod_default_is_loose():
    """Per-bot cap of $1/turn refuses Sonnet at $1.47 worst per-turn.

    Exercises the per-bot override path: same model that's allowed under
    the $5 default is refused once the operator lowers the cap on a
    specific bot.
    """
    d = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-sonnet-4-6",
        network=_net(per_turn=1.0),
        job_id="j-1",
        kind="build",
    )
    assert d.allowed is False
    assert d.failed_check == "per_turn"
    assert d.signal_payload["details"]["cap_source"] == (
        "bots.team-bot-a.forge.per_turn_cap_usd"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Signal payload shape — what reaches signals.store.observe()
# ─────────────────────────────────────────────────────────────────────────────


def test_refusal_signal_payload_has_required_observe_fields():
    """The payload must be directly hand-offable to signals.store.observe().

    Mirrors the field set produced by cost_watchdog's emitters —
    signature, producer, type, flavor, severity, scope, bot_id, title,
    body, details. Missing one of these would fail at observe() time on
    a real pod. Test catches schema drift early.
    """
    d = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-opus-4-6",
        network=_net(),
        job_id="j-9",
        kind="build",
    )
    assert d.signal_payload is not None
    for required in (
        "signature", "producer", "type", "flavor", "severity",
        "scope", "bot_id", "title", "body", "details",
    ):
        assert required in d.signal_payload, f"missing field: {required}"
    assert d.signal_payload["producer"] == fcg.PRODUCER
    assert d.signal_payload["bot_id"] == "team-bot-a"
    assert d.signal_payload["details"]["job_id"] == "j-9"


def test_refusal_signature_distinguishes_per_turn_from_per_dispatch():
    """A bot refused for per_turn AND per_dispatch on consecutive runs
    yields TWO distinct signatures so the Alerts UI shows both firings.

    Same signature would mean one alert overwrites the other in the
    signal store dedup map.
    """
    d_pt = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-opus-4-6",
        network=_net(),
        job_id="j-1",
        kind="build",
    )
    d_pd = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-sonnet-4-6",
        network=_net(message_cap=100, per_turn=5.0, per_dispatch=50.0),
        job_id="j-2",
        kind="refine",
    )
    assert d_pt.signal_payload["signature"] != d_pd.signal_payload["signature"]


def test_refusal_signature_dedups_across_repeated_refusals():
    """Two refusals of the same bot for the same check → same signature.

    Ensures the Alerts page collapses repeat refusals into a single
    firing; the operator sees ``occurrences`` climb rather than a stack
    of identical alerts.
    """
    d1 = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-opus-4-6",
        network=_net(),
        job_id="j-1",
        kind="build",
    )
    d2 = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-opus-4-6",
        network=_net(),
        job_id="j-2",
        kind="refine",
    )
    assert d1.signal_payload["signature"] == d2.signal_payload["signature"]


def test_message_cap_signal_payload_shape():
    """Same observe()-ready field set for Guard B signals."""
    payload = fcg.build_message_cap_signal(
        bot_id="team-bot-a",
        job_id="j-99",
        kind="build",
        message_cap=50,
        cap_source="bots.team-bot-a.forge.message_cap",
        model="anthropic/claude-sonnet-4-6",
        agent_exit_code=137,
        stderr_tail="max turns reached, exiting",
    )
    for required in (
        "signature", "producer", "type", "flavor", "severity",
        "scope", "bot_id", "title", "body", "details",
    ):
        assert required in payload
    assert payload["type"] == "forge_session_message_cap_exceeded"
    assert payload["details"]["agent_exit_code"] == 137
    assert payload["details"]["message_cap"] == 50
    assert "max turns reached" in payload["details"]["stderr_tail"]


def test_2026_06_03_scenario_now_capped_at_dispatch_time():
    """End-to-end shape: a Sonnet dispatch with message_cap=312 (the 2026-06-03
    incident's actual depth) and the $25 per-dispatch default is refused.

    The math: 312 turns × ~$1.47/turn worst case = ~$459 worst case >
    $25 default. Documents that the 2026-06-03 incident shape can no longer
    fly past the guard without an explicit cap raise.
    """
    d = fcg.evaluate_pre_dispatch(
        bot_id="team-bot-a",
        model="anthropic/claude-sonnet-4-6",
        network={
            "bots": {
                "team-bot-a": {
                    "forge": {"message_cap": 312},
                },
            },
        },
        job_id="j-team-bot-a-2026-06-03",
        kind="build",
    )
    assert d.allowed is False
    assert d.failed_check == "per_dispatch"
    assert d.signal_payload["details"]["projected_dispatch_total_usd"] > 25.0
