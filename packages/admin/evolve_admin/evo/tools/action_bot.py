"""Action tools — bot lifecycle.

Phase 1.4 step 2 from spec §13.8. The direct-action tools for bot
operations that aren't proposal-shaped (per spec §13.2 Pattern B):

* ``action.bot.restart(bot_id, reason?)`` — kicks the bot's gateway
  via ``deploy.restart_gateway``. Reversible (it's just a restart);
  binary "did it run" semantics; no audit-worthy diff. Write_risky
  because misuse can briefly take a bot offline.

* ``action.bot.redeploy(bot_id, reason?)`` — runs ``deploy_bot``
  end-to-end. Pulls latest config, refreshes plugin install,
  restarts the gateway. Heavier; longer-running (~30s). Used to
  apply Evolve upgrades to a single bot, or to restore drifted
  config from canonical templates.

Both tools call into the existing deploy.py infrastructure — same
code path the admin UI calls when the operator clicks the inline
buttons on the Dashboard. The tool just wraps the subprocess /
function call with structured input + validate + verify_via.

Risk tier: ``write_risky``. Auto-runs only under ``auto`` authority.
Under ``ask`` or ``auto-small``, the proxy stages a confirmation
button (or evo, per AGENTS.md teaching, stages an in-chat offer).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register
from ..admin_http import urlopen_admin

log = logging.getLogger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _import_deploy():
    """Lazy import deploy module. Heavy module (imports analyzer
    subpackages on the way in), so import only when needed."""
    try:
        from evolve_admin import deploy
        return deploy
    except ImportError as exc:
        log.warning("action.bot.*: deploy module unavailable: %s", exc)
        return None


def _bot_exists(network_path: Path, bot_id: str) -> bool:
    """Sanity check: is bot_id in network.json::bots? Apply-blocker
    if the operator typo'd or the bot was removed."""
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
        return bot_id in (net.get("bots") or {})
    except Exception:  # noqa: BLE001
        return False


# ─── action.bot.restart ──────────────────────────────────────────────────────


