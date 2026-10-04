"""
evolve_admin.oauth.providers.telegram_provider — Telegram skill provider.

Wraps ``evolve_admin.skills.telegram_install`` in the ``Provider`` interface.
Telegram uses a static BotFather token rather than OAuth, but it still fits
cleanly in the V2.1-1 Provider interface: ``is_satisfied`` checks whether a
valid token is stored, and ``action_url`` points to the token-entry route.

Registration
------------
At import time this module appends a ``Provider`` instance to
``PROVIDER_REGISTRY`` in ``providers/__init__.py``.  The registry is a
singleton list — importing this module twice is safe (the guard at the
bottom checks for duplicate registration).

V2.3-1 note
-----------
Telegram's non-OAuth shape (BotFather token rather than OAuth code exchange)
is handled entirely in ``telegram_install.py``. The Provider interface is
unchanged from V2.1-1 — Telegram fits as-is via the is_satisfied / action_url
contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ── Provider implementation ───────────────────────────────────────────────────


def _telegram_is_satisfied(bot_id: str) -> bool:
    """Return True if the bot has a valid Telegram token."""
    from ...skills import telegram_install

    try:
        status = telegram_install.resolve_status(bot_id)
        return status.bot_token_state == "valid"
    except Exception as exc:
        log.warning("telegram_provider.is_satisfied: check failed for %s: %s", bot_id, exc)
        return False


def _telegram_build_missing_item(
    bot_id: str,
    req: dict,
) -> "dict | None":
    """Return a missing-item dict for Telegram, or None if satisfied.

    Delegates status resolution to ``telegram_install.resolve_status`` and
    plan building to ``telegram_install.build_install_plan``. The returned
    shape matches the ``missing[]`` element expected by
    ``evaluate_install_prerequisites``.
    """
    from ...skills import telegram_install

    try:
        status = telegram_install.resolve_status(bot_id)
    except Exception as exc:
        log.warning("telegram_provider: Telegram status check failed for %s: %s", bot_id, exc)
        return {
            "integration_id": telegram_install.TELEGRAM_SKILL_ID,
            "skill_id": telegram_install.TELEGRAM_SKILL_ID,
            "display_name": req.get("display_name", "Telegram"),
            "reason": req.get("reason", "Telegram bot access required"),
            "status": "unknown",
            "action_url": "/api/skills/install/telegram",
            "action_label": "Set up Telegram bot",
            "install_plan_steps": [],
        }

    if status.bot_token_state == "valid":
        return None  # satisfied — nothing missing

    try:
        plan = telegram_install.build_install_plan(status)
        plan_steps = [s.to_dict() for s in plan]
    except Exception:
        plan_steps = []

    return {
        "integration_id": telegram_install.TELEGRAM_SKILL_ID,
        "skill_id": telegram_install.TELEGRAM_SKILL_ID,
        "display_name": req.get("display_name", "Telegram"),
        "reason": req.get("reason", "Telegram bot access required"),
        "status": status.bot_token_state,
        "action_url": "/api/skills/install/telegram",
        "action_label": "Set up Telegram bot",
        "install_plan_steps": plan_steps,
    }


def _telegram_action_url(bot_id: str) -> str:
    return "/api/skills/install/telegram"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402


def _build_provider() -> Provider:
    """Build the Telegram Provider instance."""
    return Provider(
        integration_ids=frozenset({"telegram"}),
        skill_id="telegram",
        is_satisfied=lambda bot_id: _telegram_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _telegram_build_missing_item(bot_id, req),
        action_url=_telegram_action_url,
        action_label="Set up Telegram bot",
    )


# Guard against double-registration if this module is somehow imported twice.
if not any("telegram" in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
