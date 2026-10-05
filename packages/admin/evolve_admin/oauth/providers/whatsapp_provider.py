"""evolve_admin.oauth.providers.whatsapp_provider — WhatsApp non-OAuth provider.

Wraps ``evolve_admin.skills.whatsapp_install`` in the ``Provider`` interface.
WhatsApp is a non-OAuth, non-token skill — pairing is a QR-code device-link
handshake (Baileys / WhatsApp Web), and the credential is the set of session
files in the per-bot authDir.

Same shape as ``imessage_provider`` (the closest precedent for a non-OAuth
skill). The orchestrator is already agnostic to OAuth vs filesystem vs
device-link satisfaction checks.

Registration is the standard ``PROVIDER_REGISTRY.append`` side-effect, guarded
against double-registration so re-imports during reload are idempotent.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _whatsapp_is_satisfied(bot_id: str) -> bool:
    """True if the bot has WhatsApp fully configured and probe-reachable."""
    from ...skills.whatsapp_install import resolve_status
    try:
        status = resolve_status(bot_id)
        return status.status == "active"
    except Exception as exc:
        log.warning(
            "whatsapp_provider.is_satisfied: check failed for %s: %s",
            bot_id, exc,
        )
        return False


def _whatsapp_build_missing_item(bot_id: str, req: dict) -> "dict | None":
    """Return a missing-item dict for WhatsApp, or None if satisfied."""
    from ...skills.whatsapp_install import (
        resolve_status,
        build_install_plan,
        WHATSAPP_SKILL_ID,
    )

    try:
        status = resolve_status(bot_id)
    except Exception as exc:
        log.warning(
            "whatsapp_provider: status check failed for %s: %s", bot_id, exc,
        )
        return {
            "integration_id": WHATSAPP_SKILL_ID,
            "skill_id": WHATSAPP_SKILL_ID,
            "display_name": req.get("display_name", "WhatsApp"),
            "reason": req.get("reason", "WhatsApp access required"),
            "status": "unknown",
            "action_url": f"/api/skills/install/{WHATSAPP_SKILL_ID}",
            "action_label": "Set up WhatsApp",
            "install_plan_steps": [],
        }

    if status.status == "active":
        return None

    try:
        plan = build_install_plan(status)
        plan_steps = [s.to_dict() for s in plan]
    except Exception:
        plan_steps = []

    return {
        "integration_id": WHATSAPP_SKILL_ID,
        "skill_id": WHATSAPP_SKILL_ID,
        "display_name": req.get("display_name", "WhatsApp"),
        "reason": req.get("reason", "WhatsApp access required"),
        "status": status.status,
        "action_url": f"/api/skills/install/{WHATSAPP_SKILL_ID}",
        "action_label": "Set up WhatsApp",
        "install_plan_steps": plan_steps,
    }


def _whatsapp_action_url(bot_id: str) -> str:
    from ...skills.whatsapp_install import WHATSAPP_SKILL_ID
    return f"/api/skills/install/{WHATSAPP_SKILL_ID}"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402


def _build_provider() -> Provider:
    return Provider(
        integration_ids=frozenset({"whatsapp"}),
        skill_id="whatsapp",
        is_satisfied=lambda bot_id: _whatsapp_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _whatsapp_build_missing_item(bot_id, req),
        action_url=_whatsapp_action_url,
        action_label="Set up WhatsApp",
    )


if not any("whatsapp" in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