def _restart_handler(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Kick the bot's gateway via launchctl.

    Phase E.3.4 path: try the admin daemon's existing
    ``POST /api/admin/gateway/<bot>/restart`` endpoint first (admin UI's
    Restart button uses it; same code). Falls back to in-process
    ``deploy.restart_gateway`` on socket failure — preserves operation
    during the migration window where evo runs as the ``evolve`` user
    and still has the sudo grant.
    """
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    # Primary path: admin daemon HTTP endpoint.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", f"/api/admin/gateway/{bot_id}/restart",
        body={"reason": reason or "evo restarted via tool"},
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        return {
            "ok": bool(body.get("ok", True)),
            "bot_id": bot_id,
            "reason": reason or "evo restarted via tool",
            "verify_via": {
                "tool": "pod_state.bots",
                "args": {"bot_id": bot_id},
                "expect": "status=online (the projection's derived field)",
            },
            "via": "admin_daemon",
        }
    if used_daemon and status is not None:
        # Daemon answered but rejected — return the daemon's response
        # rather than running the legacy path (the answer is authoritative).
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "bot_id": bot_id,
            "daemon_body": body,
        }

    # Fallback: in-process restart (migration window only).
    deploy = _import_deploy()
    if deploy is None:
        return {"ok": False, "error": "deploy module unavailable"}

    try:
        deploy.restart_gateway(bot_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.bot.restart: restart_gateway raised")
        return {
            "ok": False,
            "error": f"restart failed: {type(exc).__name__}: {exc}",
            "bot_id": bot_id,
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "reason": reason or "evo restarted via tool",
        # Post-action verify hint (spec §3.7 lever #4).
        # After restart, pod_state.bots should show the bot back
        # online — gateway probe + recent metric file.
        "verify_via": {
            "tool": "pod_state.bots",
            "args": {"bot_id": bot_id},
            "expect": "status=online (the projection's derived field)",
        },
        "via": "in_process_fallback",
    }


def _restart_validate(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm bot exists in network.json. No state-machine
    check needed — restart is idempotent and works regardless of
    current bot state (online, offline, in-recovery)."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' is not registered in network.json; "
                "operator may have typo'd the id or the bot was removed"
            ),
        }
    return {
        "ok": True,
        "context": {"bot_id": bot_id},
    }


RESTART_TOOL = Tool(
    name="action.bot.restart",
    description=(
        "Restart a bot's OpenClaw gateway. Kicks the launchctl daemon "
        "(``launchctl kickstart -k``). Quick (~3–5 seconds), reversible "
        "(it's just a restart), idempotent. Use when a gateway is "
        "misbehaving, after a config change that needs to be picked up, "
        "or when the operator asks 'restart team_bot_a'. The bot is briefly "
        "offline during the restart (~2–3 s)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for the audit log.",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_restart_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_restart_validate,
    tags=("action", "bot", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(RESTART_TOOL)


# ─── action.bot.redeploy ─────────────────────────────────────────────────────


def _redeploy_handler(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Run deploy_bot for the target bot.

    Phase E.3.5 path: try existing `POST /api/deploy` over the admin
    daemon socket first; fall back to in-process deploy_bot on socket
    failure. /api/deploy runs deploy_bot daemon-side with the same
    role/port/dry_run signature.
    """
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    # Look up role/port (passed to both paths).
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
        bot_cfg = (net.get("bots") or {}).get(bot_id) or {}
        role = bot_cfg.get("role") or "member"
        port = bot_cfg.get("port")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    # Primary path: admin daemon /api/deploy.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/deploy",
        body={"bot_id": bot_id, "role": role, "port": port, "dry_run": False},
        timeout=120.0,  # deploy_bot is ~30s; allow headroom
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        return {
            "ok": bool(body.get("ok", body.get("success", True))),
            "bot_id": bot_id,
            "role": role,
            "reason": reason or "evo redeployed via tool",
            "steps": body.get("steps", []),
            "errors": body.get("errors", []),
            "verify_via": {
                "tool": "pod_state.bots",
                "args": {"bot_id": bot_id},
                "expect": "status=online AND evolve_synced=true",
            },
            "via": "admin_daemon",
        }
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "bot_id": bot_id,
            "daemon_body": body,
        }

    # Fallback: in-process deploy_bot (migration window only).
    deploy = _import_deploy()
    if deploy is None:
        return {"ok": False, "error": "deploy module unavailable"}

    try:
        result = deploy.deploy_bot(
            bot_id=bot_id,
            role=role,
            port=port,
            network_path=network_path,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.bot.redeploy: deploy_bot raised")
        return {
            "ok": False,
            "error": f"deploy failed: {type(exc).__name__}: {exc}",
            "bot_id": bot_id,
        }

    return {
        "ok": bool(getattr(result, "success", False)),
        "bot_id": bot_id,
        "role": role,
        "reason": reason or "evo redeployed via tool",
        "steps": getattr(result, "steps", []),
        "errors": getattr(result, "errors", []),
        # Post-action verify (spec §3.7 lever #4). After redeploy,
        # the bot should be online with the latest evolve_version.
        "verify_via": {
            "tool": "pod_state.bots",
            "args": {"bot_id": bot_id},
            "expect": "status=online AND evolve_synced=true",
        },
    }


def _redeploy_validate(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm bot exists. Same gate as restart."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' is not registered in network.json"
            ),
        }
    return {
        "ok": True,
        "context": {"bot_id": bot_id, "estimated_duration_s": 30},
    }


