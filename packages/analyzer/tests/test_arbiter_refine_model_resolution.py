"""Model/target resolution for arbiter.refine — provider-agnostic (#3466 PR-3).

Pins:

  - _resolve_refine_model returns the PROVIDER-QUALIFIED tier3 model
    (pod tier3, per-bot overrides respected) — no prefix stripping, no
    discard of non-Anthropic providers, "" on broken config.
  - make_llm_caller honors the pinned model when its provider is
    credentialed; walks to pod-level infra_llm resolution otherwise;
    raises RuntimeError when NO provider is credentialed.
  - A non-Anthropic target (e.g. openai) flows through infra_llm.complete
    — the old "discard to Claude" guard is gone.
  - Explicit model override bypasses the tier resolver entirely.

Background: pre-#3466 refine.py called the Anthropic SDK directly and
DISCARDED a correctly configured non-Anthropic tier3 model, falling back
to a hardcoded Claude literal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import infra_llm  # noqa: E402
from arbiter import refine  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_refine_model — tier3 lookup, qualified form, no discard
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_refine_model_returns_qualified_id(monkeypatch):
    """The resolver returns the provider-qualified model — infra_llm
    transports strip their own prefixes."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "anthropic/claude-haiku-4-6",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert refine._resolve_refine_model(bot_id="atlas") == "anthropic/claude-haiku-4-6"


def test_resolve_refine_model_keeps_non_anthropic(monkeypatch):
    """A non-Anthropic tier3 resolution is RETURNED, not discarded — the
    old Anthropic-SDK guard is gone (#3466)."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "openai/gpt-4o-mini",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert refine._resolve_refine_model(bot_id="atlas") == "openai/gpt-4o-mini"


def test_resolve_refine_model_empty_on_broken_config(monkeypatch):
    """Resolver throwing (import failure / broken config) → "" so the
    caller walks to pod-level resolution. Never raises."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    def boom(*a, **kw):
        raise RuntimeError("config unreachable in this test")

    monkeypatch.setattr("models.resolve_tier", boom)
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert refine._resolve_refine_model(bot_id="atlas") == ""


def test_resolve_refine_model_threads_per_bot_id(monkeypatch):
    """bot_id reaches resolve_tier so per-bot tier_assignments overrides
    work for refine on one bot but not another."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    seen: list[str | None] = []

    def fake_resolve(tier, config, bot_id=None):
        seen.append(bot_id)
        # team_bot_a has been pinned to tier2 (Sonnet) for refine parity with build
        return (
            "anthropic/claude-sonnet-4-6"
            if bot_id == "team_bot_a"
            else "anthropic/claude-haiku-4-5"
        )

    monkeypatch.setattr("models.resolve_tier", fake_resolve)
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert refine._resolve_refine_model(bot_id="atlas") == "anthropic/claude-haiku-4-5"
    assert refine._resolve_refine_model(bot_id="team_bot_a") == "anthropic/claude-sonnet-4-6"
    assert seen == ["atlas", "team_bot_a"]


# ─────────────────────────────────────────────────────────────────────────────
# make_llm_caller — credentialed-pin-or-walk + no-provider raise
# ─────────────────────────────────────────────────────────────────────────────


def _keys(monkeypatch, keys: dict[str, str]) -> None:
    """Pin the primary-bot key reader (used by both credentialed_target
    and resolve_infra_llm) to a fixed provider→key map."""
    import primary_bot  # noqa: F401

    monkeypatch.setattr(
        "primary_bot.read_primary_bot_llm_keys", lambda network=None: dict(keys)
    )
    monkeypatch.setattr("primary_bot._load_network_default", lambda: {})


def _capture_complete(monkeypatch):
    calls: list[dict] = []

    def fake_complete(target, **kwargs):
        calls.append({"target": target, **kwargs})
        return '{"problem": "p", "admin_surface_summary": "s"}'

    monkeypatch.setattr(infra_llm, "complete", fake_complete)
    return calls


def test_make_caller_honors_credentialed_pin(monkeypatch):
    """Pinned tier3 model whose provider is credentialed → used as-is."""
    _keys(monkeypatch, {"anthropic": "sk-ant-fake-test-key"})
    monkeypatch.setattr(
        refine, "_resolve_refine_model", lambda bot_id: "anthropic/claude-haiku-4-5"
    )
    calls = _capture_complete(monkeypatch)

    caller = refine.make_llm_caller(bot_id="atlas")
    caller("user message")

    assert len(calls) == 1
    assert calls[0]["target"].model == "anthropic/claude-haiku-4-5"
    assert calls[0]["target"].api_key == "sk-ant-fake-test-key"
    assert calls[0]["system"] == refine._SYSTEM_PROMPT


def test_make_caller_non_anthropic_target_flows_through(monkeypatch):
    """An openai-only pod refines via the openai target — no discard."""
    _keys(monkeypatch, {"openai": "sk-openai-fake-test-key"})
    monkeypatch.setattr(
        refine, "_resolve_refine_model", lambda bot_id: "openai/gpt-4o-mini"
    )
    calls = _capture_complete(monkeypatch)

    caller = refine.make_llm_caller(bot_id="atlas")
    caller("user message")

    assert calls[0]["target"].provider == "openai"
    assert calls[0]["target"].model == "openai/gpt-4o-mini"


def test_make_caller_walks_past_uncredentialed_pin(monkeypatch):
    """Pin resolves to a provider with no key → pod-level resolution wins
    (never returns a key-less target)."""
    _keys(monkeypatch, {"openai": "sk-openai-fake-test-key"})
    monkeypatch.setattr(
        refine, "_resolve_refine_model", lambda bot_id: "anthropic/claude-haiku-4-5"
    )
    calls = _capture_complete(monkeypatch)

    caller = refine.make_llm_caller(bot_id="atlas")
    caller("user message")

    assert calls[0]["target"].provider == "openai"
    assert calls[0]["target"].api_key == "sk-openai-fake-test-key"


def test_make_caller_raises_when_no_provider(monkeypatch):
    """No provider credentialed anywhere → RuntimeError with a clear
    operator-facing message (callers surface it)."""
    _keys(monkeypatch, {})
    monkeypatch.setattr(refine, "_resolve_refine_model", lambda bot_id: "")

    with pytest.raises(RuntimeError, match="no LLM provider credentialed"):
        refine.make_llm_caller(bot_id="atlas")


def test_make_caller_explicit_model_skips_resolver(monkeypatch):
    """An explicit model must bypass the tier3 resolver — eval harnesses
    pin a specific comparison model."""
    _keys(monkeypatch, {"anthropic": "sk-ant-fake-test-key"})
    monkeypatch.setattr(
        refine, "_resolve_refine_model",
        lambda bot_id: pytest.fail("resolver should not run when explicit model is given"),
    )
    calls = _capture_complete(monkeypatch)

    caller = refine.make_llm_caller(
        bot_id="atlas", model="anthropic/claude-explicit-override"
    )
    caller("user message")

    assert calls[0]["target"].model == "anthropic/claude-explicit-override"
