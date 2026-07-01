"""
evolve_admin.oauth.orchestrator — Generic OAuth prerequisite evaluation.

Single public function: ``evaluate_install_prerequisites(bot_id, requirements)``.
Iterates ``requirements.integrations`` and delegates each integration to the
matching provider in ``PROVIDER_REGISTRY``.

V2-4 backward compatibility
---------------------------
The old ``applications/oauth_orchestrator.py`` called ``evaluate_install_prerequisites``
with three injectable reader callables (``read_plugin_enabled``,
``read_oauth_profile``, ``read_oauth_client_configured``).  Those readers are
GOG-specific.  This module keeps the same signature and forwards them to
``gog_provider._gog_build_missing_item`` when the integration_id is ``"gog"``.

That means:
- All 13 V2-4 tests pass unchanged — they inject readers into
  ``evaluate_install_prerequisites`` exactly as before.
- Adding a new provider (e.g. Slack) is a new file in providers/ with no
  injectable-reader pattern needed (Slack's readers live in its own provider).

Unknown integrations
--------------------
If no provider claims an integration_id, the orchestrator returns a generic
unsatisfied item with ``status="unknown_integration"`` and ``action_url=None``.
This is safe — the install pauses and the operator sees the unrecognised
integration name.  A future provider registration immediately handles it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


def evaluate_install_prerequisites(
    bot_id: str,
    requirements: dict,
    *,
    shared_dir: Path,
    # Injectable readers for testability — GOG-specific, forwarded to gog_provider.
    # Kept for V2-4 backward compatibility; new providers carry their own readers.
    read_plugin_enabled: "Callable[[str], bool | None] | None" = None,
    read_oauth_profile: "Callable[[str], dict | None] | None" = None,
    read_oauth_client_configured: "Callable[[], bool] | None" = None,
) -> dict:
    """Check whether all integration prerequisites for an install are satisfied.

    Reads ``requirements.integrations`` from the package manifest and checks
    each integration against the bot's current state by looking up the
    responsible provider in ``PROVIDER_REGISTRY``.

    The three injectable reader callables are GOG-specific and are forwarded
    to ``gog_provider`` for backward compatibility with V2-4 tests.  New
    providers (added in V2.1-2+) carry their own reader closures and do not
    use these parameters.

    Returns::

        {
            "satisfied": True | False,
            "missing": [
                {
                    "integration_id": "gog",
                    "skill_id": "gog",
                    "display_name": "Google (GOG)",
                    "reason": "Reads Gmail and Google Calendar for briefing content",
                    "status": "oauth_pending",
                    "action_url": "/api/skills/install/gog",
                    "action_label": "Set up Gmail & Calendar",
                    "install_plan_steps": [...]
                }
            ]
        }

    For unknown integrations (no registered provider)::

        {
            "satisfied": False,
            "missing": [
                {
                    "integration_id": "slack",
                    "skill_id": "slack",
                    "display_name": "slack",
                    "reason": "slack integration required",
                    "status": "unknown_integration",
                    "action_url": None,
                    "action_label": "Set up slack",
                    "install_plan_steps": []
                }
            ]
        }
    """
    from .providers import find_provider

    integrations = requirements.get("integrations", [])
    if not integrations:
        return {"satisfied": True, "missing": []}

    # Recognize plugin-only integrations (brave, github, …) that are
    # already loaded on this bot. These have no per-bot OAuth flow and no
    # provider in PROVIDER_REGISTRY because they need no install ceremony
    # — they just load from the pod's plugin load_paths. Without this
    # check the orchestrator falsely reports them as missing whenever the
    # manifest declares them as a requirement.
    #
    # Two sources, unioned:
    #   1. The bot's live inventory (what's actually enabled right now).
    #   2. The baseline's ``required_plugins`` (pod-wide invariants —
    #      empty by default in v2 but reserved for the case of a plugin
    #      that every bot must run).
    # Best-effort: if either lookup fails, fall back to treating as
    # missing (the previous behaviour). Both modules are available in
    # the admin daemon's runtime environment.
    pod_enabled_plugins: set[str] = set()
    try:
        from plugins import inventory as _inv  # type: ignore[import-not-found]
        live = _inv.load_inventory(shared_dir, bot_id) or {}
        for entry in live.get("entries") or []:
            if isinstance(entry, dict) and entry.get("enabled") and entry.get("name"):
                pod_enabled_plugins.add(entry["name"])
    except Exception as _inv_exc:
        log.debug(
            "orchestrator: live inventory not available for %s (%s); "
            "live-enabled plugins won't satisfy integration requirements",
            bot_id, _inv_exc,
        )
    try:
        from plugins import baseline as _bl  # type: ignore[import-not-found]
        _bl_loaded = _bl.load(shared_dir)
        pod_enabled_plugins.update(_bl_loaded.required_plugins or [])
    except Exception as _bl_exc:
        log.debug(
            "orchestrator: pod baseline not available for %s (%s)",
            bot_id, _bl_exc,
        )

    missing: list[dict] = []

    for req in integrations:
        if not isinstance(req, dict):
            continue

        # Skip integrations explicitly marked optional. The manifest author
        # opted them out of the gating contract; the operator can install
        # later via the normal Skills flow if they want the feature.
        if req.get("required") is False:
            continue

        integration_id = req.get("id", "")

        # Plugin-only integration available as a pod-wide invariant → satisfied.
        # Covers brave, github, and any other load-path-installed plugin that
        # has no per-bot OAuth surface.
        if integration_id and integration_id in pod_enabled_plugins:
            continue

        provider = find_provider(integration_id)

        if provider is None:
            # No provider registered for this integration — treat as unsatisfied.
            item: "dict | None" = {
                "integration_id": integration_id,
                "skill_id": integration_id,
                "display_name": req.get("display_name", integration_id),
                "reason": req.get("reason", f"{integration_id} integration required"),
                "status": "unknown_integration",
                "action_url": None,
                "action_label": f"Set up {req.get('display_name', integration_id)}",
                "install_plan_steps": [],
            }
        elif integration_id in ("gog", "gmail", "calendar") and (
            read_plugin_enabled is not None
            or read_oauth_profile is not None
            or read_oauth_client_configured is not None
        ):
            # V2-4 + V2.1-3 backward compat path: caller injected Google-specific
            # readers.  All three Google skills (gog, gmail, calendar) share the
            # same underlying OAuth profile + plugin, so the same readers work.
            # Bypass the provider's default closures and call the provider's internal
            # build function directly with the injected readers.
            if integration_id == "gog":
                from .providers.gog_provider import _gog_build_missing_item as _build_fn
            elif integration_id == "gmail":
                from .providers.gmail_provider import _gmail_build_missing_item as _build_fn  # type: ignore[assignment]
            else:  # calendar
                from .providers.calendar_provider import _calendar_build_missing_item as _build_fn  # type: ignore[assignment]
            item = _build_fn(
                bot_id, req,
                read_plugin_enabled=read_plugin_enabled,
                read_oauth_profile=read_oauth_profile,
                read_oauth_client_configured=read_oauth_client_configured,
                shared_dir=shared_dir,
            )
        else:
            # Normal path: use the provider's build_missing_item closure.
            try:
                item = provider.build_missing_item(bot_id, req)
            except Exception as exc:
                log.warning(
                    "orchestrator: provider %r failed for %s/%s: %s",
                    provider.skill_id, bot_id, integration_id, exc,
                )
                item = {
                    "integration_id": integration_id,
                    "skill_id": provider.skill_id,
                    "display_name": req.get("display_name", integration_id),
                    "reason": req.get("reason", f"{integration_id} integration required"),
                    "status": "unknown",
                    "action_url": provider.action_url(bot_id),
                    "action_label": provider.action_label,
                    "install_plan_steps": [],
                }

        if item is not None:
            missing.append(item)

    return {
        "satisfied": len(missing) == 0,
        "missing": missing,
    }
