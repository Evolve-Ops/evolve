"""Action tools — circuit breakers (Phase 4a).

Spec: internal/spec-circuit-breakers-2026-05-21.md §3.2.

Wraps the breakers.store + breakers_enforce primitives that were
introduced in #1392 / #1399 / #1401 / #1403. After this module
registers, evo can trip and reset breakers from any chat surface
(its own Telegram channel, the admin UI proxy, cross-bot dispatch).

Five tools:

  action.bot.trip_breaker      destructive  per-bot trip
  action.bot.reset_breaker     write_risky  per-bot reset
  action.pod.trip_breaker      destructive  pod-wide trip
  action.pod.reset_breaker     write_risky  pod-wide reset
  pod_state.breakers           read         list active trips

Both trip tools are destructive: even though L1 cost trips are
recoverable (the bot keeps responding to user chat), tripping a
breaker is a meaningful operator action that should always be
explicit. Mirrors the pause_all precedent — pause is reversible
but still destructive-tier to defeat misclicks.

Reset tools are write_risky rather than write_safe because
clearing a breaker un-blocks whatever the trip was protecting
against. If an auto-trip is in effect because the cost-spike
detector flagged a real runaway, casually resetting it lets the
runaway resume. The proxy will show a confirm button under
authority=ask, but auto-execute under authority=auto.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register


log = logging.getLogger(__name__)


# ── Lazy imports ─────────────────────────────────────────────────────────────


def _import_store():
    """breakers.store ships in the installed evolve-analyzer package."""
    try:
        from breakers import store  # noqa: WPS433
        return store
    except ImportError as exc:
        log.warning("action.breakers.*: store unavailable: %s", exc)
        return None


def _import_enforce():
    try:
        from evolve_admin import breakers_enforce
        return breakers_enforce
    except ImportError as exc:
        log.warning("action.breakers.*: breakers_enforce unavailable: %s", exc)
        return None


def _shared_dir_from_network(network_path: Path) -> Path | None:
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
        return Path(net.get("shared_dir") or "/Users/Shared/evolve")
    except Exception as exc:  # noqa: BLE001
        log.warning("action.breakers.*: shared_dir resolution failed: %s", exc)
        return None


def _network(network_path: Path) -> dict | None:
    try:
        from evolve_admin.config import load_network
        return load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("action.breakers.*: network.json read failed: %s", exc)
        return None


# ── Common validation helpers ────────────────────────────────────────────────


_VALID_TYPES = ("cost", "full")
_BOT_RESERVED_SCOPES = ("pod",)


def _validate_breaker_type(breaker_type: str) -> str | None:
    """Return a reason string on rejection, or None on accept."""
    if breaker_type not in _VALID_TYPES:
        return (
            f"breaker_type must be one of {_VALID_TYPES}; "
            f"got {breaker_type!r}"
        )
    return None


def _bot_in_network(network_path: Path, bot_id: str) -> bool:
    net = _network(network_path)
    if net is None:
        return False
    bots = net.get("bots") or {}
    return bot_id in bots


# ── action.bot.trip_breaker ──────────────────────────────────────────────────


def _trip_bot_handler(
    network_path: Path,
    bot_id: str,
    breaker_type: str,
    duration: str = "24h",
    reason: str = "manual trip via evo",
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "error": (
                "action.bot.trip_breaker requires 'confirm': true. "
                "Tripping a breaker materially changes the bot's behavior "
                "(L1: blocks background activity; L2: takes the gateway "
                "down). Defense in depth against accidental trips."
            ),
        }

    reason_msg = _validate_breaker_type(breaker_type)
    if reason_msg:
        return {"ok": False, "error": reason_msg}

    if bot_id in _BOT_RESERVED_SCOPES:
        return {
            "ok": False,
            "error": (
                f"bot_id={bot_id!r} is a reserved scope; use "
                f"action.pod.trip_breaker for pod-wide trips"
            ),
        }
    if not _bot_in_network(network_path, bot_id):
        return {
            "ok": False,
            "error": (
                f"bot '{bot_id}' is not registered in network.json; "
                "check spelling or run pod_state.bots"
            ),
        }

    # Phase E.3.5 path: admin daemon endpoint first.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/admin/breakers/trip",
        body={
            "scope": bot_id, "breaker_type": breaker_type,
            "duration": duration, "reason": reason,
        },
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        body["verify_via"] = {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"{bot_id}/{breaker_type} appears in active trips",
        }
        body.setdefault("via", "admin_daemon")
        return body
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_body": body,
        }

    # Fallback: in-process trip+enforce (migration window only).
    store = _import_store()
    enforce = _import_enforce()
    if store is None or enforce is None:
        return {"ok": False, "error": "breakers backend unavailable"}

    shared_dir = _shared_dir_from_network(network_path)
    if shared_dir is None:
        return {"ok": False, "error": "shared_dir not resolved"}

    try:
        dur = store.parse_duration(duration)
    except ValueError as exc:
        return {"ok": False, "error": f"bad duration {duration!r}: {exc}"}

    try:
        record = store.trip(
            shared_dir=shared_dir,
            scope=bot_id,
            breaker_type=breaker_type,
            duration=dur,
            initiated_by="evo:tool",
            reason=reason,
        )
    except ValueError as exc:
        return {"ok": False, "error": f"trip rejected: {exc}"}

    net = _network(network_path) or {}
    try:
        enforce_result = enforce.enforce_trip(
            scope=bot_id, breaker_type=breaker_type, network=net,
            reason=record.reason, expires_at_iso=record.expires_at,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error": f"state written but enforce failed: {exc}",
            "trip_id": record.trip_id,
        }

    return {
        "ok": enforce_result.ok,
        "scope": bot_id,
        "breaker_type": breaker_type,
        "trip_id": record.trip_id,
        "expires_at": record.expires_at,
        "reason": record.reason,
        "enforce": enforce_result.to_dict(),
        "verify_via": {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"{bot_id}/{breaker_type} appears in active trips",
        },
    }


def _trip_bot_validate(
    network_path: Path,
    bot_id: str,
    breaker_type: str,
    duration: str = "24h",
    reason: str = "manual trip via evo",
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "reason": (
                "action.bot.trip_breaker requires confirm: true "
                "(defense-in-depth against accidental trips)"
            ),
        }
    type_problem = _validate_breaker_type(breaker_type)
    if type_problem:
        return {"ok": False, "reason": type_problem}
    if bot_id in _BOT_RESERVED_SCOPES:
        return {
            "ok": False,
            "reason": (
                f"bot_id={bot_id!r} is a reserved scope; use "
                f"action.pod.trip_breaker for pod-wide trips"
            ),
        }
    if not _bot_in_network(network_path, bot_id):
        return {
            "ok": False,
            "reason": f"bot '{bot_id}' not registered in network.json",
        }
    store = _import_store()
    if store is not None:
        try:
            store.parse_duration(duration)
        except ValueError as exc:
            return {"ok": False, "reason": f"bad duration {duration!r}: {exc}"}
    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "breaker_type": breaker_type,
            "duration": duration,
            "effect": (
                "blocks background activity (gateway stays up; user chat works)"
                if breaker_type == "cost"
                else "takes the gateway down (no chat, no background)"
            ),
        },
    }


TRIP_BOT_TOOL = Tool(
    name="action.bot.trip_breaker",
    description=(
        "Trip a circuit breaker on a named bot. Two types: "
        "'cost' (blocks background activity — heartbeats, crons, "
        "scheduler-spawned turns — but user messaging through normal "
        "channels continues), 'full' (takes the gateway down "
        "entirely; bot is offline until reset or duration expires). "
        "Duration is '1h' / '4h' / '24h' / '7d' / '30m' / 'indefinite'. "
        "Requires confirm: true. Reversible via action.bot.reset_breaker."
    ),
    wire_description=(
        "Trip a circuit breaker on a bot: 'cost' blocks background activity "
        "(heartbeats/crons) while user messaging continues; 'full' takes "
        "the gateway down until reset or expiry. duration: "
        "'30m'/'1h'/'4h'/'24h'/'7d'/'indefinite'. Requires confirm: true; "
        "reversible via action.bot.reset_breaker."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "breaker_type": {
                "type": "string",
                "enum": list(_VALID_TYPES),
                "description": (
                    "'cost' blocks background activity only; 'full' takes "
                    "the gateway down entirely."
                ),
            },
            "duration": {
                "type": "string",
                "description": (
                    "Trip duration: '1h' / '4h' / '24h' / '7d' / '30m' or "
                    "'indefinite'. Default '24h'."
                ),
                "default": "24h",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Required reason for the audit log. What is the trip "
                    "responding to?"
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to actually trip.",
                "default": False,
            },
        },
        "required": ["bot_id", "breaker_type", "reason"],
        "additionalProperties": False,
    },
    handler=_trip_bot_handler,
    risk_tier=RiskTier.DESTRUCTIVE,
    validate=_trip_bot_validate,
    tags=("action", "breaker", "lifecycle", "destructive"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)
register(TRIP_BOT_TOOL)


# ── action.bot.reset_breaker ─────────────────────────────────────────────────


def _reset_bot_handler(
    network_path: Path,
    bot_id: str,
    breaker_type: str,
    reason: str = "manual reset via evo",
) -> dict[str, Any]:
    reason_msg = _validate_breaker_type(breaker_type)
    if reason_msg:
        return {"ok": False, "error": reason_msg}
    if bot_id in _BOT_RESERVED_SCOPES:
        return {
            "ok": False,
            "error": (
                f"bot_id={bot_id!r} is a reserved scope; use "
                f"action.pod.reset_breaker for pod-wide resets"
            ),
        }
    if not _bot_in_network(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    # Phase E.3.5: admin daemon endpoint first.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/admin/breakers/reset",
        body={
            "scope": bot_id, "breaker_type": breaker_type, "reason": reason,
        },
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        body["verify_via"] = {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"{bot_id}/{breaker_type} is no longer in active trips",
        }
        body.setdefault("via", "admin_daemon")
        return body
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_body": body,
        }

    # Fallback: in-process reset+enforce.
    store = _import_store()
    enforce = _import_enforce()
    if store is None or enforce is None:
        return {"ok": False, "error": "breakers backend unavailable"}

    shared_dir = _shared_dir_from_network(network_path)
    if shared_dir is None:
        return {"ok": False, "error": "shared_dir not resolved"}

    try:
        prior = store.reset(
            shared_dir=shared_dir,
            scope=bot_id,
            breaker_type=breaker_type,
            initiated_by="evo:tool",
            reason=reason,
        )
    except ValueError as exc:
        return {"ok": False, "error": f"reset rejected: {exc}"}

    if prior is None:
        return {
            "ok": True,
            "scope": bot_id,
            "breaker_type": breaker_type,
            "was_tripped": False,
            "note": "no active trip — reset is a no-op",
        }

    net = _network(network_path) or {}
    try:
        enforce_result = enforce.enforce_reset(
            scope=bot_id, breaker_type=breaker_type, network=net,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error": f"state cleared but enforce failed: {exc}",
            "was_tripped": True,
            "prior_trip_id": prior.trip_id,
        }

    return {
        "ok": enforce_result.ok,
        "scope": bot_id,
        "breaker_type": breaker_type,
        "was_tripped": True,
        "prior_trip_id": prior.trip_id,
        "enforce": enforce_result.to_dict(),
        "verify_via": {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"{bot_id}/{breaker_type} is no longer in active trips",
        },
    }


def _reset_bot_validate(
    network_path: Path,
    bot_id: str,
    breaker_type: str,
    reason: str = "manual reset via evo",
) -> dict[str, Any]:
    type_problem = _validate_breaker_type(breaker_type)
    if type_problem:
        return {"ok": False, "reason": type_problem}
    if bot_id in _BOT_RESERVED_SCOPES:
        return {
            "ok": False,
            "reason": (
                f"bot_id={bot_id!r} is a reserved scope; use "
                f"action.pod.reset_breaker"
            ),
        }
    if not _bot_in_network(network_path, bot_id):
        return {
            "ok": False,
            "reason": f"bot '{bot_id}' not registered in network.json",
        }
    return {"ok": True, "context": {"bot_id": bot_id, "breaker_type": breaker_type}}


RESET_BOT_TOOL = Tool(
    name="action.bot.reset_breaker",
    description=(
        "Reset (clear) a per-bot circuit breaker. For type='full' this "
        "brings the gateway back up immediately; for type='cost' it "
        "stops the plugin from vetoing background turns. No-op if the "
        "breaker isn't tripped."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "breaker_type": {
                "type": "string",
                "enum": list(_VALID_TYPES),
                "description": "Which breaker to reset.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for the audit log.",
            },
        },
        "required": ["bot_id", "breaker_type"],
        "additionalProperties": False,
    },
    handler=_reset_bot_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_reset_bot_validate,
    tags=("action", "breaker", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)
register(RESET_BOT_TOOL)


# ── action.pod.trip_breaker ──────────────────────────────────────────────────


def _trip_pod_handler(
    network_path: Path,
    breaker_type: str,
    duration: str = "24h",
    reason: str = "manual pod-wide trip via evo",
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "error": (
                "action.pod.trip_breaker requires 'confirm': true. "
                "Pod-wide trips affect every bot; defense in depth "
                "against accidental fleet outage."
            ),
        }

    reason_msg = _validate_breaker_type(breaker_type)
    if reason_msg:
        return {"ok": False, "error": reason_msg}

    # Phase E.3.5: admin daemon endpoint first.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/admin/breakers/trip",
        body={
            "scope": "pod", "breaker_type": breaker_type,
            "duration": duration, "reason": reason,
        },
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        body["verify_via"] = {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"pod/{breaker_type} appears in active trips",
        }
        body.setdefault("via", "admin_daemon")
        return body
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_body": body,
        }

    # Fallback: in-process trip+enforce.
    store = _import_store()
    enforce = _import_enforce()
    if store is None or enforce is None:
        return {"ok": False, "error": "breakers backend unavailable"}

    shared_dir = _shared_dir_from_network(network_path)
    if shared_dir is None:
        return {"ok": False, "error": "shared_dir not resolved"}

    try:
        dur = store.parse_duration(duration)
    except ValueError as exc:
        return {"ok": False, "error": f"bad duration {duration!r}: {exc}"}

    try:
        record = store.trip(
            shared_dir=shared_dir,
            scope="pod",
            breaker_type=breaker_type,
            duration=dur,
            initiated_by="evo:tool",
            reason=reason,
        )
    except ValueError as exc:
        return {"ok": False, "error": f"trip rejected: {exc}"}

    net = _network(network_path) or {}
    try:
        enforce_result = enforce.enforce_trip(
            scope="pod", breaker_type=breaker_type, network=net,
            reason=record.reason, expires_at_iso=record.expires_at,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error": f"state written but enforce failed: {exc}",
            "trip_id": record.trip_id,
        }

    return {
        "ok": enforce_result.ok,
        "scope": "pod",
        "breaker_type": breaker_type,
        "trip_id": record.trip_id,
        "expires_at": record.expires_at,
        "reason": record.reason,
        "enforce": enforce_result.to_dict(),
        "verify_via": {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"pod/{breaker_type} appears in active trips",
        },
    }


def _trip_pod_validate(
    network_path: Path,
    breaker_type: str,
    duration: str = "24h",
    reason: str = "manual pod-wide trip via evo",
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "reason": (
                "action.pod.trip_breaker requires confirm: true "
                "(pod-wide blast radius)"
            ),
        }
    type_problem = _validate_breaker_type(breaker_type)
    if type_problem:
        return {"ok": False, "reason": type_problem}
    store = _import_store()
    if store is not None:
        try:
            store.parse_duration(duration)
        except ValueError as exc:
            return {"ok": False, "reason": f"bad duration {duration!r}: {exc}"}
    bot_count = -1
    net = _network(network_path)
    if net is not None:
        bot_count = len(net.get("bots") or {})
    return {
        "ok": True,
        "context": {
            "scope": "pod",
            "breaker_type": breaker_type,
            "duration": duration,
            "estimated_bots_affected": bot_count,
        },
    }


TRIP_POD_TOOL = Tool(
    name="action.pod.trip_breaker",
    description=(
        "Trip a circuit breaker pod-wide (every bot). Two types: 'cost' "
        "(blocks background activity on every bot), 'full' (takes every "
        "gateway down — equivalent of action.pod.pause_all). "
        "Requires confirm: true. Reversible via action.pod.reset_breaker."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "breaker_type": {
                "type": "string",
                "enum": list(_VALID_TYPES),
                "description": (
                    "'cost' blocks background activity pod-wide; 'full' "
                    "takes every gateway down."
                ),
            },
            "duration": {
                "type": "string",
                "description": (
                    "Trip duration ('1h' / '24h' / '7d' / 'indefinite' / etc)."
                ),
                "default": "24h",
            },
            "reason": {
                "type": "string",
                "description": "Required reason for the audit log.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to actually trip.",
                "default": False,
            },
        },
        "required": ["breaker_type", "reason"],
        "additionalProperties": False,
    },
    handler=_trip_pod_handler,
    risk_tier=RiskTier.DESTRUCTIVE,
    validate=_trip_pod_validate,
    tags=("action", "pod", "breaker", "lifecycle", "destructive"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)
register(TRIP_POD_TOOL)


# ── action.pod.reset_breaker ─────────────────────────────────────────────────


def _reset_pod_handler(
    network_path: Path,
    breaker_type: str,
    reason: str = "manual pod-wide reset via evo",
) -> dict[str, Any]:
    reason_msg = _validate_breaker_type(breaker_type)
    if reason_msg:
        return {"ok": False, "error": reason_msg}

    # Phase E.3.5: admin daemon endpoint first.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/admin/breakers/reset",
        body={
            "scope": "pod", "breaker_type": breaker_type, "reason": reason,
        },
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        body["verify_via"] = {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"pod/{breaker_type} is no longer in active trips",
        }
        body.setdefault("via", "admin_daemon")
        return body
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_body": body,
        }

    # Fallback: in-process reset+enforce.
    store = _import_store()
    enforce = _import_enforce()
    if store is None or enforce is None:
        return {"ok": False, "error": "breakers backend unavailable"}

    shared_dir = _shared_dir_from_network(network_path)
    if shared_dir is None:
        return {"ok": False, "error": "shared_dir not resolved"}

    try:
        prior = store.reset(
            shared_dir=shared_dir,
            scope="pod",
            breaker_type=breaker_type,
            initiated_by="evo:tool",
            reason=reason,
        )
    except ValueError as exc:
        return {"ok": False, "error": f"reset rejected: {exc}"}

    if prior is None:
        return {
            "ok": True,
            "scope": "pod",
            "breaker_type": breaker_type,
            "was_tripped": False,
            "note": "no active pod-wide trip — reset is a no-op",
        }

    net = _network(network_path) or {}
    try:
        enforce_result = enforce.enforce_reset(
            scope="pod", breaker_type=breaker_type, network=net,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error": f"state cleared but enforce failed: {exc}",
            "was_tripped": True,
            "prior_trip_id": prior.trip_id,
        }

    return {
        "ok": enforce_result.ok,
        "scope": "pod",
        "breaker_type": breaker_type,
        "was_tripped": True,
        "prior_trip_id": prior.trip_id,
        "enforce": enforce_result.to_dict(),
        "verify_via": {
            "tool": "pod_state.breakers",
            "args": {},
            "expect": f"pod/{breaker_type} is no longer in active trips",
        },
    }


def _reset_pod_validate(
    network_path: Path,
    breaker_type: str,
    reason: str = "manual pod-wide reset via evo",
) -> dict[str, Any]:
    type_problem = _validate_breaker_type(breaker_type)
    if type_problem:
        return {"ok": False, "reason": type_problem}
    return {"ok": True, "context": {"scope": "pod", "breaker_type": breaker_type}}


RESET_POD_TOOL = Tool(
    name="action.pod.reset_breaker",
    description=(
        "Reset the pod-wide breaker for a given type. Brings affected "
        "gateways back up (for 'full') or stops plugin veto (for 'cost'). "
        "No-op if no pod-wide trip is active for that type."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "breaker_type": {
                "type": "string",
                "enum": list(_VALID_TYPES),
                "description": "Which pod-wide breaker to reset.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for the audit log.",
            },
        },
        "required": ["breaker_type"],
        "additionalProperties": False,
    },
    handler=_reset_pod_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_reset_pod_validate,
    tags=("action", "pod", "breaker", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)
register(RESET_POD_TOOL)


# ── pod_state.breakers (read companion) ──────────────────────────────────────


def _breakers_state_handler(
    network_path: Path,
    include_expired: bool = False,
) -> dict[str, Any]:
    """List currently-active breakers across the pod.

    Cheap read — scans {shared_dir}/breakers/. Used as verify_via for
    trip/reset and as a standalone "what's tripped right now?" query.
    """
    store = _import_store()
    if store is None:
        return {"ok": False, "error": "breakers backend unavailable"}

    shared_dir = _shared_dir_from_network(network_path)
    if shared_dir is None:
        return {"ok": False, "error": "shared_dir not resolved"}

    records = (
        store.list_all(shared_dir)
        if include_expired
        else store.list_active(shared_dir)
    )

    trips = []
    for r in records:
        trips.append({
            "scope": r.bot_id,            # bot_id or "pod"
            "type": r.type,
            "trip_id": r.trip_id,
            "tripped_at": r.tripped_at,
            "expires_at": r.expires_at,
            "initiated_by": r.initiated_by,
            "reason": r.reason,
            "expired": store.is_expired(r),
        })

    return {
        "ok": True,
        "active_count": sum(1 for t in trips if not t["expired"]),
        "trips": trips,
    }


BREAKERS_STATE_TOOL = Tool(
    name="pod_state.breakers",
    description=(
        "List currently-active circuit breakers across the pod. Returns "
        "scope (bot_id or 'pod'), type ('cost' or 'full'), trip_id, "
        "timestamps, who initiated, and reason. By default excludes "
        "expired trips (heal.py auto-clears them on its next cycle); "
        "pass include_expired=true to see them too. Cheap read; use as "
        "verify_via after trip/reset or on its own to answer 'what's "
        "tripped right now?'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "include_expired": {
                "type": "boolean",
                "description": (
                    "If true, include expired trips that heal hasn't "
                    "reaped yet. Default false."
                ),
                "default": False,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=_breakers_state_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "breaker", "lifecycle"),
    # Circuit-breaker state is operationally visible — when a breaker
    # trips, member bots see the resulting behavioral change directly
    # (heartbeats stop, exec-deny etc.). Letting a cross-bot member ask
    # "what's tripped right now?" closes the causal opacity gap noted
    # in feedback_evolve_bot_llm_visibility.
    authorization_scope="anyone",
)
register(BREAKERS_STATE_TOOL)
