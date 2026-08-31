"""Credential-scoped view of the pod-wide model-listings cache.

Extracted from ``routes_admin_config.register_admin_config_routes`` so the
frozen ``routes_admin_config.py`` hot file doesn't grow (file-size ratchet,
4.1a) — the filtering rule lives here and the ``/api/models/listings`` routes
call :func:`filter_listings_to_credentialed`.

The credential readers stay in the route module (they close over the Flask
app's bot resolution); they arrive here as callables.
"""
from __future__ import annotations

from collections.abc import Callable


def llm_providers(net: dict, cache: dict, credentialed: set) -> set:
    """The LLM-capable subset of a credentialed provider set the easy-setup
    wizard ranks (spec §Addendum 7 #12). ``credentialed`` is either the pod
    union or a single bot's providers, depending on the calling surface.

    An "LLM provider" is one the pod can actually route a chat turn to:
    either it names a model in a catalog rung cluster
    (``primary_bot.llm_providers_from_catalog`` — anthropic/openai/google/xai
    today), OR it lists at least one chat-capable model in discovery
    (``model_discovery.llm_providers_from_listings`` — picks up a credentialed
    DeepSeek that isn't yet in any cluster). The union, intersected with the
    credentialed set, EXCLUDES non-LLM providers (Brave, Runway) with no
    provider-name literal — both inputs are data-derived.
    """
    import model_discovery as _model_discovery
    from primary_bot import (  # type: ignore
        pod_default_catalog_view,
        llm_providers_from_catalog,
    )
    catalog = pod_default_catalog_view(net)
    from_catalog = llm_providers_from_catalog(catalog)
    from_listings = _model_discovery.llm_providers_from_listings(
        cache, providers=credentialed,
    )
    return {p for p in credentialed if p in from_catalog or p in from_listings}


def filter_listings_to_credentialed(
    cache: dict,
    net: dict,
    bot_id: str = "",
    *,
    bot_providers_with_keys: Callable[[str], set],
    pod_credentialed: Callable[[], set],
) -> dict:
    """Return the cache document filtered to credentialed providers only.

    The listings cache is pod-wide, but the picker is per-bot: with a
    ``bot_id`` the credentialed set narrows to THAT bot's own keys, so a bot
    is never offered a model it can't reach (the xai/grok-in-a-non-xAI-bot
    bug, picker side). Empty ``bot_id`` — the pod default editor — keeps the
    pod union, since that template is provider-matched per bot at resolution
    time. Unreadable bot credentials fail open to the pod set, matching the
    validate-model route (both go through ``scope_credentialed_to_bot``).

    The cache may carry providers later de-credentialed; the picker must only
    offer providers the surface can actually reach. Degraded entries are
    likewise filtered to the credentialed set.

    ``llm_providers`` is the LLM-capable subset the easy-setup wizard ranks
    (Brave/Runway excluded, DeepSeek included if it lists chat models) — the
    client builds its provider-order from THIS, not the raw credentialed set.
    """
    from ..model_catalog import scope_credentialed_to_bot
    credentialed = scope_credentialed_to_bot(
        bot_id, bot_providers_with_keys, pod_credentialed,
    )
    providers_in = (cache.get("providers") or {})
    degraded_in = (cache.get("degraded") or [])
    return {
        "refreshed_at": cache.get("refreshed_at"),
        "providers": {
            p: models for p, models in providers_in.items()
            if p in credentialed
        },
        "degraded": [
            d for d in degraded_in
            if d.get("provider") in credentialed
        ],
        "credentialed_providers": sorted(credentialed),
        "llm_providers": sorted(llm_providers(net, cache, credentialed)),
    }
