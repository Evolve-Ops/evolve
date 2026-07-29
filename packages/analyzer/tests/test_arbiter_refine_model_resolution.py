"""Tier3 model resolution for arbiter.refine — A2 of the tier-resolve sweep.

Pins:

  - make_anthropic_caller defaults its model to a sentinel resolved at
    construction time via _resolve_refine_model — pod tier3, per-bot
    overrides respected.
  - Tier3 ids are returned in the bare Anthropic form (no "anthropic/"
    prefix), because refine.py calls the Anthropic SDK directly.
  - Non-Anthropic tier3 (e.g. fallback to openai/gpt-4o-mini) yields the
    hardcoded literal with a warning — the SDK can't dispatch those.
  - Broken-config fallback lands on the hardcoded literal.
  - Explicit model override (string) bypasses the resolver entirely.

Background: refine.py's DEFAULT_MODEL was hardcoded Haiku before A2,
ignoring any pod tier3 changes operators might have configured.
Same bug shape as bot_forge.py before PR #1769 and forge_engine.py
before A1.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import refine  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_refine_model — tier3 lookup + Anthropic-prefix stripping
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_refine_model_strips_anthropic_prefix(monkeypatch):
    """resolve_tier returns ``anthropic/<model>``; refine.py calls the
    Anthropic SDK which wants the bare id."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "anthropic/claude-haiku-4-6",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert refine._resolve_refine_model(bot_id="atlas") == "claude-haiku-4-6"


def test_resolve_refine_model_falls_back_on_non_anthropic(monkeypatch, caplog):
    """tier3 fallback chain hitting openai/gpt-4o-mini → hardcoded literal
    + warning, because refine.py uses the Anthropic SDK directly."""
    import logging
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "openai/gpt-4o-mini",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    with caplog.at_level(logging.WARNING):
        result = refine._resolve_refine_model(bot_id="atlas")

    assert result == refine._DEFAULT_MODEL_FALLBACK
    assert "non-Anthropic" in caplog.text


def test_resolve_refine_model_falls_back_on_broken_config(monkeypatch):
    """Resolver throwing (import failure / broken config) → hardcoded
    literal. Refine path must never raise from the resolver."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    def boom(*a, **kw):
        raise RuntimeError("config unreachable in this test")

    monkeypatch.setattr("models.resolve_tier", boom)
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert (
        refine._resolve_refine_model(bot_id="atlas")
        == refine._DEFAULT_MODEL_FALLBACK
    )


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

    assert refine._resolve_refine_model(bot_id="atlas") == "claude-haiku-4-5"
    assert refine._resolve_refine_model(bot_id="team_bot_a") == "claude-sonnet-4-6"
    assert seen == ["atlas", "team_bot_a"]


# ─────────────────────────────────────────────────────────────────────────────
# make_anthropic_caller — sentinel-default + explicit override
# ─────────────────────────────────────────────────────────────────────────────


class _FakeAnthropic:
    """Minimal Anthropic SDK stand-in that captures the model param."""

    captured_model: list[str] = []

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.messages = self

    def create(self, *, model, **_kwargs):
        type(self).captured_model.append(model)
        return type("R", (), {"content": []})()


@pytest.fixture(autouse=True)
def _fake_anthropic(monkeypatch):
    """Swap out the Anthropic SDK so tests don't hit the network."""
    fake_mod = type(sys)("anthropic")
    fake_mod.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    _FakeAnthropic.captured_model = []
    yield


def test_make_caller_default_resolves_tier3_via_bot_id(monkeypatch):
    """No explicit model → resolver runs at construction time with the
    bot_id passed in."""
    seen_bot: list[str | None] = []

    def fake_resolver(bot_id):
        seen_bot.append(bot_id)
        return "claude-haiku-4-5-from-resolver"

    monkeypatch.setattr(refine, "_resolve_refine_model", fake_resolver)
    caller = refine.make_anthropic_caller("sk-test", bot_id="atlas")
    caller("user message")  # trigger SDK call

    assert seen_bot == ["atlas"]
    assert _FakeAnthropic.captured_model == ["claude-haiku-4-5-from-resolver"]


def test_make_caller_explicit_string_skips_resolver(monkeypatch):
    """An explicit model string must bypass the tier3 resolver — eval
    harnesses or tests need to pin a specific model."""
    monkeypatch.setattr(
        refine, "_resolve_refine_model",
        lambda bot_id: pytest.fail("resolver should not run when explicit model is given"),
    )
    caller = refine.make_anthropic_caller(
        "sk-test", bot_id="atlas", model="anthropic-explicit-override",
    )
    caller("user message")

    assert _FakeAnthropic.captured_model == ["anthropic-explicit-override"]


def test_make_caller_missing_bot_id_still_calls_resolver(monkeypatch):
    """bot_id=None is legitimate (the refine caller didn't have one).
    Resolver receives None; pod-wide tier3 stands."""
    seen_bot: list[str | None] = []

    def fake_resolver(bot_id):
        seen_bot.append(bot_id)
        return "claude-haiku-4-5"

    monkeypatch.setattr(refine, "_resolve_refine_model", fake_resolver)
    caller = refine.make_anthropic_caller("sk-test")
    caller("user message")

    assert seen_bot == [None]
