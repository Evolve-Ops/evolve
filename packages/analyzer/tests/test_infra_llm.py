"""infra_llm — the provider-agnostic client for Evolve's own LLM calls (#3466 PR-2).

Pins the PR-3 migration contract:
  * per-provider request SHAPE (url, auth header, body) via a fake transport,
  * response parsing per provider,
  * retry-once semantics on 429 / 5xx / "overloaded",
  * resolve_infra_llm across credential scenarios — tier config wins when
    credentialed, the walk NEVER lands on an uncredentialed provider, and a
    pod with no LLM keys resolves to None (callers keep degrade paths),
  * API keys never appear in str(InfraLLMError) or repr(InfraLLMTarget),
  * read_primary_bot_llm_keys (provider-field keyed, lastGood preferred,
    env overrides).

Every key here is an obvious fake — never a real credential.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ANALYZER_DIR = Path(__file__).resolve().parents[1]
if str(ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYZER_DIR))

import infra_llm  # noqa: E402
import primary_bot  # noqa: E402
from infra_llm import (  # noqa: E402
    InfraLLMError,
    InfraLLMTarget,
    complete,
    resolve_infra_llm,
)

_FAKE_KEY = "sk-test-not-a-real-key"
_NET = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}

_ALL_KEY_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _no_ambient_state(monkeypatch):
    """No ambient provider keys, no real backoff sleeps, no real pod reads."""
    for var in _ALL_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(infra_llm, "RETRY_BACKOFF_SECONDS", 0.0)
    # resolve_infra_llm(network=None) must never read the deploy box's
    # network.json from inside a test.
    monkeypatch.setattr(primary_bot, "_load_network_default", lambda: dict(_NET))


class FakeTransport:
    """Records every (url, headers, body) and replays scripted responses."""

    def __init__(self, *responses: tuple[int, dict]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, body):
        self.calls.append((url, dict(headers), json.loads(json.dumps(body))))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _anthropic_ok(text="the answer"):
    return (200, {"content": [{"type": "text", "text": text}]})


def _openai_ok(text="the answer"):
    return (200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


def _google_ok(text="the answer"):
    return (200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


# ── Per-provider request shape + parsing ──────────────────────────────────────


def test_anthropic_request_shape_and_parse():
    t = FakeTransport(_anthropic_ok())
    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    out = complete(target, prompt="hi", system="be brief", max_tokens=99,
                   temperature=0.5, transport=t)
    assert out == "the answer"
    url, headers, body = t.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == _FAKE_KEY
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers
    assert body["model"] == "claude-haiku-4-5"  # provider prefix stripped
    assert body["system"] == "be brief"
    assert body["max_tokens"] == 99
    assert body["temperature"] == 0.5
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_multi_block_parse_and_no_system_key_when_empty():
    t = FakeTransport((200, {"content": [
        {"type": "text", "text": "a"},
        {"type": "tool_use", "id": "x"},
        {"type": "text", "text": "b"},
    ]}))
    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    assert complete(target, prompt="hi", transport=t) == "ab"
    assert "system" not in t.calls[0][2]


def test_openai_request_shape_and_parse():
    t = FakeTransport(_openai_ok())
    target = InfraLLMTarget("openai", "openai/gpt-4o-mini", _FAKE_KEY)
    out = complete(target, prompt="hi", system="be brief", transport=t)
    assert out == "the answer"
    url, headers, body = t.calls[0]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {_FAKE_KEY}"
    assert "x-api-key" not in headers
    assert body["model"] == "gpt-4o-mini"
    # system rides as the first chat message
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_xai_uses_xai_base():
    t = FakeTransport(_openai_ok())
    target = InfraLLMTarget("xai", "xai/grok-3-mini", _FAKE_KEY)
    complete(target, prompt="hi", transport=t)
    url, headers, body = t.calls[0]
    assert url == "https://api.x.ai/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {_FAKE_KEY}"
    assert body["model"] == "grok-3-mini"


def test_moonshot_uses_moonshot_base():
    t = FakeTransport(_openai_ok())
    target = InfraLLMTarget("moonshot", "moonshot/kimi-k2.6", _FAKE_KEY)
    complete(target, prompt="hi", transport=t)
    url, headers, body = t.calls[0]
    assert url == "https://api.moonshot.ai/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {_FAKE_KEY}"
    assert body["model"] == "kimi-k2.6"


def test_custom_base_url_openai_compatible():
    t = FakeTransport(_openai_ok())
    target = InfraLLMTarget("myproxy", "myproxy/some-model", _FAKE_KEY,
                            base_url="https://llm.example.test/v1/")
    complete(target, prompt="hi", transport=t)
    url, headers, body = t.calls[0]
    assert url == "https://llm.example.test/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {_FAKE_KEY}"
    assert body["model"] == "some-model"


def test_unknown_provider_without_base_url_raises():
    target = InfraLLMTarget("mystery", "mystery/model-x", _FAKE_KEY)
    with pytest.raises(InfraLLMError, match="mystery"):
        complete(target, prompt="hi", transport=FakeTransport(_openai_ok()))


def test_google_request_shape_and_parse():
    t = FakeTransport(_google_ok())
    target = InfraLLMTarget("google", "google/gemini-2.0-flash", _FAKE_KEY)
    out = complete(
        target,
        messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
        system="be brief",
        max_tokens=77,
        transport=t,
    )
    assert out == "the answer"
    url, headers, body = t.calls[0]
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={_FAKE_KEY}"
    )
    assert "Authorization" not in headers and "x-api-key" not in headers
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert body["generationConfig"]["maxOutputTokens"] == 77
    # role mapping: assistant → model
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][1]["parts"] == [{"text": "a1"}]


def test_prompt_and_messages_are_mutually_exclusive():
    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    with pytest.raises(ValueError):
        complete(target, transport=FakeTransport(_anthropic_ok()))
    with pytest.raises(ValueError):
        complete(target, prompt="hi", messages=[{"role": "user", "content": "hi"}],
                 transport=FakeTransport(_anthropic_ok()))


# ── Retry semantics ───────────────────────────────────────────────────────────


def test_retry_once_on_429_then_success():
    t = FakeTransport((429, {"error": "rate limited"}), _anthropic_ok())
    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    assert complete(target, prompt="hi", transport=t) == "the answer"
    assert len(t.calls) == 2


def test_retry_exhausts_after_second_5xx():
    t = FakeTransport((500, {"error": "boom"}), (503, {"error": "still down"}))
    target = InfraLLMTarget("openai", "openai/gpt-4o-mini", _FAKE_KEY)
    with pytest.raises(InfraLLMError) as ei:
        complete(target, prompt="hi", transport=t)
    assert len(t.calls) == 2
    assert "openai" in str(ei.value) and "503" in str(ei.value)


def test_no_retry_on_400():
    t = FakeTransport((400, {"error": {"type": "invalid_request_error"}}))
    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    with pytest.raises(InfraLLMError, match="400"):
        complete(target, prompt="hi", transport=t)
    assert len(t.calls) == 1


def test_overloaded_error_body_triggers_retry():
    t = FakeTransport((400, {"error": {"type": "overloaded_error"}}), _anthropic_ok())
    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    assert complete(target, prompt="hi", transport=t) == "the answer"
    assert len(t.calls) == 2


def test_unexpected_response_shape_raises_infra_error():
    t = FakeTransport((200, {"totally": "unexpected"}))
    target = InfraLLMTarget("openai", "openai/gpt-4o-mini", _FAKE_KEY)
    with pytest.raises(InfraLLMError, match="unexpected response shape"):
        complete(target, prompt="hi", transport=t)


# ── Key redaction ─────────────────────────────────────────────────────────────


def test_error_never_contains_api_key():
    # Provider echoes the key back in an error body — it must be redacted.
    t = FakeTransport((401, {"error": f"bad key {_FAKE_KEY} rejected"}))
    target = InfraLLMTarget("anthropic", "anthropic/claude-haiku-4-5", _FAKE_KEY)
    with pytest.raises(InfraLLMError) as ei:
        complete(target, prompt="hi", transport=t)
    assert _FAKE_KEY not in str(ei.value)
    assert _FAKE_KEY not in repr(ei.value)
    assert "401" in str(ei.value)


def test_target_repr_redacts_key():
    target = InfraLLMTarget("openai", "openai/gpt-4o-mini", _FAKE_KEY)
    assert _FAKE_KEY not in repr(target)
    assert _FAKE_KEY not in str(target)
    assert "openai/gpt-4o-mini" in repr(target)


# ── resolve_infra_llm ─────────────────────────────────────────────────────────


def _with_keys(monkeypatch, keys: dict[str, str]):
    monkeypatch.setattr(
        primary_bot, "read_primary_bot_llm_keys", lambda network=None: dict(keys)
    )


def test_resolve_anthropic_only_pod(monkeypatch):
    _with_keys(monkeypatch, {"anthropic": _FAKE_KEY})
    target = resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None
    assert target.provider == "anthropic"
    assert target.model.startswith("anthropic/")
    assert target.api_key == _FAKE_KEY


def test_resolve_openai_only_pod(monkeypatch):
    _with_keys(monkeypatch, {"openai": _FAKE_KEY})
    for role in ("fast", "standard"):
        target = resolve_infra_llm(role, network=dict(_NET))
        assert target is not None, role
        assert target.provider == "openai"
        assert target.model.startswith("openai/")


def test_resolve_walks_to_credentialed_provider_not_in_default_chain(monkeypatch):
    # xai appears in no DEFAULT_TIERS chain — resolution must walk the
    # credentialed set via derive_default_tiers, not give up (and not
    # presume some other provider).
    _with_keys(monkeypatch, {"xai": _FAKE_KEY})
    target = resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None
    assert target.provider == "xai"
    assert target.model.startswith("xai/")


def test_resolve_none_when_uncredentialed(monkeypatch):
    _with_keys(monkeypatch, {})
    assert resolve_infra_llm("fast", network=dict(_NET)) is None
    assert resolve_infra_llm("standard", network=dict(_NET)) is None


def test_resolve_none_when_only_non_llm_keys(monkeypatch):
    # A brave-search key is not an LLM credential — no target, no presumption.
    _with_keys(monkeypatch, {"brave-search": _FAKE_KEY})
    assert resolve_infra_llm("fast", network=dict(_NET)) is None


def test_resolve_tier_config_wins_when_credentialed(monkeypatch):
    # Both providers credentialed; the pod's tier3 is pinned to an OpenAI
    # model → tier config wins over the default (anthropic-first) picks.
    _with_keys(monkeypatch, {"anthropic": "sk-test-ant", "openai": "sk-test-oai"})
    monkeypatch.setattr(
        primary_bot, "bot_tier_models",
        lambda network, bot_id, tier: ["openai/gpt-4o-mini"] if tier == "tier3" else [],
    )
    target = resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None
    assert (target.provider, target.model) == ("openai", "openai/gpt-4o-mini")
    assert target.api_key == "sk-test-oai"


def test_resolve_never_returns_uncredentialed_tier_model(monkeypatch):
    # Tier pinned to an Anthropic model but the pod only has an OpenAI key:
    # the anthropic entry is walked PAST (never returned key-less), landing
    # on a credentialed provider instead. This is the discard-to-claude
    # anti-pattern, inverted.
    _with_keys(monkeypatch, {"openai": _FAKE_KEY})
    monkeypatch.setattr(
        primary_bot, "bot_tier_models",
        lambda network, bot_id, tier: ["anthropic/claude-haiku-4-5"] if tier == "tier3" else [],
    )
    target = resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None
    assert target.provider == "openai"


def test_resolve_engine_default_tier_collapse(monkeypatch):
    # cascade.engine_default_tier pins engine calls to tier3 — a "standard"
    # request resolves the tier3 (fast) model, mirroring models.resolve_tier.
    _with_keys(monkeypatch, {"anthropic": _FAKE_KEY})
    net = dict(_NET)
    net["cascade"] = {"engine_default_tier": "tier3"}
    target = resolve_infra_llm("standard", network=net)
    assert target is not None
    from models import DEFAULT_TIERS
    assert target.model == DEFAULT_TIERS["tier3"]["models"][0]


def test_model_override_qualified_credentialed_wins(monkeypatch):
    # An operator pin naming a credentialed provider binds fully — model
    # AND provider — regardless of what tier config says.
    _with_keys(monkeypatch, {"anthropic": "sk-test-ant", "openai": "sk-test-oai"})
    target = resolve_infra_llm(
        "fast", network=dict(_NET), model_override="openai/gpt-4.1-mini"
    )
    assert target is not None
    assert (target.provider, target.model) == ("openai", "openai/gpt-4.1-mini")
    assert target.api_key == "sk-test-oai"


def test_model_override_qualified_uncredentialed_ignored(monkeypatch):
    # A pin naming a provider without a key must never produce an
    # uncredentialed target — normal resolution proceeds instead.
    _with_keys(monkeypatch, {"anthropic": _FAKE_KEY})
    target = resolve_infra_llm(
        "fast", network=dict(_NET), model_override="openai/gpt-4o-mini"
    )
    assert target is not None
    assert target.provider == "anthropic"
    assert "gpt" not in target.model


def test_model_override_bare_pins_model_on_resolved_provider(monkeypatch):
    # The historic env-knob form is a bare model id — it rides on whatever
    # provider resolution lands on (legacy behavior on 1-provider pods).
    _with_keys(monkeypatch, {"anthropic": _FAKE_KEY})
    target = resolve_infra_llm(
        "fast", network=dict(_NET), model_override="claude-haiku-4-5-20251001"
    )
    assert target is not None
    assert target.provider == "anthropic"
    assert target.model == "claude-haiku-4-5-20251001"


def test_resolve_env_override_credentials_end_to_end(monkeypatch, tmp_path):
    # No auth files at all; OPENAI_API_KEY in the environment is enough for
    # the real reader → resolver path to produce an openai target.
    monkeypatch.setattr(primary_bot, "primary_bot_home", lambda network: tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", _FAKE_KEY)
    target = resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None
    assert target.provider == "openai"
    assert target.api_key == _FAKE_KEY


# ── credentialed_target ───────────────────────────────────────────────────────


def test_credentialed_target_honors_credentialed_pin(monkeypatch):
    _with_keys(monkeypatch, {"openai": _FAKE_KEY})
    from infra_llm import credentialed_target

    target = credentialed_target("openai/gpt-4o-mini", network=dict(_NET))
    assert target is not None
    assert (target.provider, target.model, target.api_key) == (
        "openai", "openai/gpt-4o-mini", _FAKE_KEY,
    )


def test_credentialed_target_none_for_bare_or_uncredentialed(monkeypatch):
    # Bare id: provider unknowable — never presume. Uncredentialed
    # provider: never return a key-less target. Empty: nothing to pin.
    _with_keys(monkeypatch, {"openai": _FAKE_KEY})
    from infra_llm import credentialed_target

    assert credentialed_target("claude-haiku-4-5", network=dict(_NET)) is None
    assert credentialed_target("anthropic/claude-haiku-4-5", network=dict(_NET)) is None
    assert credentialed_target("", network=dict(_NET)) is None


# ── read_primary_bot_llm_keys ─────────────────────────────────────────────────


def _write_auth(home: Path, payload: dict) -> None:
    d = home / ".openclaw" / "agents" / "main" / "agent"
    d.mkdir(parents=True, exist_ok=True)
    (d / "auth-profiles.json").write_text(json.dumps(payload))


@pytest.fixture
def primary_home(tmp_path, monkeypatch):
    monkeypatch.setattr(primary_bot, "primary_bot_home", lambda network: tmp_path)
    return tmp_path


def test_llm_keys_reads_every_provider_by_provider_field(primary_home):
    # Profile ids are deliberately unhelpful — extraction keys on the
    # explicit "provider" field, not id substrings.
    _write_auth(primary_home, {
        "profiles": {
            "profile-a": {"type": "api_key", "provider": "anthropic", "key": "sk-test-ant"},
            "profile-b": {"type": "api_key", "provider": "openai", "key": "sk-test-oai"},
        },
    })
    keys = primary_bot.read_primary_bot_llm_keys(_NET)
    assert keys == {"anthropic": "sk-test-ant", "openai": "sk-test-oai"}


def test_llm_keys_lastgood_profile_preferred(primary_home):
    _write_auth(primary_home, {
        "profiles": {
            "openai:old": {"type": "api_key", "provider": "openai", "key": "sk-test-old"},
            "openai:new": {"type": "api_key", "provider": "openai", "key": "sk-test-good"},
        },
        "lastGood": {"openai": "openai:new"},
    })
    keys = primary_bot.read_primary_bot_llm_keys(_NET)
    assert keys["openai"] == "sk-test-good"


def test_llm_keys_api_key_preferred_over_token_but_token_only_included(primary_home):
    _write_auth(primary_home, {
        "profiles": {
            "anthropic:oauth": {"type": "token", "provider": "anthropic", "token": "tok-test-oauth"},
            "anthropic:api": {"type": "api_key", "provider": "anthropic", "key": "sk-test-ant"},
            "google:oauth": {"type": "token", "provider": "google", "token": "tok-test-goog"},
        },
    })
    keys = primary_bot.read_primary_bot_llm_keys(_NET)
    assert keys["anthropic"] == "sk-test-ant"      # api_key beats token
    assert keys["google"] == "tok-test-goog"        # token-only still surfaces


def test_llm_keys_env_overrides_per_provider(primary_home, monkeypatch):
    _write_auth(primary_home, {
        "profiles": {
            "openai:api": {"type": "api_key", "provider": "openai", "key": "sk-test-file"},
        },
    })
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-env")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-gem")
    keys = primary_bot.read_primary_bot_llm_keys(_NET)
    assert keys["openai"] == "sk-test-env"   # env beats file, same provider only
    assert keys["google"] == "sk-test-gem"   # GEMINI_API_KEY maps to google


def test_llm_keys_google_api_key_env_fallback(primary_home, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-test-goog2")
    keys = primary_bot.read_primary_bot_llm_keys(_NET)
    assert keys["google"] == "sk-test-goog2"


def test_llm_keys_empty_when_no_source(primary_home):
    assert primary_bot.read_primary_bot_llm_keys(_NET) == {}


def test_old_anthropic_reader_left_intact(primary_home):
    # PR-3 retires callers; until then the anthropic-specific reader must
    # keep working unchanged alongside the generalization.
    _write_auth(primary_home, {
        "profiles": {
            "anthropic:api_key": {"type": "api_key", "provider": "anthropic", "key": "sk-test-ant"},
        },
    })
    assert primary_bot.read_primary_bot_anthropic_key(_NET) == "sk-test-ant"
    assert primary_bot.read_primary_bot_llm_keys(_NET)["anthropic"] == "sk-test-ant"


def test_provider_key_env_assignments_only_credentialed(primary_home):
    # NAME=value pairs for the sudo/SETENV boundary — one per credentialed
    # provider, canonical env-var name, nothing for absent providers.
    _write_auth(primary_home, {
        "profiles": {
            "anthropic:api_key": {"type": "api_key", "provider": "anthropic", "key": "sk-test-ant"},
            "google:api_key": {"type": "api_key", "provider": "google", "key": "sk-test-goog"},
        },
    })
    out = primary_bot.provider_key_env_assignments(_NET)
    assert "ANTHROPIC_API_KEY=sk-test-ant" in out
    assert "GEMINI_API_KEY=sk-test-goog" in out
    assert len(out) == 2


def test_provider_key_env_assignments_empty_when_no_source(primary_home):
    assert primary_bot.provider_key_env_assignments(_NET) == []
