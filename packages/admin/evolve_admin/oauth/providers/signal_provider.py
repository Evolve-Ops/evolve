"""evolve_admin.oauth.providers.signal_provider — Signal non-OAuth provider.

.. warning::
   **LICENSING REVIEW REQUIRED BEFORE MERGE.** See the module-level note
   in ``evolve_admin.skills.signal_install`` for the signal-cli /
   libsignal copyleft posture. The provider registration is harmless on
   its own (no signal-cli code is touched), but if the review returns
   FAIL the registration call here should be commented out alongside the
   install module withdrawal.

Wraps ``evolve_admin.skills.signal_install`` in the ``Provider`` interface.
Signal is a non-OAuth, non-token skill — pairing is a QR-code device-link
handshake (signal-cli linked device), and the credential is the set of
session files in the per-bot per-number configDir.

Same shape as ``whatsapp_provider`` and ``imessage_provider`` (both
non-OAuth precedents). The orchestrator is agnostic to OAuth vs filesystem
vs device-link satisfaction checks.

Registration is the standard ``PROVIDER_REGISTRY.append`` side-effect,
guarded against double-registration so re-imports during reload are
idempotent.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _signal_is_satisfied(bot_id: str) -> bool:
    """True if the bot has Signal fully configured and probe-reachable."""
    from ...skills.signal_install import resolve_status
    try:
        status = resolve_status(bot_id)
        return status.status == "active"
    except Exception as exc:
        log.warning(
            "signal_provider.is_satisfied: check failed for %s: %s",
            bot_id, exc,
        )
        return False


def _signal_build_missing_item(bot_id: str, req: dict) -> "dict | None":
    """Return a missing-item dict for Signal, or None if satisfied."""
    from ...skills.signal_install import (
        resolve_status,
        build_install_plan,
        SIGNAL_SKILL_ID,
    )

    try:
        status = resolve_status(bot_id)
    except Exception as exc:
        log.warning(
            "signal_provider: status check failed for %s: %s", bot_id, exc,
        )
        return {
            "integration_id": SIGNAL_SKILL_ID,
            "skill_id": SIGNAL_SKILL_ID,
            "display_name": req.get("display_name", "Signal"),
            "reason": req.get("reason", "Signal access required"),
            "status": "unknown",
            "action_url": f"/api/skills/install/{SIGNAL_SKILL_ID}",
            "action_label": "Set up Signal",
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
        "integration_id": SIGNAL_SKILL_ID,
        "skill_id": SIGNAL_SKILL_ID,
        "display_name": req.get("display_name", "Signal"),
        "reason": req.get("reason", "Signal access required"),
        "status": status.status,
        "action_url": f"/api/skills/install/{SIGNAL_SKILL_ID}",
        "action_label": "Set up Signal",
        "install_plan_steps": plan_steps,
    }


def _signal_action_url(bot_id: str) -> str:
    from ...skills.signal_install import SIGNAL_SKILL_ID
    return f"/api/skills/install/{SIGNAL_SKILL_ID}"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402


def _build_provider() -> Provider:
    return Provider(
        integration_ids=frozenset({"signal"}),
        skill_id="signal",
        is_satisfied=lambda bot_id: _signal_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _signal_build_missing_item(bot_id, req),
        action_url=_signal_action_url,
        action_label="Set up Signal",
    )


if not any("signal" in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
