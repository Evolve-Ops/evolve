"""
evolve_admin.oauth.providers.slack_provider — Slack OAuth provider.

Wraps ``evolve_admin.skills.slack_install`` in the ``Provider`` interface.
This is the first non-Google OAuth provider in Evolve and validates the
V2.1-1 substrate with a real second provider.

Registration
------------
At import time this module appends a ``Provider`` instance to
``PROVIDER_REGISTRY`` in ``providers/__init__.py``.  The registry is a
singleton list — importing this module twice is safe (the guard at the
bottom checks for duplicate registration).

V2.1-6 note
-----------
The ``Provider`` interface was designed with OAuth in mind (``action_url``
returns a URL to start the flow, ``build_missing_item`` describes what's
missing). Slack fits cleanly. If V2.1-6 (iMessage — non-OAuth) needs a
different shape, the interface extension should happen in V2.1-6, not here.
The interface is unchanged from V2.1-1.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ── Provider implementation ───────────────────────────────────────────────────


def _slack_is_satisfied(bot_id: str, *, shared_dir: "Path | None" = None) -> bool:
    """Return True if the bot has a valid Slack token."""
    from ...skills import slack_install

    try:
        status = slack_install.resolve_status(bot_id, shared_dir=shared_dir)
        return status.token_state == "valid"
    except Exception as exc:
        log.warning("slack_provider.is_satisfied: check failed for %s: %s", bot_id, exc)
        return False


def _slack_build_missing_item(
    bot_id: str,
    req: dict,
    *,
    shared_dir: "Path | None" = None,
) -> "dict | None":
    """Return a missing-item dict for Slack, or None if satisfied.

    Delegates status resolution to ``slack_install.resolve_status`` and plan
    building to ``slack_install.build_install_plan``. The returned shape
    matches the ``missing[]`` element expected by ``evaluate_install_prerequisites``.
    """
    from ...skills import slack_install

    try:
        status = slack_install.resolve_status(bot_id, shared_dir=shared_dir)
    except Exception as exc:
        log.warning("slack_provider: Slack status check failed for %s: %s", bot_id, exc)
        return {
            "integration_id": slack_install.SLACK_SKILL_ID,
            "skill_id": slack_install.SLACK_SKILL_ID,
            "display_name": req.get("display_name", "Slack"),
            "reason": req.get("reason", "Slack workspace access required"),
            "status": "unknown",
            "action_url": "/api/skills/install/slack",
            "action_label": "Set up Slack workspace",
            "install_plan_steps": [],
        }

    if status.token_state == "valid":
        return None  # satisfied — nothing missing

    try:
        plan = slack_install.build_install_plan(status)
        plan_steps = [s.to_dict() for s in plan]
    except Exception:
        plan_steps = []

    return {
        "integration_id": slack_install.SLACK_SKILL_ID,
        "skill_id": slack_install.SLACK_SKILL_ID,
        "display_name": req.get("display_name", "Slack"),
        "reason": req.get("reason", "Slack workspace access required"),
        "status": status.token_state,
        "action_url": "/api/skills/install/slack",
        "action_label": "Set up Slack workspace",
        "install_plan_steps": plan_steps,
    }


def _slack_action_url(bot_id: str) -> str:
    return "/api/skills/install/slack"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402


def _build_provider() -> Provider:
    """Build the Slack Provider instance."""
    return Provider(
        integration_ids=frozenset({"slack"}),
        skill_id="slack",
        is_satisfied=lambda bot_id: _slack_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _slack_build_missing_item(bot_id, req),
        action_url=_slack_action_url,
        action_label="Set up Slack workspace",
    )


# Guard against double-registration if this module is somehow imported twice.
if not any("slack" in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
