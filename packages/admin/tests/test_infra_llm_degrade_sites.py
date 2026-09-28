"""Admin-side degrade-path sites on infra_llm (#3466 PR-4 migration).

One place pinning, for each migrated admin degrade site, both halves of
the contract:

  * target=None (no LLM provider credentialed) → the site's existing
    graceful degrade, with a provider-neutral message ("no LLM provider
    credentialed" — not "no Anthropic API key"), and
  * a NON-Anthropic resolved target flows through infra_llm verbatim
    (the feature lights up on any credentialed provider).

Sites: intake classifier/reviser/triager, wizard bot-id suggestion,
evo inspector haiku confirmer, evo wizard extractor + intent stage-2.
Every key here is an obvious fake — never a real credential.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import infra_llm as _infra  # noqa: E402
from infra_llm import InfraLLMTarget  # noqa: E402

from evolve_admin.intake import classifier as cls  # noqa: E402


_FAKE_KEY = "sk-test-not-a-real-key"


def _openai_target() -> InfraLLMTarget:
    return InfraLLMTarget("openai", "openai/gpt-4o-mini", _FAKE_KEY)


def _fake_complete(response_text: str, captured: dict):
    def fake(target, *, prompt=None, messages=None, system="", max_tokens=0,
             temperature=0.0, timeout=0, transport=None):
        captured.update(target=target, prompt=prompt, system=system,
                        max_tokens=max_tokens, timeout=timeout)
        return response_text

    return fake


# ── intake classifier / reviser / triager ────────────────────────────────────


def test_classifier_degrades_provider_neutral(monkeypatch):
    monkeypatch.setattr(cls, "_resolve_target", lambda: None)
    v = cls._default_classifier("something broke", cls.ClassificationContext())
    assert v.category == "local_env"
    assert v.confidence == 0.0
    assert "no LLM provider credentialed" in v.reasoning
    assert "Anthropic" not in v.reasoning


def test_classifier_flows_through_non_anthropic_target(monkeypatch):
    captured: dict = {}
    canned = json.dumps({
        "category": "evolve_code", "confidence": 0.8,
        "draft_title": "t", "draft_body": "b", "reasoning": "r",
    })
    monkeypatch.setattr(cls, "_resolve_target", _openai_target)
    monkeypatch.setattr(_infra, "complete", _fake_complete(canned, captured))
    v = cls._default_classifier("the applier crashed", cls.ClassificationContext())
    assert v.category == "evolve_code"
    assert captured["target"].provider == "openai"
    assert "the applier crashed" in captured["prompt"]


def test_reviser_degrades_provider_neutral(monkeypatch):
    monkeypatch.setattr(cls, "_resolve_target", lambda: None)
    v = cls._default_reviser("Title", "Body", "make it shorter",
                             cls.ClassificationContext())
    assert (v.new_title, v.new_body) == ("Title", "Body")
    assert "no LLM provider credentialed" in v.reasoning


def test_triager_degrades_provider_neutral(monkeypatch):
    monkeypatch.setattr(cls, "_resolve_target", lambda: None)
    v = cls._default_triager("t", "b", "o/r", "author",
                             cls.ClassificationContext())
    assert v.confidence == 0.0
    assert "no LLM provider credentialed" in v.reasoning


# ── wizard bot-id suggestion ─────────────────────────────────────────────────


def test_bot_id_suggestion_heuristic_when_no_provider(monkeypatch):
    from evolve_admin.web import wizard_routes as wr

    monkeypatch.setattr(_infra, "resolve_infra_llm", lambda role: None)
    out = wr._suggest_bot_id_with_optional_llm("research bot for openclaw fans")
    assert out == "research"  # the heuristic


def test_bot_id_suggestion_via_non_anthropic_target(monkeypatch):
    from evolve_admin.web import wizard_routes as wr

    captured: dict = {}
    monkeypatch.setattr(_infra, "complete", _fake_complete("news_watch", captured))
    out = wr.suggest_bot_id_via_llm("watch the news for me", _openai_target())
    assert out == "news_watch"
    assert captured["target"].provider == "openai"


def test_bot_id_suggestion_falls_back_on_call_error(monkeypatch):
    from evolve_admin.web import wizard_routes as wr

    def boom(*a, **kw):
        raise RuntimeError("transport down")

    monkeypatch.setattr(_infra, "complete", boom)
    out = wr.suggest_bot_id_via_llm("research bot for openclaw fans",
                                    _openai_target())
    assert out == "research"


# ── evo inspector haiku confirmer ────────────────────────────────────────────


def test_inspector_defaults_to_no_without_provider(monkeypatch):
    from evolve_admin.evo import inspector as insp

    monkeypatch.setattr(insp, "_resolve_haiku_target", lambda: None)
    verdict = insp._default_haiku_fn("some assistant response")
    assert verdict.branch == "no"
    assert verdict.reason == "no_llm_provider"


def test_inspector_flows_through_non_anthropic_target(monkeypatch):
    from evolve_admin.evo import inspector as insp

    captured: dict = {}
    monkeypatch.setattr(insp, "_resolve_haiku_target", _openai_target)
    monkeypatch.setattr(
        _infra, "complete", _fake_complete("yes/d: evidence-free fix", captured))
    verdict = insp._default_haiku_fn("sudo rm -rf everything, trust me")
    assert verdict.branch == "yes/d"
    assert captured["target"].provider == "openai"
    assert captured["timeout"] == insp._HAIKU_TIMEOUT_S


def test_inspector_env_override_reaches_resolver(monkeypatch):
    from evolve_admin.evo import inspector as insp

    captured: dict = {}

    def fake_resolve(role, *, network=None, model_override=""):
        captured["role"] = role
        captured["override"] = model_override
        return None

    monkeypatch.setattr(_infra, "resolve_infra_llm", fake_resolve)
    monkeypatch.setenv("EVOLVE_INSPECTOR_HAIKU_MODEL", "openai/gpt-4o-mini")
    assert insp._resolve_haiku_target() is None
    assert captured == {"role": "fast", "override": "openai/gpt-4o-mini"}


# ── evo wizard extractor + intent stage-2 ────────────────────────────────────


def test_wizard_extractor_empty_when_no_provider(monkeypatch):
    from evolve_admin.evo.wizard import extractor as ext
    from evolve_admin.evo.wizard.phases import FieldSpec

    monkeypatch.setattr(ext, "_resolve_target", lambda **kw: None)
    out = ext._default_extractor(
        "my name is Morgan", (FieldSpec("name", "the user's name"),), {})
    assert out == {}


def test_wizard_extractor_flows_through_non_anthropic_target(monkeypatch):
    from evolve_admin.evo.wizard import extractor as ext
    from evolve_admin.evo.wizard.phases import FieldSpec

    captured: dict = {}
    monkeypatch.setattr(ext, "_resolve_target", lambda **kw: _openai_target())
    monkeypatch.setattr(
        _infra, "complete", _fake_complete(json.dumps({"name": "Morgan"}), captured))
    out = ext._default_extractor(
        "my name is Morgan", (FieldSpec("name", "the user's name"),), {})
    assert out == {"name": "Morgan"}
    assert captured["target"].provider == "openai"


def test_intent_stage2_degrades_provider_neutral(monkeypatch):
    from evolve_admin.evo.wizard import intent as itt

    monkeypatch.setattr(itt, "_resolve_intent_target", lambda: None)
    res = itt._default_intent_parser("yes please", "pitch", False)
    assert res.action == "unknown"
    assert res.stage == "fallback"
    assert "no LLM provider credentialed" in res.rationale


def test_intent_stage2_flows_through_non_anthropic_target(monkeypatch):
    from evolve_admin.evo.wizard import intent as itt

    captured: dict = {}
    canned = json.dumps({
        "action": "accept", "confidence": 0.9, "rationale": "clear yes"})
    monkeypatch.setattr(itt, "_resolve_intent_target", _openai_target)
    monkeypatch.setattr(_infra, "complete", _fake_complete(canned, captured))
    res = itt._default_intent_parser("yes please", "pitch", False)
    assert res.action == "accept"
    assert captured["target"].provider == "openai"


def test_intent_env_pin_chain_reaches_resolver(monkeypatch):
    from evolve_admin.evo.wizard import intent as itt

    captured: dict = {}

    def fake_resolve(role, *, network=None, model_override=""):
        captured["override"] = model_override
        return None

    monkeypatch.setattr(_infra, "resolve_infra_llm", fake_resolve)
    monkeypatch.delenv("EVOLVE_INTENT_MODEL", raising=False)
    monkeypatch.setenv("EVOLVE_WIZARD_EXTRACTOR_MODEL", "openai/gpt-4o-mini")
    assert itt._resolve_intent_target() is None
    assert captured["override"] == "openai/gpt-4o-mini"

    monkeypatch.setenv("EVOLVE_INTENT_MODEL", "xai/grok-3-mini")
    itt._resolve_intent_target()
    assert captured["override"] == "xai/grok-3-mini"
