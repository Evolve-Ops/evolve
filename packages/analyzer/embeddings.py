#!/usr/bin/env python3
"""
embeddings.py — Embedding-provider registry, resolver, and pricing.

Single source of truth for which embedding provider a bot uses for
``memory_search``. Distinct from chat-model tiers (see models.py): chat
models and embedding models are different model families even within the
same vendor (e.g., openai/gpt-4o for chat vs openai/text-embedding-3-large
for embeddings), and not every chat-capable provider offers embeddings
(anthropic, xai do not — they recommend voyage and have no first-party
embedding API respectively).

Why a separate registry:
  - Provider menu differs from chat (no anthropic, no xai).
  - Quality axis differs (multimodal capability, retrieval-tuned, local).
  - Pricing model differs (single per-token rate, no input/output split).

Resolution order (mirrors models._get_tier_config):
  1. Hardcoded DEFAULT_EMBEDDING_CHAIN
  2. network.json → models.embedding.default_chain (pod-wide)
  3. network.json → models.embedding.per_bot[bot].chain (per-bot override)

Per-bot OpenClaw config is the actual runtime authority — evolve writes
``agents.defaults.memorySearch.{provider, fallback}`` into each bot's
openclaw.json so OpenClaw can fail over without evolve in the loop.
This module is what decides what to write.

network.json embedding block:
  "models": {
    "embedding": {
      "default_chain": ["gemini", "openai", "local"],
      "per_provider": {
        "gemini":  { "model": "gemini-embedding-001" },
        "openai":  { "model": "text-embedding-3-large" },
        "voyage":  { "model": "voyage-3-large" },
        "local":   { "model": "local" }
      },
      "per_bot": {
        "security_bot": { "chain": ["gemini", "openai"] }
      }
    }
  }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ── Provider capability registry ──────────────────────────────────────────────
# Embedding-capable providers OpenClaw supports today. The chat-model menu in
# models.py is broader (includes anthropic, xai); this is intentionally smaller.
#
# Fields:
#   label              — display name for UI
#   default_model      — recommended model id when none specified
#   needs_api_key      — true → requires explicit credential; gates UI offers
#   credential_keys    — network.json keys that satisfy this provider's auth
#   capabilities       — set of feature flags ("multimodal", "subscription",
#                        "local", "retrieval_tuned")
#   notes              — one-line UI hint shown next to the provider in pickers


@dataclass(frozen=True)
class EmbeddingProvider:
    id: str
    label: str
    default_model: str
    needs_api_key: bool
    credential_keys: tuple[str, ...]
    capabilities: frozenset[str]
    notes: str


EMBEDDING_PROVIDERS: dict[str, EmbeddingProvider] = {
    "gemini": EmbeddingProvider(
        id="gemini",
        label="Google Gemini",
        default_model="gemini-embedding-001",
        needs_api_key=True,
        credential_keys=("google", "gemini"),
        capabilities=frozenset({"multimodal"}),
        notes="Only provider with multimodal indexing (images + audio).",
    ),
    "openai": EmbeddingProvider(
        id="openai",
        label="OpenAI",
        default_model="text-embedding-3-large",
        needs_api_key=True,
        credential_keys=("openai",),
        capabilities=frozenset(),
        notes="Strong general-purpose. Fast, well-supported.",
    ),
    "voyage": EmbeddingProvider(
        id="voyage",
        label="Voyage AI",
        default_model="voyage-3-large",
        needs_api_key=True,
        credential_keys=("voyage",),
        capabilities=frozenset({"retrieval_tuned"}),
        notes="Anthropic-recommended, retrieval-specialized. Highest text recall.",
    ),
    "github-copilot": EmbeddingProvider(
        id="github-copilot",
        label="GitHub Copilot",
        default_model="copilot-text-embedding-3-small",
        needs_api_key=False,
        credential_keys=("github_copilot", "copilot"),
        capabilities=frozenset({"subscription"}),
        notes="Bundled with Copilot subscription. No extra per-call billing.",
    ),
    "mistral": EmbeddingProvider(
        id="mistral",
        label="Mistral",
        default_model="mistral-embed",
        needs_api_key=True,
        credential_keys=("mistral",),
        capabilities=frozenset(),
        notes="Multilingual support.",
    ),
    "bedrock": EmbeddingProvider(
        id="bedrock",
        label="AWS Bedrock",
        default_model="amazon.titan-embed-text-v2:0",
        needs_api_key=False,
        credential_keys=("aws", "bedrock"),
        capabilities=frozenset(),
        notes="Auto-detected when AWS credential chain resolves.",
    ),
    "local": EmbeddingProvider(
        id="local",
        label="Local (GGUF)",
        default_model="local",
        needs_api_key=False,
        credential_keys=(),
        capabilities=frozenset({"local", "offline"}),
        notes="Free, offline. Slower and lower quality than cloud providers.",
    ),
    "ollama": EmbeddingProvider(
        id="ollama",
        label="Ollama",
        default_model="nomic-embed-text",
        needs_api_key=False,
        credential_keys=(),
        capabilities=frozenset({"local", "offline"}),
        notes="Local Ollama instance. Must be set explicitly.",
    ),
}


# ── Default chain ─────────────────────────────────────────────────────────────
# Used when network.json carries no embedding block. Order: multimodal-capable
# first, then offline-safe fallback. OpenAI is omitted from the default chain
# because the OpenAI embeddings tier most pods are provisioned on hits 429s
# under normal memory_search load; falling back to it creates retry storms
# that saturate the gateway event loop (observed jamming security_bot's gateway
# until it crash-loops). Operators who want OpenAI in the chain can add it
# explicitly via network.json or the AI Optimization UI.
# Resolver filters this against actually-available credentials at runtime.

DEFAULT_EMBEDDING_CHAIN: list[str] = ["gemini", "local"]


# ── Per-token pricing (USD per million tokens) ────────────────────────────────
# Embeddings price as a single rate (no input/output split, no caching tier).
# Used by usage_analytics for cost rollups once embedding cost events land
# in the cost stream (Phase 6 — currently OpenClaw doesn't emit them).

EMBEDDING_PRICING: dict[str, float] = {
    # Gemini (provider id "gemini" — matches OpenClaw's memorySearch.provider)
    "gemini/gemini-embedding-001":          0.15,
    "gemini/text-embedding-004":            0.025,
    # OpenAI
    "openai/text-embedding-3-large":        0.13,
    "openai/text-embedding-3-small":        0.02,
    "openai/text-embedding-ada-002":        0.10,
    # Voyage
    "voyage/voyage-3-large":                0.18,
    "voyage/voyage-3":                      0.06,
    "voyage/voyage-3-lite":                 0.02,
    # Mistral
    "mistral/mistral-embed":                0.10,
    # AWS Bedrock (Titan v2)
    "bedrock/amazon.titan-embed-text-v2:0": 0.02,
    # Local / Ollama / Copilot — no per-call cost
    "local/local":                          0.0,
    "ollama/nomic-embed-text":              0.0,
    "github-copilot/copilot-text-embedding-3-small": 0.0,
}


# ── Public API ────────────────────────────────────────────────────────────────


def is_embedding_capable(provider_id: str) -> bool:
    """True if ``provider_id`` is in the embedding registry."""
    return provider_id in EMBEDDING_PROVIDERS


def embedding_provider(provider_id: str) -> EmbeddingProvider | None:
    """Lookup with no error — None if unknown."""
    return EMBEDDING_PROVIDERS.get(provider_id)


def embedding_capable_providers() -> list[str]:
    """Sorted list of all embedding-capable provider IDs."""
    return sorted(EMBEDDING_PROVIDERS)


def configured_embedding_providers(
    config: dict[str, Any],
    credential_provider_ids: "set[str] | None" = None,
) -> list[str]:
    """
    Embedding-capable providers that *also* have credentials configured.

    ``credential_provider_ids`` is the set of provider IDs the caller has
    determined have keys (typically derived from a bot's auth-profiles.json
    via ``providers_from_auth_profiles``). When not supplied, falls back to
    scanning ``network.json models.providers`` — but on real evolve installs
    that block is rarely populated (creds live per-bot in auth-profiles.json),
    so callers should pass ``credential_provider_ids`` explicitly whenever a
    bot is in scope.

    Returned in DEFAULT_EMBEDDING_CHAIN order first, then alphabetical.
    No-key providers (local, copilot, bedrock, ollama) are always offered.
    """
    if credential_provider_ids is None:
        providers_block = config.get("models", {}).get("providers", {}) or {}
        credential_provider_ids = set(providers_block.keys())

    available: set[str] = set()
    for pid, prov in EMBEDDING_PROVIDERS.items():
        if not prov.needs_api_key:
            available.add(pid)
            continue
        if any(key in credential_provider_ids for key in prov.credential_keys):
            available.add(pid)

    chain_order = {pid: i for i, pid in enumerate(DEFAULT_EMBEDDING_CHAIN)}
    return sorted(available, key=lambda p: (chain_order.get(p, 999), p))


def providers_from_auth_profiles(auth_profiles: dict) -> set[str]:
    """
    Extract the set of provider IDs that have credentials in an auth-profiles.json.

    Accepts either the canonical ``{"profiles": {...}}`` wrapper or the
    bare ``{profile_id: {...}}`` dict. Reads each profile's ``provider``
    field, lowercased. Profiles without a provider are ignored.
    """
    if isinstance(auth_profiles, dict) and "profiles" in auth_profiles:
        profiles = auth_profiles["profiles"] or {}
    else:
        profiles = auth_profiles or {}
    out: set[str] = set()
    if not isinstance(profiles, dict):
        return out
    for entry in profiles.values():
        if not isinstance(entry, dict):
            continue
        prov = entry.get("provider")
        if isinstance(prov, str) and prov:
            out.add(prov.lower())
    return out


def chain_from_credentials(provider_ids: set[str] | list[str]) -> list[str]:
    """
    Compute a default embedding chain from a set of credential-providing IDs.

    Used during install (setup_wizard) when network.json hasn't been written
    yet — we only have the bare set of providers the operator collected
    keys for. Returns a chain in DEFAULT_EMBEDDING_CHAIN preference order,
    filtered to those with credentials, with ``local`` always appended as
    the safety net.

    Provider IDs may be either embedding-provider IDs (``gemini``) or
    chat-provider IDs (``google``) — credential aliases are honored, so
    a ``google`` credential resolves to the ``gemini`` embedding provider.
    """
    have = set(provider_ids)
    available: list[str] = []
    for pid, prov in EMBEDDING_PROVIDERS.items():
        if not prov.needs_api_key:
            continue
        if any(key in have for key in prov.credential_keys):
            available.append(pid)

    chain_order = {pid: i for i, pid in enumerate(DEFAULT_EMBEDDING_CHAIN)}
    available.sort(key=lambda p: (chain_order.get(p, 999), p))

    if "local" not in available:
        available.append("local")
    return available


def resolve_embedding_chain(
    config: dict[str, Any],
    bot_id: str | None = None,
    *,
    credential_provider_ids: "set[str] | None" = None,
) -> list[str]:
    """
    Resolve the ordered embedding-provider chain for ``bot_id``.

    Merge order:
      DEFAULT_EMBEDDING_CHAIN
        → network.json models.embedding.default_chain
        → network.json models.embedding.per_bot[bot].chain

    When ``bot_id`` is None, falls through to the primary bot so engine
    background calls inherit the operator's per-bot choices (matches the
    pattern in models._get_tier_config).

    The resolved chain is filtered against ``configured_embedding_providers``
    so a provider with no credentials never appears in the final list. Pass
    ``credential_provider_ids`` (the set of provider IDs that have keys, e.g.
    via ``providers_from_auth_profiles``) so the filter operates on real
    per-bot credentials rather than the rarely-populated ``models.providers``
    block in network.json.

    Returns at minimum ["local"] so callers always have something to use.
    """
    embedding_block = config.get("models", {}).get("embedding", {}) or {}

    # Pod-wide default
    chain = list(embedding_block.get("default_chain") or DEFAULT_EMBEDDING_CHAIN)

    # Primary-bot fallthrough for engine calls
    if bot_id is None:
        try:
            from primary_bot import primary_bot_id  # type: ignore

            bot_id = primary_bot_id(config)
        except Exception:
            bot_id = None

    # Per-bot override
    if bot_id:
        per_bot = embedding_block.get("per_bot", {}).get(bot_id, {})
        bot_chain = per_bot.get("chain")
        if isinstance(bot_chain, list) and bot_chain:
            chain = list(bot_chain)

    # Filter to providers that can actually be used (have credentials).
    available = set(configured_embedding_providers(config, credential_provider_ids))
    chain = [p for p in chain if p in available]

    # Always guarantee a usable terminal — local needs no credentials.
    if not chain:
        chain = ["local"]
    elif "local" not in chain and len(chain) < 2:
        chain.append("local")

    return chain


def model_for_provider(
    provider_id: str,
    config: dict[str, Any] | None = None,
) -> str | None:
    """
    Resolve the embedding model id for ``provider_id``.

    Precedence: network.json models.embedding.per_provider[id].model →
    EmbeddingProvider.default_model. Returns None for unknown providers.
    """
    prov = EMBEDDING_PROVIDERS.get(provider_id)
    if prov is None:
        return None
    if config:
        per_prov = (
            config.get("models", {})
            .get("embedding", {})
            .get("per_provider", {})
            .get(provider_id, {})
        )
        model = per_prov.get("model")
        if isinstance(model, str) and model:
            return model
    return prov.default_model


def memory_search_block(
    config: dict[str, Any],
    bot_id: str | None = None,
    *,
    credential_provider_ids: "set[str] | None" = None,
) -> dict[str, Any]:
    """
    Build the ``agents.defaults.memorySearch`` block to write into a bot's
    openclaw.json. OpenClaw's runtime supports a primary ``provider`` plus
    a single ``fallback`` (string). We take the first two entries of the
    resolved chain.
    """
    chain = resolve_embedding_chain(
        config, bot_id, credential_provider_ids=credential_provider_ids
    )
    if not chain:
        return {}
    block: dict[str, Any] = {"provider": chain[0]}
    if len(chain) > 1:
        block["fallback"] = chain[1]
    return block


# ── Warning surfaces ──────────────────────────────────────────────────────────
# Used by the admin UI banner and by setup wizard validation.


def embedding_chain_warning(
    config: dict[str, Any],
    bot_id: str | None = None,
    *,
    credential_provider_ids: "set[str] | None" = None,
) -> str | None:
    """
    Return a human-readable warning if the bot's embedding chain is fragile,
    else None. Conditions:
      - chain is empty / only contains "local" when remote providers exist
        but none are configured
      - chain has a single non-local entry with no fallback
    """
    chain = resolve_embedding_chain(
        config, bot_id, credential_provider_ids=credential_provider_ids
    )
    has_remote = any(p not in {"local", "ollama"} for p in chain)

    if not has_remote:
        return (
            "No cloud embedding provider is configured. Memory search will "
            "fall back to a local GGUF model — slower and lower quality. "
            "Add an API key for Gemini, OpenAI, or Voyage."
        )
    if len(chain) == 1 and chain[0] not in {"local", "ollama"}:
        return (
            f"Embedding provider {chain[0]!r} has no fallback. "
            "If its key is rate-limited or revoked, memory search will fail. "
            "Configure a fallback provider in AI Optimization."
        )
    return None
