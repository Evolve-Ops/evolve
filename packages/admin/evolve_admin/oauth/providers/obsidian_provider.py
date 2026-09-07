"""
evolve_admin.oauth.providers.obsidian_provider — Obsidian filesystem provider.

Wraps ``evolve_admin.skills.obsidian_install`` in the ``Provider`` interface.
Obsidian is a ``kind=filesystem`` skill: no OAuth, but the canonical install
is a vetted MCP server (``@modelcontextprotocol/server-filesystem`` scoped to
the user's chosen vault path). Satisfaction is determined by whether the bot's
openclaw.json has ``mcp.servers.obsidian`` configured — the same authoritative
signal the rewired install module emits (see ``resolve_status_mcp``).

Registration
------------
At import time this module appends a ``Provider`` instance to
``PROVIDER_REGISTRY``.  The guard at the bottom prevents double-registration.

History
-------
* 2026-05-30: Obsidian rewired as an MCP-backed install (PR #1817). The skill
  id became ``obsidian_vault`` (the value of ``OBSIDIAN_SKILL_ID``); the
  canonical "configured" signal moved from ``workspace/note_taker/config.json``
  (the old paste-token dead-end) to ``mcp.servers.obsidian`` in openclaw.json.
  This provider was updated to match: it now registers for ``obsidian_vault``
  and detects via ``resolve_status_mcp``. Gallery manifests that previously
  declared ``"id": "obsidian"`` should be updated to ``"obsidian_vault"`` —
  see ``gallery/note-taker/p-f14e9562.json`` for the reference.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# ── Provider implementation ───────────────────────────────────────────────────


def _obsidian_resolve_status(bot_id: str):
    """Resolve install status using the MCP-aware status resolver.

    Mirrors the wiring in ``web/server.py`` so the orchestrator and the
    Skills install routes agree on what "configured" means.
    """
    from ...skills import _oc_install_common as _oc_common
    from ...skills.obsidian_install import resolve_status_mcp
    return resolve_status_mcp(bot_id, read_oc_config=_oc_common.read_oc_config)


def _obsidian_is_satisfied(bot_id: str) -> bool:
    """Return True iff ``mcp.servers.obsidian`` is configured on the bot."""
    try:
        status = _obsidian_resolve_status(bot_id)
        return status.status == "active"
    except Exception as exc:
        log.warning("obsidian_provider.is_satisfied: check failed for %s: %s", bot_id, exc)
        return False


def _obsidian_build_missing_item(bot_id: str, req: dict) -> "dict | None":
    """Return a missing-item dict for Obsidian, or None if satisfied."""
    from ...skills.obsidian_install import (
        OBSIDIAN_SKILL_ID,
        build_install_plan,
    )

    try:
        status = _obsidian_resolve_status(bot_id)
    except Exception as exc:
        log.warning("obsidian_provider: status check failed for %s: %s", bot_id, exc)
        return {
            "integration_id": OBSIDIAN_SKILL_ID,
            "skill_id": OBSIDIAN_SKILL_ID,
            "display_name": req.get("display_name", "Obsidian Vault"),
            "reason": req.get("reason", "Obsidian vault access required"),
            "status": "unknown",
            "action_url": f"/api/skills/install/{OBSIDIAN_SKILL_ID}",
            "action_label": "Set up Obsidian Vault",
            "install_plan_steps": [],
        }

    if status.status == "active":
        return None  # satisfied — mcp.servers.obsidian is present

    try:
        plan = build_install_plan(status)
        plan_steps = [s.to_dict() for s in plan]
    except Exception:
        plan_steps = []

    return {
        "integration_id": OBSIDIAN_SKILL_ID,
        "skill_id": OBSIDIAN_SKILL_ID,
        "display_name": req.get("display_name", "Obsidian Vault"),
        "reason": req.get("reason", "Obsidian vault access required"),
        "status": status.status,
        "action_url": f"/api/skills/install/{OBSIDIAN_SKILL_ID}",
        "action_label": "Set up Obsidian Vault",
        "install_plan_steps": plan_steps,
    }


def _obsidian_action_url(bot_id: str) -> str:
    from ...skills.obsidian_install import OBSIDIAN_SKILL_ID
    return f"/api/skills/install/{OBSIDIAN_SKILL_ID}"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402
from ...skills.obsidian_install import OBSIDIAN_SKILL_ID  # noqa: E402


def _build_provider() -> Provider:
    """Build the Obsidian Provider instance."""
    return Provider(
        integration_ids=frozenset({OBSIDIAN_SKILL_ID}),
        skill_id=OBSIDIAN_SKILL_ID,
        is_satisfied=lambda bot_id: _obsidian_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _obsidian_build_missing_item(bot_id, req),
        action_url=_obsidian_action_url,
        action_label="Set up Obsidian Vault",
    )


# Guard against double-registration if this module is somehow imported twice
if not any(OBSIDIAN_SKILL_ID in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
