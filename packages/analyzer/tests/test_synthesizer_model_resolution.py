"""Target/model selection for proposal_synthesizer — provider-agnostic (#3466).

Pins:

  - make_llm_caller (Phase 3) dispatches to whatever InfraLLMTarget the
    caller resolved via infra_llm — a non-Anthropic target (openai)
    flows through; no discard, no hardcoded fallback.
  - make_tool_using_caller (Phase 4) stays Anthropic-SDK-backed but
    accepts either the qualified or bare model form (the caller only
    enters this path when the resolved provider is Anthropic).
  - synthesize.main resolves via infra_llm: exits 1 with a clear
    "no LLM provider credentialed" message when nothing resolves; on a
    non-Anthropic pod the tool-using default DEGRADES to the prose-only
    Phase 3 path instead of dying.

Background: pre-#3466 the synthesizer resolved tiers through an
Anthropic-only helper that DISCARDED non-Anthropic tier models, and
synthesize.py hard-exited when no *Anthropic* key resolved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import infra_llm  # noqa: E402
from infra_llm import InfraLLMTarget  # noqa: E402
from proposal_synthesizer import synthesize, synthesizer  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# make_llm_caller (Phase 3) — target pass-through, provider-agnostic
# ─────────────────────────────────────────────────────────────────────────────


def _capture_complete(monkeypatch):
    calls: list[dict] = []

    def fake_complete(target, **kwargs):
        calls.append({"target": target, **kwargs})
        return "{}"

    monkeypatch.setattr(infra_llm, "complete", fake_complete)
    return calls


def test_phase3_caller_uses_resolved_target(monkeypatch):
    calls = _capture_complete(monkeypatch)
    target = InfraLLMTarget(
        provider="anthropic",
        model="anthropic/claude-haiku-4-5",
        api_key="sk-ant-fake-test-key",
    )
    caller = synthesizer.make_llm_caller(target)
    caller("system prompt", "user msg")

    assert calls[0]["target"] is target
    assert calls[0]["system"] == "system prompt"
    assert calls[0]["messages"] == [{"role": "user", "content": "user msg"}]
    assert calls[0]["max_tokens"] == synthesizer.DEFAULT_MAX_TOKENS


def test_phase3_caller_non_anthropic_target_flows_through(monkeypatch):
    """An openai target dispatches like any other — no discard-to-Claude."""
    calls = _capture_complete(monkeypatch)
    target = InfraLLMTarget(
        provider="openai", model="openai/gpt-4o-mini", api_key="sk-openai-fake"
    )
    caller = synthesizer.make_llm_caller(target)
    caller("system prompt", "user msg")

    assert calls[0]["target"].provider == "openai"


# ─────────────────────────────────────────────────────────────────────────────
# make_tool_using_caller (Phase 4) — Anthropic SDK, model form tolerance
# ─────────────────────────────────────────────────────────────────────────────


class _FakeAnthropic:
    """Anthropic SDK stand-in that records the model parameter."""

    captured_model: list[str] = []

    def __init__(self, api_key: str):
        self.messages = self

    def create(self, *, model, **_kwargs):
        type(self).captured_model.append(model)
        return type("R", (), {"content": [], "stop_reason": "end_turn"})()


@pytest.fixture(autouse=True)
def _fake_anthropic(monkeypatch):
    fake_mod = type(sys)("anthropic")
    fake_mod.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    _FakeAnthropic.captured_model = []
    yield


def test_phase4_caller_strips_qualified_prefix():
    """The tier registry hands out ``anthropic/<id>``; the SDK wants the
    bare id."""
    caller = synthesizer.make_tool_using_caller(
        "sk-test", "anthropic/claude-sonnet-4-6"
    )
    caller("system", [], [])
    assert _FakeAnthropic.captured_model == ["claude-sonnet-4-6"]


def test_phase4_caller_accepts_bare_model():
    caller = synthesizer.make_tool_using_caller("sk-test", "cli-pinned-id")
    caller("system", [], [])
    assert _FakeAnthropic.captured_model == ["cli-pinned-id"]


# ─────────────────────────────────────────────────────────────────────────────
# synthesize.main — resolution wiring + degrade paths
# ─────────────────────────────────────────────────────────────────────────────


def _pin_resolution(monkeypatch, target):
    monkeypatch.setattr(infra_llm, "resolve_infra_llm", lambda role, network=None: target)
    monkeypatch.setattr(
        infra_llm, "credentialed_target", lambda model, network=None: None
    )


def test_main_exits_1_when_no_provider(monkeypatch, tmp_path, capsys):
    _pin_resolution(monkeypatch, None)
    rc = synthesize.main(["--shared-dir", str(tmp_path)])
    assert rc == 1
    assert "no LLM provider credentialed" in capsys.readouterr().err


def test_main_no_tools_runs_phase3_on_openai_target(monkeypatch, tmp_path):
    """openai-only pod, --no-tools: Phase 3 runs against the openai
    target (the old path exited 1 without an Anthropic key)."""
    target = InfraLLMTarget(
        provider="openai", model="openai/gpt-4o-mini", api_key="sk-openai-fake"
    )
    _pin_resolution(monkeypatch, target)
    seen: dict = {}

    def fake_pending(shared_dir, *, llm_call):
        seen["llm_call"] = llm_call
        return synthesizer.SynthesizerStats()

    monkeypatch.setattr(synthesize, "synthesize_pending", fake_pending)
    rc = synthesize.main(["--shared-dir", str(tmp_path), "--no-tools", "--quiet"])
    assert rc == 0
    assert "llm_call" in seen


def test_main_tool_mode_degrades_to_phase3_on_non_anthropic(
    monkeypatch, tmp_path, capsys
):
    """Default (tool-using) mode on a non-Anthropic pod degrades to the
    prose-only Phase 3 path with a note — it must NOT die and must NOT
    call the Anthropic-SDK tool caller."""
    target = InfraLLMTarget(
        provider="openai", model="openai/gpt-4o-mini", api_key="sk-openai-fake"
    )
    _pin_resolution(monkeypatch, target)
    monkeypatch.setattr(
        synthesize, "make_tool_using_caller",
        lambda *a, **k: pytest.fail("tool caller must not run on a non-Anthropic pod"),
    )
    ran: dict = {}

    def fake_pending(shared_dir, *, llm_call):
        ran["phase3"] = True
        return synthesizer.SynthesizerStats()

    monkeypatch.setattr(synthesize, "synthesize_pending", fake_pending)
    rc = synthesize.main(["--shared-dir", str(tmp_path), "--quiet"])
    assert rc == 0
    assert ran.get("phase3")
    assert "tool-using agent unavailable" in capsys.readouterr().err


def test_main_tool_mode_uses_anthropic_tool_caller(monkeypatch, tmp_path):
    """Anthropic pod, default mode: the tool-using Phase 4 path runs with
    the resolved target's key + model."""
    target = InfraLLMTarget(
        provider="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-ant-fake-test-key",
    )
    _pin_resolution(monkeypatch, target)
    seen: dict = {}

    def fake_tool_caller(api_key, model, **kwargs):
        seen["api_key"] = api_key
        seen["model"] = model
        return lambda *a, **k: {}

    monkeypatch.setattr(synthesize, "make_tool_using_caller", fake_tool_caller)
    monkeypatch.setattr(
        synthesize, "synthesize_pending_with_tools",
        lambda shared_dir, *, llm_call: synthesizer.SynthesizerStats(),
    )
    rc = synthesize.main(["--shared-dir", str(tmp_path), "--quiet"])
    assert rc == 0
    assert seen == {
        "api_key": "sk-ant-fake-test-key",
        "model": "anthropic/claude-sonnet-4-6",
    }


def test_main_model_override_credentialed(monkeypatch, tmp_path):
    """--model with a credentialed provider-qualified id wins outright."""
    pinned = InfraLLMTarget(
        provider="anthropic",
        model="anthropic/claude-haiku-4-5",
        api_key="sk-ant-fake-test-key",
    )
    monkeypatch.setattr(
        infra_llm, "credentialed_target",
        lambda model, network=None: pinned if model == "anthropic/claude-haiku-4-5" else None,
    )
    monkeypatch.setattr(
        infra_llm, "resolve_infra_llm",
        lambda role, network=None: pytest.fail("resolver must not run for a credentialed --model"),
    )
    seen: dict = {}

    def fake_make_llm_caller(target, **k):
        seen["target"] = target
        return lambda *a: ""

    monkeypatch.setattr(synthesize, "make_llm_caller", fake_make_llm_caller)
    monkeypatch.setattr(
        synthesize, "synthesize_pending",
        lambda shared_dir, *, llm_call: synthesizer.SynthesizerStats(),
    )
    rc = synthesize.main([
        "--shared-dir", str(tmp_path), "--no-tools", "--quiet",
        "--model", "anthropic/claude-haiku-4-5",
    ])
    assert rc == 0
    assert seen["target"] is pinned
