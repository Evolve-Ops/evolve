"""Credential reads from a bot's openclaw.json.

Extracted from ``routes_admin_shared`` so that frozen hot file doesn't grow
(file-size ratchet, 4.1a).
"""
from __future__ import annotations


# Providers for which openclaw.json is the AUTHORITATIVE credential store —
# the runtime reads the key from ``plugins.entries[<p>].config``, not from
# env, auth-profiles.json, or channels.*.
#
# This distinction is what makes "plugin enabled + no key" a trustworthy
# defect signal. For everything else it is NOT: LLM providers (anthropic,
# google, openai, xai) routinely run off a workspace ``.env`` or
# auth-profiles entry that the openclaw.json probe can't see, so treating
# their enabled-but-unkeyed state as a gap would cry wolf on working bots —
# the exact false-negative-avoidance reasoning documented in
# ``skills/inventory._resolve_plugin_status``, pointed the other way.
#
# Consumers: ``skills.inventory`` (Skills-page status) and
# ``web.credentials_visibility`` (Credentials-tab visibility rule (d)). Keep
# them agreeing — a provider in one and not the other is how the canonical /
# legacy brave mismatch #3219 fixed came about in the first place.
INLINE_KEY_PROVIDERS: frozenset[str] = frozenset({"brave"})


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
