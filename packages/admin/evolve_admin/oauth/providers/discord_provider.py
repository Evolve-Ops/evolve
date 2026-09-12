"""
evolve_admin.oauth.providers.discord_provider — Discord OAuth provider.

Wraps ``evolve_admin.skills.discord_install`` in the ``Provider`` interface.
Mirrors the Slack provider shape (V2.1-2). Discord is server/workspace-shaped
like Slack, so the provider structure is nearly identical.

Registration
------------
At import time this module appends a ``Provider`` instance to
``PROVIDER_REGISTRY`` in ``providers/__init__.py``. The registry is a
singleton list — importing this module twice is safe (the guard at the
bottom checks for duplicate registration).

Discord vs. Slack provider differences
---------------------------------------
Slack: OAuth returns a per-workspace bot token. Each workspace needs its own
       OAuth flow.
Discord: The bot token is a single pod-level credential stored in the keystore.
         The "OAuth" flow for Discord is really a guild-invite flow: we generate
         a bot invite URL with the client_id + permissions, and the operator
         selects which server(s) to add the bot to. The ``is_satisfied`` check
         validates the keystore bot token against Discord's /users/@me endpoint.

The interface is unchanged from V2.1-1.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ── Provider implementation ───────────────────────────────────────────────────


def _discord_is_satisfied(bot_id: str, *, shared_dir: "Path | None" = None) -> bool:
    """Return True if the bot has a valid Discord token."""
    from ...skills import discord_install

    try:
        status = discord_install.resolve_status(bot_id, shared_dir=shared_dir)
        return status.token_state == "valid"
    except Exception as exc:
        log.warning("discord_provider.is_satisfied: check failed for %s: %s", bot_id, exc)
        return False


def _discord_build_missing_item(
    bot_id: str,
    req: dict,
    *,
    shared_dir: "Path | None" = None,
) -> "dict | None":
    """Return a missing-item dict for Discord, or None if satisfied.

    Delegates status resolution to ``discord_install.resolve_status`` and plan
    building to ``discord_install.build_install_plan``. The returned shape
    matches the ``missing[]`` element expected by ``evaluate_install_prerequisites``.
    """
    from ...skills import discord_install

    try:
        status = discord_install.resolve_status(bot_id, shared_dir=shared_dir)
    except Exception as exc:
        log.warning("discord_provider: Discord status check failed for %s: %s", bot_id, exc)
        return {
            "integration_id": discord_install.DISCORD_SKILL_ID,
            "skill_id": discord_install.DISCORD_SKILL_ID,
            "display_name": req.get("display_name", "Discord"),
            "reason": req.get("reason", "Discord server access required"),
            "status": "unknown",
            "action_url": "/api/skills/install/discord",
            "action_label": "Set up Discord server",
            "install_plan_steps": [],
        }

    if status.token_state == "valid":
        return None  # satisfied — nothing missing

    try:
        plan = discord_install.build_install_plan(status)
        plan_steps = [s.to_dict() for s in plan]
    except Exception:
        plan_steps = []

    return {
        "integration_id": discord_install.DISCORD_SKILL_ID,
        "skill_id": discord_install.DISCORD_SKILL_ID,
        "display_name": req.get("display_name", "Discord"),
        "reason": req.get("reason", "Discord server access required"),
        "status": status.token_state,
        "action_url": "/api/skills/install/discord",
        "action_label": "Set up Discord server",
        "install_plan_steps": plan_steps,
    }


def _discord_action_url(bot_id: str) -> str:
    return "/api/skills/install/discord"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402


def _build_provider() -> Provider:
    """Build the Discord Provider instance."""
    return Provider(
        integration_ids=frozenset({"discord"}),
        skill_id="discord",
        is_satisfied=lambda bot_id: _discord_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _discord_build_missing_item(bot_id, req),
        action_url=_discord_action_url,
        action_label="Set up Discord server",
    )


# Guard against double-registration if this module is somehow imported twice.
if not any("discord" in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