REDEPLOY_TOOL = Tool(
    name="action.bot.redeploy",
    description=(
        "Redeploy a bot — runs the full deploy_bot pipeline: pull "
        "config, refresh plugin install, restart gateway, run smoke "
        "audit. Heavier than restart (~30s). Use to apply an Evolve "
        "version upgrade to one bot, restore drifted config to "
        "canonical templates, or fix a bot whose plugin install got "
        "corrupted. The bot is offline during the restart phase "
        "(~3–5 s within the ~30s total)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for the audit log.",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_redeploy_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_redeploy_validate,
    tags=("action", "bot", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(REDEPLOY_TOOL)


# ─── action.bot.remove (destructive) ─────────────────────────────────────────


def _remove_handler(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Remove a bot from the network — graceful retire path.

    Wraps ``retire.retire_bot`` via the admin daemon's
    ``/api/lifecycle/retire`` endpoint: archives the bot's data, stops
    every per-bot daemon (gateway + Evolve infra), removes the bot
    from network.json. Does NOT delete the bot's home dir, macOS user
    account, or the archive — all reversible.

    Pre-2026-05-31 this called ``/api/remove`` which silently left 7+
    LaunchDaemons running. The new endpoint matches what this tool's
    docstring always promised: clean removal without nuking user data.

    For the full irreversible nuke (delete macOS user + home dir) the
    operator must use the admin UI's typed-DELETE flow or the
    ``evolve-admin delete-bot`` CLI — never via evo, because the
    typed-DELETE gate isn't completable from a tool call.

    Defense-in-depth: even though the destructive tier always renders
    a confirm button, we ALSO require an explicit ``confirm: true`` in
    the args. The LLM has to actively choose to pass that flag — a
    misclick on the confirm button alone doesn't trigger the
    destructive path.
    """
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    if not confirm:
        return {
            "ok": False,
            "error": (
                "action.bot.remove requires 'confirm': true. This is a "
                "destructive operation — the bot's daemons stop, its "
                "data is archived, and it is removed from network.json."
            ),
        }

    # Phase E.3.5 + 2026-05-31 redesign: admin daemon /api/lifecycle/retire
    # (replaces the broken /api/remove which only deleted a legacy plist).
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/lifecycle/retire",
        body={"botId": bot_id, "dryRun": False},
        timeout=120.0,  # retire archives data; allow more time than the
                        # old /api/remove which did almost nothing
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        return {
            "ok": bool(body.get("ok", body.get("success", True))),
            "bot_id": bot_id,
            "reason": reason or "evo removed via tool",
            "steps": body.get("steps", []),
            "errors": body.get("errors", []),
            "via": "admin_daemon",
        }
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "bot_id": bot_id,
            "daemon_body": body,
        }

    # Fallback: in-process retire_bot (mirrors the new daemon endpoint).
    # The old fallback was deploy.remove_bot, which only stripped a
    # legacy plist + the network entry while leaving 7+ daemons running.
    # We swapped to retire_bot so the fallback's blast radius matches
    # the daemon path's promise.
    try:
        from evolve_admin.retire import retire_bot as _retire_bot
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"retire module unavailable: {type(exc).__name__}: {exc}",
        }

    try:
        result = _retire_bot(bot_id, network_path=network_path, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.bot.remove: retire_bot raised")
        return {
            "ok": False,
            "error": f"remove failed: {type(exc).__name__}: {exc}",
            "bot_id": bot_id,
        }

    return {
        "ok": bool(getattr(result, "success", False)),
        "bot_id": bot_id,
        "reason": reason or "evo removed via tool",
        "steps": getattr(result, "steps", []),
        "errors": getattr(result, "errors", []),
        # Post-action verify (spec §3.7 lever #4). After remove,
        # pod_state.bots should no longer list this bot.
        "verify_via": {
            "tool": "pod_state.bots",
            "args": {},
            "expect": f"bot '{bot_id}' no longer appears in the bots list",
        },
    }


def _remove_validate(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Dry-run: bot must exist and confirm must be set.

    The confirm gate is checked at validate-time too so the proxy can
    surface a clear reason in the chat before rendering the confirmation
    button — "you need to pass confirm: true" is more helpful than a
    silent button.
    """
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' is not registered in network.json; "
                "operator may have typo'd the id or the bot was already removed"
            ),
        }
    if not confirm:
        return {
            "ok": False,
            "reason": (
                "action.bot.remove requires confirm: true (defense in "
                "depth against accidental destructive calls)"
            ),
        }
    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "irreversible_summary": (
                "launchd job unloaded; bot dropped from network.json. "
                "Home dir and workspace preserved."
            ),
        },
    }


