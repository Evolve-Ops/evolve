"""tests/test_embeddings.py — pure-Python tests for embeddings registry/resolver.

Covers:
  - Provider registry shape (id matches dict key, prices for default models)
  - configured_embedding_providers gates on credentials
  - resolve_embedding_chain merge order (defaults → pod → per-bot)
  - memory_search_block trims to OpenClaw's {provider, fallback} shape
  - embedding_chain_warning surfaces fragile-chain conditions
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from embeddings import (  # noqa: E402
    DEFAULT_EMBEDDING_CHAIN,
    EMBEDDING_PRICING,
    EMBEDDING_PROVIDERS,
    configured_embedding_providers,
    embedding_capable_providers,
    embedding_chain_warning,
    is_embedding_capable,
    memory_search_block,
    model_for_provider,
    providers_from_auth_profiles,
    resolve_embedding_chain,
)


# ── Registry shape ───────────────────────────────────────────────────────────


def test_registry_id_matches_key():
    for key, prov in EMBEDDING_PROVIDERS.items():
        assert prov.id == key, f"{key} → {prov.id} mismatch"


def test_registry_covers_expected_providers():
    """Spec calls out gemini, openai, voyage, copilot, mistral, bedrock, local, ollama."""
    expected = {"gemini", "openai", "voyage", "github-copilot",
                "mistral", "bedrock", "local", "ollama"}
    assert set(EMBEDDING_PROVIDERS.keys()) >= expected


def test_no_chat_only_providers_in_embedding_registry():
    """Anthropic + xAI have no embedding products — must not appear."""
    assert "anthropic" not in EMBEDDING_PROVIDERS
    assert "xai" not in EMBEDDING_PROVIDERS


def test_each_default_model_has_pricing_entry():
    """Every provider's default model must be priced (even if 0.0 for local)."""
    for prov in EMBEDDING_PROVIDERS.values():
        key = f"{prov.id}/{prov.default_model}"
        assert key in EMBEDDING_PRICING, f"Missing pricing for {key}"


def test_default_chain_uses_known_providers():
    for pid in DEFAULT_EMBEDDING_CHAIN:
        assert pid in EMBEDDING_PROVIDERS, f"Default chain references unknown {pid}"


def test_is_embedding_capable():
    assert is_embedding_capable("gemini")
    assert is_embedding_capable("openai")
    assert not is_embedding_capable("anthropic")
    assert not is_embedding_capable("nonexistent")


def test_capable_providers_returns_sorted():
    result = embedding_capable_providers()
    assert result == sorted(result)


# ── Credential gating ────────────────────────────────────────────────────────


def test_configured_includes_no_key_providers_unconditionally():
    """local/copilot/bedrock/ollama should be available with empty network.json."""
    result = configured_embedding_providers({})
    assert "local" in result
    assert "github-copilot" in result
    assert "bedrock" in result
    assert "ollama" in result


def test_configured_omits_keyed_providers_without_credentials():
    result = configured_embedding_providers({})
    assert "gemini" not in result
    assert "openai" not in result
    assert "voyage" not in result


def test_configured_includes_keyed_providers_when_present():
    config = {"models": {"providers": {"openai": {"key": "sk-..."}}}}
    assert "openai" in configured_embedding_providers(config)


def test_configured_recognizes_credential_aliases():
    """gemini accepts both 'google' and 'gemini' as credential keys."""
    via_google = {"models": {"providers": {"google": {"key": "AIza..."}}}}
    via_gemini = {"models": {"providers": {"gemini": {"key": "AIza..."}}}}
    assert "gemini" in configured_embedding_providers(via_google)
    assert "gemini" in configured_embedding_providers(via_gemini)


def test_configured_orders_chain_first():
    """Chain-order providers come ahead of others."""
    config = {"models": {"providers": {"openai": {}, "voyage": {}}}}
    result = configured_embedding_providers(config)
    # openai is in DEFAULT_EMBEDDING_CHAIN; voyage isn't → openai first.
    assert result.index("openai") < result.index("voyage")


# ── providers_from_auth_profiles ─────────────────────────────────────────────


