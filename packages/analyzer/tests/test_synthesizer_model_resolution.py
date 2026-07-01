"""Tier-resolved model selection for proposal_synthesizer — A3 of the sweep.

Pins:

  - _resolve_pod_anthropic_tier strips the ``anthropic/`` prefix and
    returns the bare model id; non-Anthropic resolutions fall back to
    the provided literal with a warning.
  - make_anthropic_caller (Phase 3) defaults to tier3 via the resolver;
    make_tool_using_caller (Phase 4) defaults to tier2. Both use a
    sentinel so an explicit model string bypasses the resolver.
  - synthesize.py CLI's existing ``--model`` flag still works — it
    passes through as ``kwargs["model"]`` to the factory.

Background: synthesizer DEFAULT_MODEL (Haiku) and DEFAULT_AGENT_MODEL
(Sonnet) were hardcoded before A3 — pod-wide tier3/tier2 changes
were silently ignored on every cron run of the synthesizer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from proposal_synthesizer import synthesizer  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_pod_anthropic_tier — shared resolver for both phases
# ─────────────────────────────────────────────────────────────────────────────


def test_resolver_strips_anthropic_prefix(monkeypatch):
    """Tier registry stores ``anthropic/<model>``; synthesizer wants
    bare id (Anthropic SDK form)."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "anthropic/claude-haiku-4-5",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert (
        synthesizer._resolve_pod_anthropic_tier("tier3", "fallback-x")
        == "claude-haiku-4-5"
    )


def test_resolver_falls_back_on_non_anthropic(monkeypatch, caplog):
    """tier3 fallback chain hitting google/gemini → caller-supplied
    fallback + warning. Synthesizer SDK call can't dispatch non-Anthropic."""
    import logging
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "google/gemini-2.0-flash",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    with caplog.at_level(logging.WARNING):
        result = synthesizer._resolve_pod_anthropic_tier(
            "tier3", "my-fallback-id",
        )

    assert result == "my-fallback-id"
    assert "non-Anthropic" in caplog.text


def test_resolver_falls_back_on_broken_config(monkeypatch):
    """Resolver throwing → fallback. Cron path must never raise."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    def boom(*a, **kw):
        raise RuntimeError("config unreachable")

    monkeypatch.setattr("models.resolve_tier", boom)
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert (
        synthesizer._resolve_pod_anthropic_tier("tier3", "fallback-id")
        == "fallback-id"
    )


def test_resolver_does_not_thread_bot_id(monkeypatch):
    """Synthesizer is pod-wide work (evolve-bot universal role). The
    resolver must NOT pass bot_id — that would inadvertently apply a
    single bot's tier_assignments override to a pod-wide synthesis run."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    seen: list[tuple] = []

    def fake_resolve(tier, config, bot_id=None):
        seen.append((tier, bot_id))
        return "anthropic/claude-haiku-4-5"

    monkeypatch.setattr("models.resolve_tier", fake_resolve)
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    synthesizer._resolve_pod_anthropic_tier("tier3", "fallback-id")
    assert seen == [("tier3", None)]


# ─────────────────────────────────────────────────────────────────────────────
# make_anthropic_caller (Phase 3) — defaults to tier3
# ─────────────────────────────────────────────────────────────────────────────


class _FakeAnthropic:
    """Anthropic SDK stand-in that records the model parameter."""

    captured_model: list[str] = []

    def __init__(self, api_key: str):
        self.messages = self

    def create(self, *, model, **_kwargs):
        type(self).captured_model.append(model)
        return type("R", (), {"content": []})()


@pytest.fixture(autouse=True)
def _fake_anthropic(monkeypatch):
    fake_mod = type(sys)("anthropic")
    fake_mod.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    _FakeAnthropic.captured_model = []
    yield


def test_phase3_caller_default_resolves_tier3(monkeypatch):
    """Phase 3 (synthesis, no tools) — default model comes from tier3
    resolver, not the hardcoded literal."""
    monkeypatch.setattr(
        synthesizer, "_resolve_pod_anthropic_tier",
        lambda tier, fallback: (
            "claude-haiku-4-5-resolved" if tier == "tier3"
            else pytest.fail(f"expected tier3, got {tier}")
        ),
    )
    caller = synthesizer.make_anthropic_caller("sk-test")
    caller("system", "user")

    assert _FakeAnthropic.captured_model == ["claude-haiku-4-5-resolved"]


def test_phase3_explicit_model_skips_resolver(monkeypatch):
    """``--model`` from synthesize.py CLI passes through as
    kwargs["model"] — must bypass the tier3 resolver."""
    monkeypatch.setattr(
        synthesizer, "_resolve_pod_anthropic_tier",
        lambda tier, fallback: pytest.fail("resolver must not run with explicit model"),
    )
    caller = synthesizer.make_anthropic_caller("sk-test", model="cli-pinned-id")
    caller("system", "user")

    assert _FakeAnthropic.captured_model == ["cli-pinned-id"]


# ─────────────────────────────────────────────────────────────────────────────
# make_tool_using_caller (Phase 4) — defaults to tier2
# ─────────────────────────────────────────────────────────────────────────────


def test_phase4_caller_default_resolves_tier2(monkeypatch):
    """Phase 4 (tool-using investigation loop) — default model comes
    from tier2 resolver. tier2 is the workhorse tier; tool-use needs
    sustained reasoning across turns and the Phase 3 cheap-tier would
    underperform here."""
    seen: list[str] = []

    def fake_resolver(tier, fallback):
        seen.append(tier)
        return "claude-sonnet-4-6-resolved"

    monkeypatch.setattr(
        synthesizer, "_resolve_pod_anthropic_tier", fake_resolver,
    )
    # make_tool_using_caller invokes the SDK lazily — to trigger the
    # default-model resolution we just need to construct it.
    synthesizer.make_tool_using_caller("sk-test")
    assert seen == ["tier2"]


def test_phase4_explicit_model_skips_resolver(monkeypatch):
    monkeypatch.setattr(
        synthesizer, "_resolve_pod_anthropic_tier",
        lambda tier, fallback: pytest.fail("resolver must not run with explicit model"),
    )
    # No assertion needed beyond "no resolver call" — pytest.fail in the
    # patch fires the assertion if it runs.
    synthesizer.make_tool_using_caller("sk-test", model="cli-pinned-id")
