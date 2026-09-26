"""Credential reads from a bot's openclaw.json.

Extracted from ``routes_admin_shared`` so that frozen hot file doesn't grow
(file-size ratchet, 4.1a).
"""
from __future__ import annotations

from .oc_agent_keys import KNOWN_LLM_PROVIDERS

# Providers whose credential Evolve can POSITIVELY VERIFY — not "providers
# whose key lives in openclaw.json", which is what this set used to mean.
#
# This distinction is what makes "plugin enabled + no key" a trustworthy
# defect signal: for a provider in this set we can actually go and look, so
# enabled-but-keyless is a finding rather than a guess.
#
# TWO FAMILIES ARE IN IT, for two different reasons:
#
#   * ``brave`` — the runtime reads the key from
#     ``plugins.entries.brave.config`` (or the legacy
#     ``tools.web.search.apiKey``), both inside openclaw.json. See
#     ``brave_key_from_oc_config``.
#
#   * the LLM providers — the runtime reads the key from OC's PER-AGENT
#     store: ``agents/<a>/agent/plugins/<p>/catalog.json``,
#     ``agents/<a>/agent/models.json`` and
#     ``agents/<a>/agent/codex-home/auth.json``. See
#     :mod:`evolve_admin.web.oc_agent_keys`.
#
# THIS COMMENT USED TO SAY THE OPPOSITE, and the reasoning it gave was
# refuted by measurement on 2026-09-04. It read: "LLM providers (anthropic,
# google, openai, xai) routinely run off a workspace ``.env`` or
# auth-profiles entry that the openclaw.json probe can't see, so treating
# their enabled-but-unkeyed state as a gap would cry wolf on working bots."
# The premise was right — the openclaw.json probe could not see those keys —
# but the conclusion (hide the row) was wrong, and the guessed location was
# wrong too. A read-only scan of the live PoC bot found the keys in NONE of
# ``.env``, ``auth-profiles.json`` or ``openclaw.json``: they were in the
# per-agent runtime store above, duplicated across every agent the bot runs.
# The visible cost of hiding rather than probing: the Credentials tab showed
# NO LLM Providers section at all while the Anthropic console showed $36
# month-to-date, and "how do we rotate API keys for LLMs?" had no UI answer.
# The fix is to look in the right place (``OcAgentCatalogKeyProbe``), not to
# suppress the row.
#
# Consumers: ``skills.inventory`` (Skills-page status) and
# ``web.credentials_visibility`` (Credentials-tab visibility rule (d)). Keep
# them agreeing — a provider in one and not the other is how the canonical /
# legacy brave mismatch #3219 fixed came about in the first place.
#
# The residual false-positive risk, stated rather than papered over: a bot
# whose LLM key comes from an environment variable in the gateway's plist is
# invisible to every probe, so an enabled provider there reads as a gap. That
# bot is caught one clause later — visibility rule (d′) upgrades any provider
# with turns in the last 30 days to ``active_unlocated`` BEFORE rule (d) is
# evaluated, so a working-but-unlocatable provider is never labelled
# "missing". A provider that is enabled, keyless AND has never served a turn
# is a gap by any reading.
INLINE_KEY_PROVIDERS: frozenset[str] = frozenset({"brave"}) | frozenset(
    KNOWN_LLM_PROVIDERS
)

#: The LLM subset of the above — the providers whose verification goes
#: through the per-agent runtime store rather than openclaw.json.
RUNTIME_STORE_KEY_PROVIDERS: frozenset[str] = frozenset(KNOWN_LLM_PROVIDERS)


def brave_key_from_oc_config(oc_cfg: dict) -> str | None:
    """Return the Brave API key stored in openclaw.json, or None.

    The wizard / rotate path writes the key to the CANONICAL location
    ``plugins.entries.brave.config.webSearch.apiKey`` (see
    ``server._RUNTIME_MIRROR_PATH``). Hand-edits and older configs sometimes
    carry it at the LEGACY ``tools.web.search.apiKey`` (the path ``ocadmin``'s
    menu offers to migrate). The Credentials-tab probe must honour BOTH or a
    legacy-configured key reads as "Setup required" even though search works.
    Canonical wins when both are present.
    """
    if not isinstance(oc_cfg, dict):
        return None
    web_search = (
        oc_cfg.get("plugins", {})
              .get("entries", {})
              .get("brave", {})
              .get("config", {})
              .get("webSearch", {}) or {}
    )
    canonical = (web_search.get("apiKey") or "").strip()
    if canonical:
        return canonical
    legacy = (
        (oc_cfg.get("tools", {}).get("web", {}).get("search", {}) or {})
        .get("apiKey") or ""
    ).strip()
    return legacy or None
