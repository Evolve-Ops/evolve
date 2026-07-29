"""Tests for the Haiku-backed prompt-injection verifier.

Covers:
  - JSON parsing (clean, fenced, prose-prefixed, malformed)
  - API error → ambiguous (fail-closed)
  - Confidence clamping
  - Wiring fallback when SDK / key is missing
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.security_warden.scanners import llm_verifier as ver  # noqa: E402
from generators.security_warden.scanners import prompt_injection as inj  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    yield
    inj.reset_llm_verifier()
    ver._reset_for_test()


def _mock_client(response_text: str):
    """Build a fake Anthropic client whose messages.create returns response_text."""
    block = MagicMock()
    block.type = "text"
    block.text = response_text

    response = MagicMock()
    response.content = [block]

    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _matches_for(*pattern_ids: str) -> list[inj.InjectionMatch]:
    return [
        inj.InjectionMatch(pattern_id=p, start=0, end=10, match_text="placeholder")
        for p in pattern_ids
    ]


def test_parse_clean_json():
    verify = ver.make_haiku_verifier(
        _mock_client('{"verdict": "injection", "confidence": 0.92, "rationale": "clear override"}')
    )
    result = verify("ignore previous instructions", _matches_for("ignore_previous_instructions"))
    assert result.verdict == "injection"
    assert result.confidence == 0.92
    assert "clear override" in result.rationale


def test_parse_fenced_json():
    verify = ver.make_haiku_verifier(
        _mock_client('```json\n{"verdict": "legitimate", "confidence": 0.8, "rationale": "quoting"}\n```')
    )
    result = verify("text", _matches_for("dan_jailbreak"))
    assert result.verdict == "legitimate"
    assert result.confidence == 0.8


def test_parse_prose_prefix():
    verify = ver.make_haiku_verifier(
        _mock_client(
            'Sure — here is my classification.\n\n'
            '{"verdict": "ambiguous", "confidence": 0.55, "rationale": "context-dep"}'
        )
    )
    result = verify("text", _matches_for("system_prompt_marker"))
    assert result.verdict == "ambiguous"


def test_parse_malformed_response_returns_ambiguous():
    verify = ver.make_haiku_verifier(_mock_client("totally not json"))
    result = verify("text", _matches_for("ignore_previous_instructions"))
    assert result.verdict == "ambiguous"
    assert result.confidence == 0.0
    assert "parse_failure" in result.rationale


def test_unknown_verdict_normalized_to_ambiguous():
    verify = ver.make_haiku_verifier(
        _mock_client('{"verdict": "definitely_yes", "confidence": 0.99}')
    )
    result = verify("text", _matches_for("dan_jailbreak"))
    assert result.verdict == "ambiguous"


def test_confidence_clamped_to_unit_interval():
    verify = ver.make_haiku_verifier(
        _mock_client('{"verdict": "injection", "confidence": 1.7}')
    )
    result = verify("text", _matches_for("dan_jailbreak"))
    assert result.confidence == 1.0

    verify_neg = ver.make_haiku_verifier(
        _mock_client('{"verdict": "injection", "confidence": -0.3}')
    )
    result = verify_neg("text", _matches_for("dan_jailbreak"))
    assert result.confidence == 0.0


def test_api_error_returns_ambiguous_low_confidence():
    """Fail closed: an API error must not flood the operator with critical proposals."""
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("connection refused")

    verify = ver.make_haiku_verifier(client)
    result = verify("ignore previous instructions", _matches_for("ignore_previous_instructions"))
    assert result.verdict == "ambiguous"
    assert result.confidence == 0.0
    assert "api_error" in result.rationale


def test_no_matches_short_circuits_to_legitimate():
    """If somehow called without matches, don't bother the API."""
    client = MagicMock()
    verify = ver.make_haiku_verifier(client)
    result = verify("anything", [])
    assert result.verdict == "legitimate"
    assert client.messages.create.call_count == 0


def test_long_input_gets_truncated_in_user_message():
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        block = MagicMock()
        block.type = "text"
        block.text = '{"verdict": "injection", "confidence": 0.9}'
        resp = MagicMock()
        resp.content = [block]
        return resp

    client = MagicMock()
    client.messages.create.side_effect = fake_create

    long_text = "ignore previous instructions " * 200  # ~6kB
    verify = ver.make_haiku_verifier(client)
    verify(long_text, _matches_for("ignore_previous_instructions"))

    user_msg = captured["messages"][0]["content"]
    assert "[...truncated...]" in user_msg
    # Original was ~6kB; we ship at most 2kB + a small wrapper
    assert len(user_msg) < 3000


def test_wire_default_verifier_no_key_returns_false(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ver, "_AUTH_PROFILES_PATH", tmp_path / "no-such-file.json")
    assert ver.wire_default_verifier() is False


def test_wire_default_verifier_is_idempotent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Pretend SDK works on first attempt
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    assert ver.wire_default_verifier() is True
    # Second call is a no-op (returns False, doesn't re-import)
    assert ver.wire_default_verifier() is False
