"""evolve_admin.oauth.providers.imessage_provider — iMessage non-OAuth provider (V2.1-6).

Wraps ``evolve_admin.skills.imessage_install`` in the ``Provider`` interface.
iMessage is a ``kind=local_system`` skill — no OAuth, no token, no cloud.
Satisfaction is determined by TCC permission checks + Messages.app state.

This is the second non-OAuth provider in the registry (after Obsidian). It
validates the provider interface for ``kind=local_system`` skills: TCC checks
substitute for OAuth token checks; ``action_url`` routes to the iMessage skill
install page rather than an OAuth dance.

Interface notes
---------------
The ``Provider.is_satisfied`` / ``build_missing_item`` callables follow the same
contract as OAuth providers. The orchestrator is already agnostic to whether
the underlying check is an OAuth token test or a filesystem/TCC check — the
Obsidian provider (V2.1-5) already established this pattern.

No interface changes were needed to accommodate iMessage.

Registration
------------
At import time this module appends a ``Provider`` instance to
``PROVIDER_REGISTRY``. The guard at the bottom prevents double-registration.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# ── Provider implementation ───────────────────────────────────────────────────


def _imessage_is_satisfied(bot_id: str) -> bool:
    """Return True if the bot has iMessage fully configured and ready to use."""
    from ...skills.imessage_install import resolve_status
    try:
        status = resolve_status(bot_id)
        return status.status == "active"
    except Exception as exc:
        log.warning("imessage_provider.is_satisfied: check failed for %s: %s", bot_id, exc)
        return False


def _imessage_build_missing_item(bot_id: str, req: dict) -> "dict | None":
    """Return a missing-item dict for iMessage, or None if satisfied."""
    from ...skills.imessage_install import (
        resolve_status,
        build_install_plan,
        IMESSAGE_SKILL_ID,
    )

    try:
        status = resolve_status(bot_id)
    except Exception as exc:
        log.warning("imessage_provider: status check failed for %s: %s", bot_id, exc)
        return {
            "integration_id": IMESSAGE_SKILL_ID,
            "skill_id": IMESSAGE_SKILL_ID,
            "display_name": req.get("display_name", "iMessage"),
            "reason": req.get("reason", "iMessage access required"),
            "status": "unknown",
            "action_url": f"/api/skills/install/{IMESSAGE_SKILL_ID}",
            "action_label": "Set up iMessage",
            "install_plan_steps": [],
        }

    if status.status == "active":
        return None  # satisfied

    try:
        plan = build_install_plan(status)
        plan_steps = [s.to_dict() for s in plan]
    except Exception:
        plan_steps = []

    return {
        "integration_id": IMESSAGE_SKILL_ID,
        "skill_id": IMESSAGE_SKILL_ID,
        "display_name": req.get("display_name", "iMessage"),
        "reason": req.get("reason", "iMessage access required"),
        "status": status.status,
        "action_url": f"/api/skills/install/{IMESSAGE_SKILL_ID}",
        "action_label": "Set up iMessage",
        "install_plan_steps": plan_steps,
    }


def _imessage_action_url(bot_id: str) -> str:
    from ...skills.imessage_install import IMESSAGE_SKILL_ID
    return f"/api/skills/install/{IMESSAGE_SKILL_ID}"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402


def _build_provider() -> Provider:
    """Build the iMessage Provider instance."""
    return Provider(
        integration_ids=frozenset({"imessage"}),
        skill_id="imessage",
        is_satisfied=lambda bot_id: _imessage_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _imessage_build_missing_item(bot_id, req),
        action_url=_imessage_action_url,
        action_label="Set up iMessage",
    )


# Guard against double-registration
if not any("imessage" in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