def test_providers_from_auth_profiles_canonical_shape():
    """Standard auth-profiles.json has {"profiles": {id: {provider, ...}}}."""
    raw = {"profiles": {
        "openai:api_key":    {"provider": "openai",    "type": "api_key", "key": "k"},
        "google:api_key":    {"provider": "google",    "type": "api_key", "key": "k"},
        "anthropic:api_key": {"provider": "anthropic", "type": "api_key", "key": "k"},
    }}
    assert providers_from_auth_profiles(raw) == {"openai", "google", "anthropic"}


def test_providers_from_auth_profiles_bare_dict_shape():
    """Test fixtures occasionally pass the bare {profile_id: {...}} dict."""
    raw = {
        "openai:api_key": {"provider": "openai", "type": "api_key"},
    }
    assert providers_from_auth_profiles(raw) == {"openai"}


def test_providers_from_auth_profiles_lowercases():
    raw = {"profiles": {"x": {"provider": "OpenAI"}}}
    assert providers_from_auth_profiles(raw) == {"openai"}


def test_providers_from_auth_profiles_handles_empty_and_garbage():
    assert providers_from_auth_profiles({}) == set()
    assert providers_from_auth_profiles({"profiles": {}}) == set()
    assert providers_from_auth_profiles({"profiles": None}) == set()
    assert providers_from_auth_profiles({"profiles": "not a dict"}) == set()
    assert providers_from_auth_profiles({"profiles": {"x": "not a dict"}}) == set()
    assert providers_from_auth_profiles({"profiles": {"x": {}}}) == set()


# ── credential_provider_ids kwarg (post-#913 fix) ────────────────────────────


def test_configured_with_explicit_credential_set():
    """When credential_provider_ids is passed, it overrides network.json scan."""
    config = {"models": {"providers": {"openai": {}}}}  # would imply openai only
    # But explicit set says google + voyage are credentialed.
    result = configured_embedding_providers(
        config, credential_provider_ids={"google", "voyage"}
    )
    assert "gemini" in result      # google → gemini alias
    assert "voyage" in result
    assert "openai" not in result  # not in the explicit set


def test_resolve_uses_explicit_credentials_over_network_providers():
    """resolve_embedding_chain threads credential_provider_ids through filter."""
    config = {
        "models": {
            "providers": {},  # empty — would normally yield only no-key providers
            "embedding": {"default_chain": ["openai", "local"]},
        }
    }
    chain = resolve_embedding_chain(
        config, bot_id="bot", credential_provider_ids={"openai"}
    )
    assert "openai" in chain  # explicit creds make openai available


def test_warning_uses_explicit_credentials():
    """Warning surface honors the explicit credential set."""
    config = {"models": {"embedding": {"default_chain": ["openai", "local"]}}}
    # With explicit openai creds, warning should NOT fire.
    msg = embedding_chain_warning(
        config, bot_id="bot", credential_provider_ids={"openai"}
    )
    assert msg is None
    # With no credentials, warning SHOULD fire.
    msg = embedding_chain_warning(
        config, bot_id="bot", credential_provider_ids=set()
    )
    assert msg is not None


# ── Resolver ─────────────────────────────────────────────────────────────────


def test_resolve_returns_default_chain_when_empty():
    config = {"models": {"providers": {"openai": {}, "google": {}}}}
    chain = resolve_embedding_chain(config, bot_id="bot")
    # Default chain is gemini → local (openai dropped — operator must add
    # it explicitly via the AI Optimization UI). Credentialed providers
    # outside DEFAULT_EMBEDDING_CHAIN are NOT auto-promoted into the chain
    # by the default-fallback path; they only appear if explicitly listed.
    assert chain[0] == "gemini"
    assert "local" in chain
    assert "openai" not in chain


def test_resolve_filters_unavailable_providers():
    """A provider in the chain without credentials must be dropped."""
    config = {
        "models": {
            "providers": {},  # no API keys
            "embedding": {"default_chain": ["openai", "voyage", "local"]},
        }
    }
    chain = resolve_embedding_chain(config, bot_id="bot")
    assert "openai" not in chain  # filtered
    assert "voyage" not in chain  # filtered
    assert "local" in chain


