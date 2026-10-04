"""observations.llm_extractor on infra_llm (#3466 PR-4 migration).

Pins the migrated contract:
  - make_llm_extractor dispatches through infra_llm.complete with the
    resolved target (non-Anthropic flow-through proven via a fake
    transport against an openai target),
  - API/parse failures degrade to [] (no tuples), never raise,
  - wire_default_extractor stays on the stub when no LLM provider is
    credentialed and wires the target when one resolves.

Every key here is an obvious fake — never a real credential.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from infra_llm import InfraLLMTarget  # noqa: E402
from observations import extract as ext  # noqa: E402
from observations import llm_extractor as llx  # noqa: E402
from observations.extract import (  # noqa: E402
    ExtractorVocabulary,
    Transcript,
    Turn,
)


_FAKE_KEY = "sk-test-not-a-real-key"


@pytest.fixture(autouse=True)
def _reset_extractor_seam():
    yield
    ext.reset_extractor()


def _transcript(text: str = "Outcome: fixed the boiler schedule") -> Transcript:
    return Transcript(
        bot_id="atlas",
        session_id="s-1",
        turns=[Turn(role="user", text=text, timestamp="2026-07-29T00:00:00Z")],
    )


def _vocab() -> ExtractorVocabulary:
    return ExtractorVocabulary.from_active_nouns(["heating"])


def test_extractor_openai_flow_through():
    """#3466: the extractor works verbatim on a non-Anthropic provider —
    request shape goes to the openai-compatible endpoint, system prompt in
    the system slot, and the openai response shape parses to tuples."""
    calls: list[tuple[str, dict, dict]] = []
    payload = {"tuples": [{
        "noun": "heating", "verb": "configuring", "mood": None,
        "engagement": 5, "segment_id": "seg-0", "confidence": 0.9,
    }]}

    def transport(url, headers, body):
        calls.append((url, dict(headers), body))
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    target = InfraLLMTarget("openai", "openai/gpt-4o-mini", _FAKE_KEY)
    extract_fn = llx.make_llm_extractor(target, transport=transport)
    rows = extract_fn(_transcript(), _vocab())
    assert rows == payload["tuples"]

    url, headers, body = calls[0]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {_FAKE_KEY}"
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0]["role"] == "system"
    assert "Outcome: fixed the boiler schedule" in body["messages"][1]["content"]


def test_extractor_api_error_degrades_to_no_tuples():
    def boom(url, headers, body):
        raise RuntimeError("connection refused")

    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    extract_fn = llx.make_llm_extractor(target, transport=boom)
    assert extract_fn(_transcript(), _vocab()) == []


def test_extractor_empty_transcript_short_circuits():
    calls = []

    def transport(url, headers, body):
        calls.append(url)
        return 200, {}

    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    extract_fn = llx.make_llm_extractor(target, transport=transport)
    empty = Transcript(bot_id="atlas", session_id="s-2", turns=[])
    assert extract_fn(empty, _vocab()) == []
    assert calls == []


def test_wire_default_extractor_no_provider_stays_on_stub(monkeypatch):
    monkeypatch.setattr(llx, "resolve_infra_llm", lambda role: None)
    assert llx.wire_default_extractor() is False


def test_wire_default_extractor_wires_resolved_target(monkeypatch):
    target = InfraLLMTarget("openai", "openai/gpt-4o-mini", _FAKE_KEY)
    monkeypatch.setattr(llx, "resolve_infra_llm", lambda role: target)
    wired = {}
    monkeypatch.setattr(
        llx, "set_extractor", lambda fn: wired.setdefault("fn", fn))
    assert llx.wire_default_extractor() is True
    assert callable(wired["fn"])