REMOVE_TOOL = Tool(
    name="action.bot.remove",
    description=(
        "Remove a bot from the pod. Unloads its launchd gateway daemon "
        "and removes the entry from network.json. Does NOT delete the "
        "bot's home directory or workspace — operator can restore from "
        "/Users/<bot>/.openclaw/* manually if needed. Requires explicit "
        "confirm: true in args (destructive ops always render a confirm "
        "button AND require the flag — defense in depth)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "reason": {
                "type": "string",
                "description": "Reason for the audit log (recommended).",
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Must be true to actually remove. Defense-in-depth "
                    "redundancy with the destructive-tier confirm button."
                ),
                "default": False,
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_remove_handler,
    risk_tier=RiskTier.DESTRUCTIVE,
    validate=_remove_validate,
    tags=("action", "bot", "lifecycle", "destructive"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(REMOVE_TOOL)


# ─── action.bot.repair_acls ──────────────────────────────────────────────────


def _repair_acls_handler(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Re-run ``deploy.set_evolve_read_acl(bot_id)`` for an existing bot.

    Same code path the deploy flow runs; idempotent. Useful when:

      * A bot was deployed before a new file got added to the ACL
        helper (e.g. ``.zshrc`` ACL added 2026-05-20 — pre-existing
        bots tripped the ``audit_identity / .zshrc unreadable``
        signal until this was run).

      * An audit-readability signal is firing because evolve user
        can't read something it should be able to. Cheaper than a
        full redeploy.

    Quick (~1-2 seconds), reversible (just re-applies POSIX ACLs that
    are already documented), idempotent. Write_safe — no service
    interruption, no destructive operation.
    """
    deploy = _import_deploy()
    if deploy is None:
        return {"ok": False, "error": "deploy module unavailable"}

    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    try:
        deploy.set_evolve_read_acl(bot_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.bot.repair_acls: set_evolve_read_acl raised")
        return {
            "ok": False,
            "error": f"ACL repair failed: {type(exc).__name__}: {exc}",
            "bot_id": bot_id,
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "reason": reason or "evo repaired ACLs via tool",
        # Post-action verify hint. The next audit sweep will re-read
        # the bot's .zshrc / .openclaw/ files; the audit_identity
        # signal should clear (move firing → resolved). Lever #4
        # (verify_via) — the model confirms by checking signals.
        "verify_via": {
            "tool": "pod_state.signals.firing",
            "args": {"bot_id": bot_id, "producer": "audit"},
            "expect": (
                "any audit_identity signal for this bot on .zshrc "
                "or .openclaw/ readability should resolve by the "
                "next audit sweep (~minutes)"
            ),
        },
    }


def _repair_acls_validate(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm bot exists. ACL repair is idempotent + non-
    destructive, so no state-machine check is needed — operator can
    always run this."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' is not registered in network.json; "
                "operator may have typo'd the id or the bot was removed"
            ),
        }
    return {"ok": True, "context": {"bot_id": bot_id}}


REPAIR_ACLS_TOOL = Tool(
    name="action.bot.repair_acls",
    description=(
        "Re-apply the deploy-time ACL grants for a bot. Runs "
        "``deploy.set_evolve_read_acl(bot_id)`` — same code the "
        "deploy flow runs. Use when an ``audit_identity / .zshrc "
        "unreadable`` (or similar audit-readability) signal is "
        "firing, OR when the operator says 'fix the audit perms on "
        "X' / 'why can't the audit read X's .zshrc'. Cheaper than a "
        "full redeploy (~1-2 s vs ~30 s). Idempotent + non-"
        "destructive — no service interruption. DO NOT propose a "
        "``chmod o+r`` shell snippet for these signals; this tool "
        "uses ACL (evolve-only) instead, which is the right security "
        "posture."
    ),
    wire_description=(
        "Re-apply the deploy-time ACL grants for a bot. Use when an "
        "audit-readability signal is firing (e.g. ``audit_identity / "
        ".zshrc unreadable``) or the operator asks to fix audit perms "
        "on a bot. Idempotent, non-destructive, ~1-2 s — cheaper than "
        "a full redeploy. DO NOT propose a ``chmod o+r`` shell "
        "snippet; this ACL path is the right security posture."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason for the audit log (e.g. 'clear "
                    "audit_identity signal for admin_bot')."
                ),
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_repair_acls_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_repair_acls_validate,
    tags=("action", "bot", "acl", "audit"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(REPAIR_ACLS_TOOL)


# ─── action.bot.rescan_apps ──────────────────────────────────────────────────


def _rescan_apps_handler(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Trigger an app inventory scan for ``bot_id`` via the admin
    server's existing ``POST /api/applications/scan?bot=<id>`` endpoint.

    Same code path as the operator clicking **Run Scan** on the
    Applications page. The scan runs as a background thread on the
    admin server — this tool returns as soon as the scan is kicked
    off (the lock file is written and the subprocess is spawned).
    Operator can verify completion via ``pod_state.app_scan_status``
    after a few minutes; the chip on the bot's dashboard tile
    should clear on the next tile-metrics refresh.

    Why this isn't a proposal: scanning is operator-initiated
    maintenance, fully reversible (the scan only writes manifest
    files in the bot's own workspace + a status stamp in shared_dir),
    bounded blast radius (one bot at a time). ``WRITE_SAFE`` matches
    the spec's risk tiering.

    Why it's an HTTP wrapper, not a direct subprocess call: the
    admin server owns the lockfile + the post-scan stamping step
    (server.py:_monitor at line 1611). Going through HTTP keeps
    that orchestration in one place and avoids duplicating the
    lock + stamping logic.
    """
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    try:
        from evolve_admin.config import (
            load_network,
            resolve_admin_base_url,
        )
    except ImportError as exc:
        return {"ok": False, "error": f"evolve_admin.config not importable: {exc}"}

    try:
        net = load_network(network_path)
        base_url = resolve_admin_base_url(net).rstrip("/")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve admin base URL: {exc}"}

    import urllib.error
    import urllib.request
    import json as _json

    url = f"{base_url}/api/applications/scan?bot={bot_id}"
    try:
        req = urllib.request.Request(
            url, method="POST",
            headers={"Accept": "application/json"},
            data=b"",
        )
        with urlopen_admin(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        return {
            "ok": False,
            "error": (
                f"admin server returned HTTP {exc.code}"
                + (f": {err_body[:200]}" if err_body else "")
            ),
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": f"admin server unreachable at {url}: {exc.reason}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"HTTP POST failed: {exc}"}

    try:
        payload = _json.loads(body)
    except _json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"admin server returned non-JSON: {exc}",
        }

    # The endpoint returns {"status": "started" | "already_running"}.
    status = (payload or {}).get("status")
    if status not in ("started", "already_running"):
        return {
            "ok": False,
            "error": f"unexpected admin response: {payload!r}",
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "status": status,
        "reason": reason or "evo triggered rescan via tool",
        # Verify hint — after a few minutes the chip should clear.
        # Model can poll pod_state.app_scan_status and check
        # chip_firing_reason.chip_firing == false (with a recent
        # mtime).
        "verify_via": {
            "tool": "pod_state.app_scan_status",
            "args": {"bot_id": bot_id},
            "expect": (
                "chip_firing == false + recent mtime (scan takes "
                "~30s to a few minutes depending on app count)"
            ),
        },
    }


def _rescan_apps_validate(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm bot exists in network.json. The endpoint
    handles already-running-scan gracefully (returns ``already_running``
    instead of 4xx), so we don't need a state-machine check here."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": f"bot '{bot_id}' is not registered in network.json",
        }
    return {"ok": True, "context": {"bot_id": bot_id}}


RESCAN_APPS_TOOL = Tool(
    name="action.bot.rescan_apps",
    description=(
        "Trigger an app inventory scan on ``bot_id``. Same code "
        "path the Applications page's **Run Scan** button uses. "
        "The scan runs as a background subprocess (~30s to a few "
        "minutes depending on app count) and updates the canonical "
        "``.scan-status.json`` when done — which clears the "
        "``scan_needed`` chip on the bot's dashboard tile on the "
        "next tile-metrics refresh. Use this when the operator says "
        "'I've been running scans but the warning persists' "
        "(diagnose first with ``pod_state.app_scan_status`` to "
        "understand why the existing scans aren't clearing the chip "
        "— it may be a stamping bug, not a missing scan). Returns "
        "immediately after kicking off; verify completion via "
        "``pod_state.app_scan_status`` a few minutes later."
    ),
    wire_description=(
        "Trigger an app inventory scan for one bot — same path as the "
        "Applications page's Run Scan button. Runs in the background "
        "(~30s to a few minutes) and clears the ``scan_needed`` chip "
        "when done. If the operator says the warning persists despite "
        "scans, diagnose first with ``pod_state.app_scan_status``. "
        "Returns immediately; verify completion there a few minutes "
        "later."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason for the audit log (e.g. 'operator "
                    "reports scan_needed chip persists for evolve')."
                ),
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_rescan_apps_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_rescan_apps_validate,
    tags=("action", "bot", "applications", "scan"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(RESCAN_APPS_TOOL)


# ─── action.bot.backup_workspace ────────────────────────────────────────────

# The backup mechanism: each bot's workspace is a git repo
# (/Users/<bot>/.openclaw/workspace/). ``analyzer/backup.py`` stages
# openclaw.json + metrics, commits, and pushes to the bot's configured
# backupRepoUrl. It runs nightly at 02:00 via a launchd job installed
# by ``deploy._install_launchd_backup`` with label
# ``ai.evolve.<bot>.backup``.
#
# On-demand path: ``sudo /bin/launchctl kickstart -k system/ai.evolve.<bot>.backup``
# starts the same job immediately. The ``-k`` ensures we restart even
# if (rarely) the scheduled run is in flight. The sudoers grant at
# ``/etc/sudoers.d/evolve`` already covers the wildcard
# ``system/ai.evolve.*`` (setup_wizard §8), so no new grant is needed.
#
# This wrapper exists specifically because evo kept inventing shell
# snippets ("``sudo -u team_bot_a git push``", "``launchctl kickstart``") in
# transcripts when operators asked to back something up on demand.
# The tool replaces those fabrications with a registered action that
# matches the production code path.


def _backup_plist_path(bot_id: str) -> Path:
    """Where the bot's backup launchd plist lives on disk. Validates
    by file existence — if the plist is missing, the bot doesn't have
    backup configured (typically: setup never ran ``backup-init``
    against this bot's GitHub remote)."""
    return Path(f"/Library/LaunchDaemons/ai.evolve.{bot_id}.backup.plist")


def _backup_workspace_handler(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Trigger an on-demand workspace backup for one bot.

    Phase E.3.4 path: try the admin daemon's existing
    ``POST /api/backup/cloud/run`` endpoint first. Falls back to
    an in-process kickstart via the Scheduler seam on socket failure
    (same ``sudo /bin/launchctl kickstart -k`` argv as before — the
    seam doesn't widen what this gateway subprocess can do; privilege
    still comes from the sudoers grant).

    (Path was ``/api/maintenance/backup-now`` before Phase 4f
    consolidation; the alias still works, but new code uses the
    canonical ``/api/backup/cloud/*`` namespace.)
    """
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    # Primary path: admin daemon endpoint.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/backup/cloud/run",
        body={"botId": bot_id},
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        return {
            "ok": bool(body.get("ok", True)),
            "bot_id": bot_id,
            "launchd_label": f"ai.evolve.{bot_id}.backup",
            "reason": reason or "evo triggered backup via tool",
            "note": (
                "Backup is running in the background (typically 10-30s). "
                "Same code path as the nightly 02:00 job. Check completion "
                "via pod_state.backup_status."
            ),
            "via": "admin_daemon",
        }
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "bot_id": bot_id,
            "daemon_body": body,
        }

    # Fallback: in-process launchctl kickstart (migration window only).
    plist = _backup_plist_path(bot_id)
    if not plist.exists():
        return {
            "ok": False,
            "error": (
                f"no backup launchd plist at {plist} — bot '{bot_id}' "
                f"is not configured for git backup. Run ``evolve-admin "
                f"deploy {bot_id}`` to install the daemon, then "
                f"initialize the remote via the Security page's "
                f"Backup-init affordance."
            ),
        }

    from ...runtime import get_scheduler
    ok, out = get_scheduler().restart(f"ai.evolve.{bot_id}.backup")
    if not ok:
        # The seam's runner never raises — timeouts/OSErrors surface as
        # (rc=1, message), so this single branch covers both the old
        # exception path and the old non-zero-exit path.
        return {
            "ok": False,
            "error": f"launchctl kickstart failed: {out.strip() or 'no output'}",
            "bot_id": bot_id,
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "launchd_label": f"ai.evolve.{bot_id}.backup",
        "reason": reason or "evo triggered backup via tool",
        "note": (
            "Backup is running in the background (typically 10-30s). "
            "Same code path as the nightly 02:00 job. Check completion "
            "via pod_state.backup_status."
        ),
        # Post-action verify (spec §3.7 lever #4): the backup commit
        # advances when the job completes. Calling the status tool
        # after ~30s should show last_commit_at moved forward.
        "verify_via": {
            "tool": "pod_state.backup_status",
            "args": {"bot_id": bot_id},
            "expect": (
                "last_commit_at advances within ~30s; "
                "last_commit_subject starts with '[backup]'"
            ),
        },
    }


def _backup_workspace_validate(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: bot must exist + plist must be installed.

    Both gates are validate-time so the operator sees a clear reason
    in chat ("backup not configured — run deploy first") instead of a
    button that errors at click time."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' is not registered in network.json; "
                "operator may have typo'd the id or the bot was removed"
            ),
        }
    plist = _backup_plist_path(bot_id)
    if not plist.exists():
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' isn't set up for backup yet — there's "
                f"no launchd plist installed. This is a one-time setup "
                f"step (the documented command is ``evolve-admin deploy "
                f"{bot_id}``, run from the operator's admin terminal), "
                f"plus a ``backupRepoUrl`` entry in network.json. Surface "
                f"it as a setup workflow, not a shell snippet to copy-"
                f"paste from chat."
            ),
        }
    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "estimated_duration_s": 30,
            "launchd_label": f"ai.evolve.{bot_id}.backup",
        },
    }


BACKUP_WORKSPACE_TOOL = Tool(
    name="action.bot.backup_workspace",
    description=(
        "Trigger an on-demand workspace backup for one bot. Wraps the "
        "same nightly launchd job (``ai.evolve.<bot>.backup``, scheduled "
        "at 02:00) that runs ``analyzer/backup.py`` as the bot user: "
        "stages openclaw.json + metrics into ``evolve-backup/``, commits "
        "with a ``[backup]`` prefix, pushes to the bot's configured "
        "``backupRepoUrl``. Use when the operator says 'back up X now', "
        "'force a backup', or wants to confirm a config change made it "
        "into git before tomorrow's nightly run. Quick to trigger "
        "(~1s); the actual backup runs in background (~10-30s). DO NOT "
        "propose a shell snippet invoking ``analyzer/backup.py`` or "
        "``launchctl kickstart`` directly — this tool is the registered "
        "path and it's identical to what the operator would type."
    ),
    wire_description=(
        "Trigger an on-demand workspace backup for one bot — same "
        "launchd job as the nightly 02:00 backup (commits + pushes to "
        "the bot's configured backupRepoUrl). Use when the operator "
        "says 'back up X now' or wants a config change in git before "
        "tonight's run. Runs in background (~10-30s). DO NOT propose "
        "a shell snippet (``analyzer/backup.py`` or ``launchctl "
        "kickstart``) — this tool is the registered path."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason for the audit log (e.g. 'verify "
                    "drift acceptance committed' or 'pre-update "
                    "snapshot before plugin change')."
                ),
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_backup_workspace_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_backup_workspace_validate,
    tags=("action", "bot", "backup"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(BACKUP_WORKSPACE_TOOL)
