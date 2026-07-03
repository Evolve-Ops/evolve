"""Cross-bot roster mutation tools for evo.

Phase C.1 of docs/spec-user-roster-and-roles-2026-06-07.md (Path C — evo
cross-bot admin).

The pod admin (or future ``primary_user`` once Phase C.2 ships) can chat
with evo and say things like:

  "evo, block 555 on atlas telegram"
  "evo, set bob to primary_user on atlas"
  "evo, set atlas's telegram channel to auto_admit"

Each natural-language intent maps to one of the four tools registered
here. The tools forward to the admin-daemon endpoints in
``routes_bot_users`` (already shipped in Phase A) with an
``X-Requester-Identity`` header so the daemon's per-endpoint capability
check resolves the original requester on the TARGET bot — not on evo.

Authorization shape:

* ``authorization_scope="admin"`` — only admin_ui + telegram_operator
  surfaces can invoke. The cross_bot_member surface (a team member
  typing ``evo X`` into a member bot's channel) is refused at the evo
  dispatcher gate. Path B (primary_user talking to their own bot
  directly) is a separate code path coming in Phase C.2 — plugin-side
  tools that pass the speaker's identity in the same header.

* The admin-daemon side is defense in depth — even if the evo gate were
  bypassed, the daemon's ``_check_capability`` resolves the requester's
  role on the target bot and refuses with 403 if the capability isn't
  granted.

Risk tier is ``write_risky``: each call changes who can talk to a
bot or what they can do. Reversible (block → unblock, role flip back)
but the side-effects ship immediately on the channel.

Spec: docs/spec-user-roster-and-roles-2026-06-07.md §9 (Mutation API)
+ §10 (Admin paths — Path C).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...config import load_network
from ..admin_client import try_daemon_call
from . import RiskTier, Tool, register


log = logging.getLogger(__name__)


# Channels we surface — matches routes_bot_users.KNOWN_PROVIDERS so
# tool validation rejects unknown channel names before they reach the
# daemon. Kept as a tuple here (not imported) to avoid a circular import
# evo/tools → web/routes_bot_users → flask → ...
KNOWN_CHANNELS: tuple[str, ...] = ("telegram", "slack", "discord", "whatsapp")

# Roles assignable via PATCH role. ``admin`` is set by pod-admin claim in
# network.json (not assignable here); ``blocked`` is set via the dedicated
# block endpoint, not PATCH role. So the assignable set is two values.
ASSIGNABLE_ROLES: tuple[str, ...] = ("primary_user", "participant")

NEWCOMER_MODES: tuple[str, ...] = ("auto_admit", "require_approval", "closed")


# ── Requester-identity resolution ────────────────────────────────────────


def _resolve_requester_identity(
    network_path: Path,
) -> "tuple[str, str] | None":
    """Pick a stable_id to carry in the X-Requester-Identity header.

    For Phase C.1 the admin_ui + telegram_operator surfaces both
    represent the pod admin. We attribute the action to the first pod
    admin claim found in ``network.json::pod.admins.external_ids[telegram]``
    so the daemon's capability check resolves the right role.

    Returns ``(platform, stable_id)`` on success, ``None`` if no
    pod-admin claim exists (in which case the tool falls back to no
    header — the daemon's UI-trusted path takes over).
    """
    try:
        network = load_network(network_path)
    except Exception:  # noqa: BLE001
        return None
    pod = network.get("pod") or {}
    admins = pod.get("admins") or {}
    ext = admins.get("external_ids") or {}
    # Prefer telegram (most common operator surface), then slack, etc.
    for platform in ("telegram", "slack", "discord", "whatsapp"):
        ids = ext.get(platform) or []
        if ids:
            return (platform, str(ids[0]))
    return None


# ── Validation helpers ───────────────────────────────────────────────────


def _validate_bot_target(target_bot: str,
                         network_path: Path) -> "str | None":
    """Return None if the bot exists, else an error string."""
    if not target_bot or not isinstance(target_bot, str):
        return "target_bot is required and must be a non-empty string"
    try:
        network = load_network(network_path)
    except Exception:  # noqa: BLE001
        return None  # let the daemon side raise if network.json is broken
    if target_bot not in (network.get("bots") or {}):
        return f"unknown bot: {target_bot!r}"
    return None


def _validate_channel(channel: str) -> "str | None":
    if channel not in KNOWN_CHANNELS:
        return f"unknown channel: {channel!r}; expected one of {KNOWN_CHANNELS}"
    return None


# ── Tool handlers ────────────────────────────────────────────────────────


def _set_role_handler(
    *,
    target_bot: str,
    channel: str,
    stable_id: str,
    role: str,
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    if network_path is None:
        return {"ok": False, "error": "internal: network_path not bound"}
    requester = _resolve_requester_identity(network_path)
    headers: dict[str, str] = {"X-Requester-Source-Bot": "evolve"}
    if requester:
        headers["X-Requester-Identity"] = f"{requester[0]}:{requester[1]}"
    used, status, body = try_daemon_call(
        "PATCH",
        f"/api/admin/bots/{target_bot}/users/{channel}/{stable_id}",
        {"role": role},
        extra_headers=headers,
    )
    if not used:
        return {
            "ok": False,
            "error": "admin daemon unavailable",
            "hint": "the unix socket isn't reachable; check ai.evolve.evolve.admin-ui is running",
        }
    return _response_or_error(status, body, action=f"set role={role}")


def _block_handler(
    *,
    target_bot: str,
    channel: str,
    stable_id: str,
    reason: str = "",
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    if network_path is None:
        return {"ok": False, "error": "internal: network_path not bound"}
    requester = _resolve_requester_identity(network_path)
    headers: dict[str, str] = {"X-Requester-Source-Bot": "evolve"}
    if requester:
        headers["X-Requester-Identity"] = f"{requester[0]}:{requester[1]}"
    used, status, body = try_daemon_call(
        "POST",
        f"/api/admin/bots/{target_bot}/users/{channel}/{stable_id}/block",
        {"reason": reason or ""},
        extra_headers=headers,
    )
    if not used:
        return {"ok": False, "error": "admin daemon unavailable"}
    return _response_or_error(status, body, action="block")


def _unblock_handler(
    *,
    target_bot: str,
    channel: str,
    stable_id: str,
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    if network_path is None:
        return {"ok": False, "error": "internal: network_path not bound"}
    requester = _resolve_requester_identity(network_path)
    headers: dict[str, str] = {"X-Requester-Source-Bot": "evolve"}
    if requester:
        headers["X-Requester-Identity"] = f"{requester[0]}:{requester[1]}"
    used, status, body = try_daemon_call(
        "POST",
        f"/api/admin/bots/{target_bot}/users/{channel}/{stable_id}/unblock",
        None,
        extra_headers=headers,
    )
    if not used:
        return {"ok": False, "error": "admin daemon unavailable"}
    return _response_or_error(status, body, action="unblock")


def _newcomer_mode_handler(
    *,
    target_bot: str,
    channel: str,
    mode: str,
    default_engagement_surfaces: "list[str] | None" = None,
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    if network_path is None:
        return {"ok": False, "error": "internal: network_path not bound"}
    requester = _resolve_requester_identity(network_path)
    headers: dict[str, str] = {"X-Requester-Source-Bot": "evolve"}
    if requester:
        headers["X-Requester-Identity"] = f"{requester[0]}:{requester[1]}"
    payload: dict[str, Any] = {"mode": mode}
    if default_engagement_surfaces is not None:
        payload["default_engagement_surfaces"] = default_engagement_surfaces
    used, status, body = try_daemon_call(
        "PUT",
        f"/api/admin/bots/{target_bot}/channels/{channel}/newcomer_mode",
        payload,
        extra_headers=headers,
    )
    if not used:
        return {"ok": False, "error": "admin daemon unavailable"}
    return _response_or_error(status, body, action=f"set newcomer_mode={mode}")


def _response_or_error(status: "int | None", body: Any, *,
                       action: str) -> dict[str, Any]:
    """Normalize the daemon's response into a tool reply.

    Distinguishes the 403-forbidden path (capability check refused)
    from a 4xx-validation or 5xx-server-error so the LLM's reply can
    be precise about why the action didn't apply.
    """
    if status is None:
        return {"ok": False, "error": "no response from admin daemon"}
    if status == 403:
        detail = ((body or {}).get("detail") if isinstance(body, dict) else None)
        return {
            "ok": False,
            "error": "forbidden",
            "detail": detail or "capability gate denied the action",
            "http_status": 403,
        }
    if status >= 400:
        err = ((body or {}).get("error") if isinstance(body, dict)
               else None) or f"admin daemon returned HTTP {status}"
        return {"ok": False, "error": err, "http_status": status}
    return {
        "ok": True,
        "action": action,
        "result": body,
    }


# ── Validators ───────────────────────────────────────────────────────────


def _set_role_validate(
    *,
    target_bot: str,
    channel: str,
    stable_id: str,
    role: str,
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    if network_path is None:
        return {"ok": False, "error": "internal: network_path not bound"}
    err = _validate_bot_target(target_bot, network_path)
    if err:
        return {"ok": False, "error": err}
    err = _validate_channel(channel)
    if err:
        return {"ok": False, "error": err}
    if not stable_id or not isinstance(stable_id, str):
        return {"ok": False, "error": "stable_id is required"}
    if role not in ASSIGNABLE_ROLES:
        return {"ok": False, "error":
                f"role must be one of {ASSIGNABLE_ROLES}; "
                "use 'block' to set blocked, and admin role is set by "
                "the pod-admin claim in network.json"}
    return {"ok": True}


def _block_validate(
    *,
    target_bot: str,
    channel: str,
    stable_id: str,
    reason: str = "",
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    if network_path is None:
        return {"ok": False, "error": "internal: network_path not bound"}
    err = _validate_bot_target(target_bot, network_path)
    if err:
        return {"ok": False, "error": err}
    err = _validate_channel(channel)
    if err:
        return {"ok": False, "error": err}
    if not stable_id or not isinstance(stable_id, str):
        return {"ok": False, "error": "stable_id is required"}
    return {"ok": True}


def _unblock_validate(
    *,
    target_bot: str,
    channel: str,
    stable_id: str,
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    return _block_validate(
        target_bot=target_bot, channel=channel, stable_id=stable_id,
        network_path=network_path)


def _newcomer_mode_validate(
    *,
    target_bot: str,
    channel: str,
    mode: str,
    default_engagement_surfaces: "list[str] | None" = None,
    network_path: "Path | None" = None,
) -> dict[str, Any]:
    if network_path is None:
        return {"ok": False, "error": "internal: network_path not bound"}
    err = _validate_bot_target(target_bot, network_path)
    if err:
        return {"ok": False, "error": err}
    err = _validate_channel(channel)
    if err:
        return {"ok": False, "error": err}
    if mode not in NEWCOMER_MODES:
        return {"ok": False, "error":
                f"mode must be one of {NEWCOMER_MODES}"}
    if default_engagement_surfaces is not None:
        if not isinstance(default_engagement_surfaces, list):
            return {"ok": False, "error":
                    "default_engagement_surfaces must be a list"}
        for v in default_engagement_surfaces:
            if v not in ("group", "dm"):
                return {"ok": False, "error":
                        f"surface {v!r} not in ('group', 'dm')"}
    return {"ok": True}


# ── Tool registration ────────────────────────────────────────────────────


SET_ROLE_TOOL = Tool(
    name="action.roster.set_role",
    description=(
        "Set a user's role on a target bot. Use for assigning a user "
        "as primary_user (admin-equivalent for that bot) or demoting "
        "them back to participant. To block a user use action.roster.block "
        "instead — block is sticky and survives re-pairing. "
        "Admin role comes from network.json pod-admin claims, not this tool."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target_bot": {
                "type": "string",
                "description": "The bot whose roster you are modifying (e.g. 'atlas')",
            },
            "channel": {
                "type": "string",
                "enum": list(KNOWN_CHANNELS),
                "description": "Messaging platform (telegram, slack, etc.)",
            },
            "stable_id": {
                "type": "string",
                "description": (
                    "The user's platform-stable identifier (Telegram numeric "
                    "user_id, Slack user_id, Discord snowflake). NOT the "
                    "@handle — handles can change."
                ),
            },
            "role": {
                "type": "string",
                "enum": list(ASSIGNABLE_ROLES),
                "description": "Target role",
            },
        },
        "required": ["target_bot", "channel", "stable_id", "role"],
        "additionalProperties": False,
    },
    handler=_set_role_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_set_role_validate,
    tags=("action", "roster", "roles", "cross-bot"),
    authorization_scope="admin",
)


BLOCK_TOOL = Tool(
    name="action.roster.block",
    description=(
        "Block a user from a target bot — sticky deny that survives "
        "re-pairing. Use when a user has lost trust and the operator "
        "wants to prevent them from re-engaging via /start. For temporary "
        "removal that allows re-pairing, use the Disconnect button in "
        "the admin UI instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target_bot": {"type": "string"},
            "channel": {"type": "string", "enum": list(KNOWN_CHANNELS)},
            "stable_id": {"type": "string"},
            "reason": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "Optional one-line reason recorded in the audit log "
                    "(e.g. 'spamming the channel', 'compromised account')"
                ),
            },
        },
        "required": ["target_bot", "channel", "stable_id"],
        "additionalProperties": False,
    },
    handler=_block_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_block_validate,
    tags=("action", "roster", "block", "cross-bot"),
    authorization_scope="admin",
)


UNBLOCK_TOOL = Tool(
    name="action.roster.unblock",
    description=(
        "Remove a user from a target bot's block index. Does NOT re-admit "
        "them — they would have to send /start again and be approved through "
        "the normal pairing flow. Use when you want to allow a previously "
        "blocked user back, but want them to go through pairing again."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target_bot": {"type": "string"},
            "channel": {"type": "string", "enum": list(KNOWN_CHANNELS)},
            "stable_id": {"type": "string"},
        },
        "required": ["target_bot", "channel", "stable_id"],
        "additionalProperties": False,
    },
    handler=_unblock_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_unblock_validate,
    tags=("action", "roster", "unblock", "cross-bot"),
    authorization_scope="admin",
)


NEWCOMER_MODE_TOOL = Tool(
    name="action.channel.set_newcomer_mode",
    description=(
        "Set a target bot's per-channel newcomer mode — how the bot "
        "treats an unknown sender's /start. Modes: 'auto_admit' (group "
        "membership = automatic participant role; best for trusted private "
        "groups), 'require_approval' (existing pairing flow; admin "
        "approves each /start), 'closed' (operator-only roster management). "
        "Optionally also sets the channel's default engagement surfaces "
        "for auto-admitted users."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target_bot": {"type": "string"},
            "channel": {"type": "string", "enum": list(KNOWN_CHANNELS)},
            "mode": {
                "type": "string",
                "enum": list(NEWCOMER_MODES),
                "description": "newcomer-handling mode",
            },
            "default_engagement_surfaces": {
                "type": "array",
                "items": {"type": "string", "enum": ["group", "dm"]},
                "description": (
                    "Optional; surfaces a newly-admitted participant "
                    "is honored on. Default is platform-specific."
                ),
            },
        },
        "required": ["target_bot", "channel", "mode"],
        "additionalProperties": False,
    },
    handler=_newcomer_mode_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_newcomer_mode_validate,
    tags=("action", "channel", "config", "cross-bot"),
    authorization_scope="admin",
)


def init_for_network_path(network_path: Path) -> None:
    """Bind ``network_path`` into each tool's handler + validate closure.

    Mirrors action_feedback.init_for_shared_dir. Called by the admin
    server boot to make the tools functional after the registry has
    been seeded with the path.
    """
    for tool in (SET_ROLE_TOOL, BLOCK_TOOL, UNBLOCK_TOOL, NEWCOMER_MODE_TOOL):
        bound_handler = _bind_network_path(tool.handler, network_path)
        bound_validate = _bind_network_path(tool.validate, network_path)
        register(Tool(
            name=tool.name,
            description=tool.description,
            schema=tool.schema,
            handler=bound_handler,
            risk_tier=tool.risk_tier,
            validate=bound_validate,
            tags=tool.tags,
            authorization_scope=tool.authorization_scope,
        ))


def _bind_network_path(fn, network_path: Path):
    """Closure helper — keeps the signature flexible across handlers."""
    def _bound(**kwargs: Any) -> dict[str, Any]:
        return fn(network_path=network_path, **kwargs)
    return _bound


# Register the unbound tools at import time so the registry knows about
# them. ``init_for_network_path`` then re-registers each with the
# network_path bound for actual invocation.
register(SET_ROLE_TOOL)
register(BLOCK_TOOL)
register(UNBLOCK_TOOL)
register(NEWCOMER_MODE_TOOL)
