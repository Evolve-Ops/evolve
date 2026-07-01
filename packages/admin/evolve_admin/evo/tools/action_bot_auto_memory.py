"""Action tool — toggle OpenClaw's auto-memory writes for one bot.

OC's auto-memory is the per-bot markdown corpus under
``/Users/<bot>/.openclaw/workspace/memory/`` (MEMORY.md index, dated
entries, topical subdirs) plus the sibling embedding/FTS index at
``/Users/<bot>/.openclaw/memory/main.sqlite``. The Security →
Auto-Memory tab inventories it; this tool flips the kill-switch.

The canonical OC flag is ``plugins.slots.memory`` (oc-config-schema.txt
line 40562). Setting it to ``"none"`` disables the memory plugin
entirely — no writes, no retrieval. Omitting the key falls back to
OC's default ("builtin"). ``memorySearch.enabled`` is NOT the right
switch: it only kills search/retrieval, the bot keeps writing memory
files.

Why this tool exists: per the standing rule that every observation /
inference feature ships with a user-flippable opt-out
(``feedback_user_observation_optout``), the operator needs a way to
disable auto-memory per bot. The Auto-Memory tab in the admin UI
is the natural surface, but evo should be able to do it too — saying
"turn off memory on personal_bot" should just work, without the operator
having to remember which tab or which flag.

Risk tier: ``write_risky``. Touches openclaw.json and kickstarts the
gateway (~3-5 s offline). Same shape as
``action.bot.pin_plugin_version``.

Wipe path: NOT in this tool. Disabling stops new writes; existing
content stays until the operator removes it. A separate
``action.bot.wipe_auto_memory`` would be the right home for the
destructive path — deferred until there's a clear request shape.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


_BOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _bot_exists(network_path: Path, bot_id: str) -> bool:
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
        return bot_id in (net.get("bots") or {})
    except Exception:  # noqa: BLE001
        return False


def _set_auto_memory_handler(
    network_path: Path,
    bot_id: str,
    enabled: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    """Call the admin daemon's PUT /api/admin/bot/<id>/auto-memory endpoint.

    The endpoint owns the openclaw.json read/mutate/write + kickstart
    + intent recording. This tool just routes the call and surfaces
    the response to the model.
    """
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "PUT",
        f"/api/admin/bot/{bot_id}/auto-memory",
        body={"enabled": enabled, "reason": reason or ""},
        timeout=20.0,  # safe_write runs OC config validate + kickstart
    )
    if not used_daemon:
        return {
            "ok": False,
            "error": (
                "admin daemon unreachable — auto-memory toggle requires "
                "the daemon's privileged write path. Try again after the "
                "daemon is back, or have the operator flip "
                "plugins.slots.memory directly in the Auto-Memory tab."
            ),
            "bot_id": bot_id,
        }
    if status != 200 or not isinstance(body, dict):
        return {
            "ok": False,
            "error": (
                f"admin daemon returned status {status}"
                + (f": {body.get('error')}" if isinstance(body, dict) and body.get("error") else "")
            ),
            "bot_id": bot_id,
            "daemon_body": body,
        }

    # Verify hint: the Security → Auto-Memory tab inventories every
    # bot's file count + size. After disable, file count stops growing;
    # after enable, it resumes within a few turns. The inventory
    # endpoint runs on demand from the UI; the model can re-call this
    # tool to confirm the slot value flipped.
    result: dict[str, Any] = {
        "ok": bool(body.get("ok", True)),
        "bot_id": bot_id,
        "enabled": enabled,
        "status": body.get("status"),
        "prior_value": body.get("prior_value"),
        "new_value": body.get("new_value"),
        "reason": reason,
        "verify_via": {
            "tool": "pod_state.bots",
            "args": {"bot_id": bot_id},
            "expect": (
                "bot returns to online status after the gateway restart "
                "(~3-5 s). The Auto-Memory tab's per-bot file count "
                "stops advancing when disabled; resumes when re-enabled."
            ),
        },
    }
    if "kickstart_warning" in body:
        result["kickstart_warning"] = body["kickstart_warning"]
    return result


def _set_auto_memory_validate(
    network_path: Path,
    bot_id: str,
    enabled: bool | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: bot exists, bot_id syntactically valid, enabled supplied."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _BOT_ID_RE.fullmatch(bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot_id {bot_id!r} is invalid (ASCII letters/digits/-/_, ≤64)"
            ),
        }
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' is not registered in network.json; "
                "operator may have typo'd the id or the bot was removed"
            ),
        }
    if not isinstance(enabled, bool):
        return {
            "ok": False,
            "reason": "enabled (bool) is required — true to enable, false to disable",
        }
    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "enabled": enabled,
            "estimated_duration_s": 8,  # safe_write validate + kickstart
        },
    }


SET_AUTO_MEMORY_TOOL = Tool(
    name="action.bot.set_auto_memory",
    description=(
        "Enable or disable OpenClaw's auto-memory for one bot. Flips "
        "``plugins.slots.memory`` between ``\"none\"`` (disabled — no "
        "MEMORY.md writes, no embedding-index updates, no retrieval) "
        "and the OC default (enabled). Writes openclaw.json via "
        "``safe_write_bot_config`` (validates the config before "
        "touching the live file), kickstarts the gateway so OC "
        "reloads (~3-5 s offline), and records a config-intent so "
        "future drift generators don't re-propose the inverse.\n\n"
        "Use when the operator says 'turn off memory on <bot>', "
        "'stop <bot> from remembering', 'disable auto-memory on "
        "<bot>', or wants to re-enable it after a privacy review. "
        "DO NOT use ``memorySearch.enabled`` for this — that flag "
        "only kills retrieval, not writes. The slot flag is the "
        "real kill-switch.\n\n"
        "Wipe is a separate action. This tool only stops new writes; "
        "existing memory content under "
        "``/Users/<bot>/.openclaw/workspace/memory/`` stays in place "
        "until the operator removes it. The Security → Auto-Memory "
        "tab shows the current footprint per bot.\n\n"
        "Idempotent: if the slot already matches the requested state, "
        "returns ``status: \"already\"`` with no write, no kickstart."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "enabled": {
                "type": "boolean",
                "description": (
                    "true to enable auto-memory (OC default behavior); "
                    "false to disable (plugins.slots.memory = \"none\")."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason for the audit log / config-intent "
                    "record (e.g. 'operator privacy review' or "
                    "'temporarily disabled while debugging memory bloat')."
                ),
            },
        },
        "required": ["bot_id", "enabled"],
        "additionalProperties": False,
    },
    handler=_set_auto_memory_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_set_auto_memory_validate,
    tags=("action", "bot", "memory", "privacy"),
    authorization_scope="admin",
)


register(SET_AUTO_MEMORY_TOOL)