def test_resolve_pod_default_chain_overrides_hardcoded():
    config = {
        "models": {
            "providers": {"voyage": {}},
            "embedding": {"default_chain": ["voyage", "local"]},
        }
    }
    chain = resolve_embedding_chain(config, bot_id="bot")
    assert chain[0] == "voyage"


def test_resolve_per_bot_overrides_pod_default():
    config = {
        "models": {
            "providers": {"openai": {}, "google": {}},
            "embedding": {
                "default_chain": ["gemini", "local"],
                "per_bot": {"security_bot": {"chain": ["openai", "local"]}},
            },
        }
    }
    pod_chain = resolve_embedding_chain(config, bot_id="other_bot")
    security_bot_chain = resolve_embedding_chain(config, bot_id="security_bot")
    assert pod_chain[0] == "gemini"
    assert security_bot_chain[0] == "openai"


def test_resolve_guarantees_local_terminal_for_single_provider_chain():
    """A single non-local provider gets local appended as safety net."""
    config = {
        "models": {
            "providers": {"openai": {}},
            "embedding": {"default_chain": ["openai"]},
        }
    }
    chain = resolve_embedding_chain(config, bot_id="bot")
    assert chain == ["openai", "local"]


def test_resolve_falls_back_to_local_when_nothing_available():
    """No credentials, no chain → local is the floor."""
    config = {"models": {"embedding": {"default_chain": ["openai"]}}}
    chain = resolve_embedding_chain(config, bot_id="bot")
    assert chain == ["local"]


# ── Model resolution ─────────────────────────────────────────────────────────


def test_model_for_provider_returns_default():
    assert model_for_provider("gemini") == "gemini-embedding-001"
    assert model_for_provider("openai") == "text-embedding-3-large"


def test_model_for_provider_honors_per_provider_override():
    config = {
        "models": {
            "embedding": {
                "per_provider": {"openai": {"model": "text-embedding-3-small"}},
            }
        }
    }
    assert model_for_provider("openai", config) == "text-embedding-3-small"
    # Other providers still use default.
    assert model_for_provider("gemini", config) == "gemini-embedding-001"


def test_model_for_provider_returns_none_for_unknown():
    assert model_for_provider("nonexistent") is None


# ── memory_search_block ──────────────────────────────────────────────────────


def test_memory_search_block_includes_fallback():
    config = {"models": {"providers": {"openai": {}, "google": {}}}}
    block = memory_search_block(config, bot_id="bot")
    assert "provider" in block
    assert "fallback" in block
    assert block["provider"] != block["fallback"]


def test_memory_search_block_omits_fallback_for_single_chain():
    """When chain has exactly one entry, no fallback field is written."""
    # local-only chain stays length-1 (resolver doesn't append local-to-local).
    config = {"models": {"embedding": {"default_chain": ["local"]}}}
    block = memory_search_block(config, bot_id="bot")
    assert block == {"provider": "local"}


# ── Warnings ─────────────────────────────────────────────────────────────────


def test_warning_when_only_local_available():
    """No remote keys, chain falls to local → warn user to add a real provider."""
    config = {"models": {}}
    msg = embedding_chain_warning(config, bot_id="bot")
    assert msg is not None
    assert "Gemini" in msg or "OpenAI" in msg


def test_warning_when_single_remote_no_fallback():
    """Per-bot config explicitly pins one remote provider → fragile."""
    config = {
        "models": {
            "providers": {"openai": {}},
            "embedding": {"per_bot": {"bot": {"chain": ["openai"]}}},
        }
    }
    # Resolver appends local as safety net, so this becomes a 2-entry chain;
    # warning should NOT fire because there's a fallback.
    assert embedding_chain_warning(config, bot_id="bot") is None


def test_no_warning_when_chain_is_healthy():
    config = {
        "models": {
            "providers": {"openai": {}, "google": {}},
        }
    }
    assert embedding_chain_warning(config, bot_id="bot") is None
