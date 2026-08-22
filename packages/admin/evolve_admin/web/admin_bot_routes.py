"""evolve_admin.web.admin_bot_routes — admin-daemon-side endpoints for evo tools.

Phase E.3 (docs/spec-evo-account-separation-2026-05-25.md) routes evo's
privileged tool calls through the admin daemon over a unix-socket
binding. This module registers the routes those tools call. Each route
gates on ``@require_trusted_peer`` so it's reachable only over the unix
socket from the configured trusted user(s).

Endpoints registered here:

  * C1 — ``GET /api/admin/bot/<bot_id>/openclaw-config`` (Phase E.3.1)
    Replaces evo's in-process ``config.bot`` tool's direct fs read of
    ``/Users/<bot>/.openclaw/openclaw.json``. Server runs the read +
    the redaction/projection helpers and returns the same JSON shape
    the tool returned previously.

  * C2 — ``GET /api/admin/bot/<bot_id>/backup-status`` (Phase E.3.2)
    Replaces evo's in-process ``pod_state(query="backup_status")`` tool's
    cross-bot git shellouts. Server runs the existing
    ``_backup_status_handler`` (which has ACL read access to every
    bot's workspace) and returns the same flat dict the tool returned.

  * C5 — ``POST /api/admin/infra/<daemon_id>/restart`` (Phase E.3.4)
    Replaces evo's in-process ``action.infra.daemon_restart`` tool's
    direct ``sudo /bin/launchctl kickstart -k system/ai.evolve.<id>``.
    Server validates daemon_id against the same allowlist the tool
    used and runs the kickstart admin-daemon-side (where the sudoers
    grant lives). The other action-tool endpoints
    (``/api/admin/gateway/<bot>/restart``, ``/api/recovery/...``,
    ``/api/backup/cloud/run`` (was ``/api/maintenance/backup-now``,
    aliased for back-compat),
    ``/api/arbiter/proposals/<id>/act``)
    already exist for the admin UI; this PR re-plumbs evo's tools to
    call them via the unix socket without adding ``@require_trusted_peer``
    (they're admin-UI-callable already; same trust surface).

  * C4 — ``POST /api/admin/breakers/trip`` + ``/api/admin/breakers/reset``
    (Phase E.3.5). Replaces evo's in-process ``action.{bot,pod}.{trip,
    reset}_breaker`` tools that hit ``permissions.writer.write_openclaw_fields``
    → /tmp + sudo + cp + chmod + launchctl. Both bot-scope and pod-
    scope trips/resets handled by the same endpoints via a ``scope``
    field in the body. Protected by ``@require_trusted_peer``.

C3 is a re-plumb only — no new endpoint, just point ``pod_state(query="bots")``
at the existing ``GET /api/status`` route.

Additional endpoints (post-Phase-E):

  * ``PUT /api/admin/bot/<bot_id>/auto-memory`` — toggles OC's
    ``plugins.slots.memory`` between ``"none"`` (disable auto-memory
    writes + retrieval entirely) and the OC default. Backs the
    ``bot_action(action="set_auto_memory")`` evo tool. Writes via
    ``safe_write_bot_config`` and kicks the gateway so OC reloads.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from ..runtime import get_launchd_scheduler
from .peer_auth import require_trusted_peer

log = logging.getLogger(__name__)


def _load_openclaw_for_bot(bot_id: str, network_path: Path) -> dict | None:
    """Read a bot's openclaw.json from inside the admin daemon.

    Reuses the same direct-read + sudo /bin/cat fallback the tools use
    today, but runs as the admin daemon's user (which has the
    ACL grant via set_evolve_read_acl). Returns None on read failure.
    """
    try:
        from evolve_admin.config import bot_home, load_network
    except ImportError:
        return None

    try:
        network = load_network(network_path)
        home = bot_home(bot_id, network)
        oc_path = home / ".openclaw" / "openclaw.json"
    except Exception:  # noqa: BLE001
        return None

    import json as _json
    try:
        return _json.loads(oc_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        pass
    except _json.JSONDecodeError:
        return None

    import subprocess
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(oc_path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return _json.loads(r.stdout)
    except (subprocess.TimeoutExpired, _json.JSONDecodeError, OSError):
        return None


def register_admin_bot_routes(app: Flask, network_path: Path) -> None:
    """Mount the evo-only admin-bot endpoints on the Flask app.

    All routes here are guarded by ``@require_trusted_peer`` — reachable
    only over the admin-daemon unix socket from the trusted user list.
    """

    @app.get("/api/admin/bot/<bot_id>/openclaw-config")
    @require_trusted_peer(network_path=network_path)
    def admin_bot_openclaw_config(bot_id: str):
        """C1 — return the operator-relevant projection of a bot's openclaw.json.

        Same shape evo's ``config.bot`` MCP tool returned when it read
        the file directly. After re-plumbing, the tool calls this
        endpoint instead and gets the same shape.

        The projection lives in ``evo/tools/config_bot.py`` —
        ``_project_bot_config`` is imported here so the projection logic
        stays in one place. (Long-term refactor: move the projection to
        a neutral location. Out of scope for E.3.1.)

        Secret redaction is part of the projection — this endpoint
        never returns raw token / API-key content. The model on the
        other end of evo's tool call gets the same sentinel strings as
        before ("[redacted: N-char secret]").
        """
        from evolve_admin.evo.tools.config_bot import _project_bot_config

        if not bot_id:
            return jsonify({"error": "bot_id is required"}), 400

        cfg = _load_openclaw_for_bot(bot_id, network_path)
        if cfg is None:
            return jsonify({
                "bot_id": bot_id,
                "available": False,
                "error": (
                    "could not read openclaw.json "
                    "(bot not found or no read access)"
                ),
            }), 404

        return jsonify({
            "bot_id": bot_id,
            "available": True,
            "config": _project_bot_config(cfg),
        })

    @app.get("/api/admin/bot/<bot_id>/backup-status")
    @require_trusted_peer(network_path=network_path)
    def admin_bot_backup_status(bot_id: str):
        """C2 — return backup state for one bot.

        Same shape evo's ``pod_state(query="backup_status")`` returned when it
        ran git/plist reads directly from inside the gateway subprocess.
        The admin daemon runs as the ``evolve`` user (which has the
        cross-bot ACL grants); evo's gateway after Phase E.2.b runs as
        the unprivileged ``evo`` user (no cross-bot ACLs), so the
        in-process path stops working there. Routing through the daemon
        lets the read continue using the existing ACL.

        Reuses ``_backup_status_handler`` from the tool module so the
        projection shape stays in one place. Long-term refactor: move
        the helpers to a neutral location. Out of scope for E.3.2.
        """
        from evolve_admin.evo.tools.pod_state_backup import _backup_status_handler

        if not bot_id:
            return jsonify({"error": "bot_id is required"}), 400

        result = _backup_status_handler(network_path, bot_id)
        # _backup_status_handler doesn't have an "available" gate —
        # missing-bot scenarios surface as backup_configured=False +
        # workspace_exists=False inside the dict. Caller (evo's tool)
        # can paraphrase those flags directly.
        return jsonify(result)

    @app.post("/api/admin/infra/<daemon_id>/restart")
    @require_trusted_peer(network_path=network_path)
    def admin_infra_daemon_restart(daemon_id: str):
        """C5 — kick a pod-wide infra daemon via launchctl kickstart.

        Replaces evo's in-process ``action.infra.daemon_restart`` tool's
        ``sudo /bin/launchctl kickstart`` call. The sudoers grant lives
        on the admin daemon's `evolve` user (not on evo's future `evo`
        user), so the kickstart must happen here.

        Validates daemon_id against the same allowlist the tool used —
        mirrored here so the server enforces the trust boundary
        independently of the tool's input validation.

        Body (optional): ``{"reason": "<operator-supplied rationale>"}``
        — logged to the audit trail but doesn't affect the kickstart.
        """
        from flask import request as _req

        # Allowlist mirror from action_infra.py. Keep in sync — a daemon
        # added there but not here can't be kicked via the new path.
        _allowed = frozenset({
            "evolve.heal", "evolve.verify", "evolve.repo-puller",
            "evolve.admin-ui", "evolve.signal-notifier",
            "evolve.pod-health", "evolve.audit", "evolve.weekly-review",
            "evolve.usage-logger", "evolve.retention",
            "evolve.cost_watchdog", "evolve.session_economics",
            "evolve.embedding_monitor", "evolve.proposal-auto-resolve",
            "evolve.proposal_synthesizer", "evolve.update-watcher",
            "evolve.anthropic-admin-ingest", "evolve.audit-scheduler",
        })

        if daemon_id not in _allowed:
            return jsonify({
                "ok": False,
                "error": f"daemon_id {daemon_id!r} not in allowlist",
            }), 400

        body = _req.get_json(silent=True) or {}
        reason = body.get("reason") or ""

        plist = Path(f"/Library/LaunchDaemons/ai.evolve.{daemon_id}.plist")
        if not plist.exists():
            return jsonify({
                "ok": False,
                "error": (
                    f"plist not installed: {plist} — run "
                    "`evolve-admin install-infra-jobs`?"
                ),
            }), 404

        # raw(): the response embeds the launchctl exit code and stderr as
        # separate JSON fields; Scheduler.restart() collapses them to
        # (ok, combined-text). Timeouts/spawn errors surface as
        # (1, "", str(exc)) per the seam runner's contract.
        rc, _out, err = get_launchd_scheduler().raw(
            "kickstart", "-k", f"system/ai.evolve.{daemon_id}",
        )

        log.info(
            "admin_infra_daemon_restart: kicked %s (rc=%d) reason=%s",
            daemon_id, rc, reason,
        )
        if rc != 0:
            return jsonify({
                "ok": False,
                "error": f"launchctl kickstart returned {rc}",
                "stderr": err.strip()[:500],
            }), 500

        return jsonify({
            "ok": True,
            "daemon_id": daemon_id,
            "reason": reason,
        })

    @app.post("/api/admin/breakers/trip")
    @require_trusted_peer(network_path=network_path)
    def admin_breakers_trip():
        """C4 — trip a breaker (bot-scope or pod-scope).

        Body: ``{scope, breaker_type, duration?, reason?}`` where
        ``scope`` is either a bot_id or the literal ``"pod"``.

        Replaces evo's in-process action.{bot,pod}.trip_breaker tools.
        Writes state via ``breakers.store.trip`` then enforces via
        ``breakers_enforce.enforce_trip`` (which uses /tmp+sudo+cp+
        launchctl admin-daemon-side).

        Returns the same shape the tool used to assemble in-process:
        ``{ok, scope, breaker_type, trip_id, expires_at, reason, enforce}``.
        """
        from flask import request as _req
        body = _req.get_json(silent=True) or {}
        scope = (body.get("scope") or "").strip()
        breaker_type = (body.get("breaker_type") or "").strip()
        duration = body.get("duration") or "24h"
        reason = body.get("reason") or "trip via admin daemon"

        if not scope:
            return jsonify({"ok": False, "error": "scope is required"}), 400
        if not breaker_type:
            return jsonify({
                "ok": False, "error": "breaker_type is required",
            }), 400

        try:
            from breakers import store as _bstore
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"breakers.store unavailable: {exc}"}), 500
        try:
            from evolve_admin import breakers_enforce as _enforce
            from evolve_admin.config import load_network
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"breakers_enforce unavailable: {exc}"}), 500

        try:
            network = load_network(network_path)
            shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
            dur = _bstore.parse_duration(duration)
        except (ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        try:
            record = _bstore.trip(
                shared_dir=shared_dir,
                scope=scope,
                breaker_type=breaker_type,
                duration=dur,
                initiated_by="evo:tool",
                reason=reason,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"trip rejected: {exc}"}), 400

        try:
            er = _enforce.enforce_trip(
                scope=scope, breaker_type=breaker_type, network=network,
                reason=record.reason, expires_at_iso=record.expires_at,
            )
        except ValueError as exc:
            return jsonify({
                "ok": False,
                "error": f"state written but enforce failed: {exc}",
                "trip_id": record.trip_id,
            }), 500

        return jsonify({
            "ok": er.ok,
            "scope": scope,
            "breaker_type": breaker_type,
            "trip_id": record.trip_id,
            "expires_at": record.expires_at,
            "reason": record.reason,
            "enforce": er.to_dict(),
        })

    @app.post("/api/admin/breakers/reset")
    @require_trusted_peer(network_path=network_path)
    def admin_breakers_reset():
        """C4 — reset a breaker (bot-scope or pod-scope).

        Body: ``{scope, breaker_type, reason?}``. Mirrors trip.
        """
        from flask import request as _req
        body = _req.get_json(silent=True) or {}
        scope = (body.get("scope") or "").strip()
        breaker_type = (body.get("breaker_type") or "").strip()
        reason = body.get("reason") or "reset via admin daemon"

        if not scope:
            return jsonify({"ok": False, "error": "scope is required"}), 400
        if not breaker_type:
            return jsonify({
                "ok": False, "error": "breaker_type is required",
            }), 400

        try:
            from breakers import store as _bstore
            from evolve_admin import breakers_enforce as _enforce
            from evolve_admin.config import load_network
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"breakers backend unavailable: {exc}"}), 500

        try:
            network = load_network(network_path)
            shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        try:
            record = _bstore.reset(
                shared_dir=shared_dir,
                scope=scope,
                breaker_type=breaker_type,
                initiated_by="evo:tool",
                reason=reason,
            )
        except (ValueError, KeyError) as exc:
            return jsonify({"ok": False, "error": f"reset rejected: {exc}"}), 400

        try:
            er = _enforce.enforce_reset(
                scope=scope, breaker_type=breaker_type, network=network,
                reason=reason,
            )
        except ValueError as exc:
            return jsonify({
                "ok": False,
                "error": f"state updated but enforce failed: {exc}",
                "reset_id": getattr(record, "reset_id", None),
            }), 500

        return jsonify({
            "ok": er.ok,
            "scope": scope,
            "breaker_type": breaker_type,
            "reason": reason,
            "enforce": er.to_dict(),
        })

    @app.post("/api/admin/applications/coherence-check")
    @require_trusted_peer(network_path=network_path)
    def admin_applications_coherence_check():
        """Run Pass C3 (LLM capability check) for one app, persist the verdict.

        Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.

        Body: ``{bot_id, app_id, trigger}`` where trigger is one of
        ``charter_change | forge_approval | on_demand``.

        Returns the dispatcher's structured result — see
        ``coherence_c3_dispatcher.DispatchResult``. Statuses:

          * ``ok=true``    — LLM ran, verdict cached on the manifest
          * ``ok=false, skipped=true`` — should_run_c3 said no (rate-
                limit, no charter change, etc.); no LLM call billed
          * ``ok=false, skipped=false`` — LLM dispatch failed; ``error``
                explains; ``raw_response`` may carry the LLM's
                unparseable response when relevant

        Cost: ~5k tokens per run (Haiku-ish). The estimate is returned
        in ``cost_estimate_usd`` so the operator surface can show what
        the call cost before / after it ran.
        """
        from flask import request as _req
        from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3
        from evolve_admin.config import load_network

        body = _req.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        app_id = (body.get("app_id") or "").strip()
        trigger = (body.get("trigger") or "").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id is required"}), 400
        if not app_id:
            return jsonify({"ok": False, "error": "app_id is required"}), 400
        if not trigger:
            return jsonify({"ok": False, "error": "trigger is required"}), 400

        try:
            network = load_network(network_path)
            shared_dir = Path(
                network.get("sharedDir", "/Users/Shared/evolve")
            )
        except OSError as exc:
            return jsonify({
                "ok": False,
                "error": f"could not load network config: {exc}",
            }), 500

        result = dispatch_c3(
            bot_id=bot_id, app_id=app_id, trigger=trigger,
            shared_dir=shared_dir, network=network,
        )
        return jsonify(result.to_dict())

    @app.put("/api/admin/bot/<bot_id>/auto-memory")
    @require_trusted_peer(network_path=network_path)
    def admin_bot_set_auto_memory(bot_id: str):
        """Toggle OC's auto-memory writes for one bot.

        OC's flag is ``plugins.slots.memory`` (per oc-config-schema.txt
        line 40562): setting it to ``"none"`` disables the memory plugin
        entirely — no MEMORY.md writes, no embedding-index updates.
        Omitting the key falls back to OC's default ("builtin"). Note
        that ``memorySearch.enabled`` only kills retrieval, not writes —
        the slot flag is the canonical kill-switch.

        Body: ``{enabled: bool, reason?: string}``. ``enabled=False``
        sets the slot to "none"; ``enabled=True`` removes the override
        so OC's default returns. Records the choice via ``config_intent``
        so future drift generators don't re-propose the inverse.

        Returns the same shape the evo tool returns to the model:
        ``{ok, bot_id, enabled, prior_value, new_value, status,
        verify_via}``. ``status`` is one of ``set | already`` (no-op
        because the slot already matches the request).
        """
        from flask import request as _req
        body = _req.get_json(silent=True) or {}
        if "enabled" not in body or not isinstance(body["enabled"], bool):
            return jsonify({
                "ok": False,
                "error": "body.enabled (bool) is required",
            }), 400
        enabled = bool(body["enabled"])
        reason = (body.get("reason") or "").strip()

        cfg = _load_openclaw_for_bot(bot_id, network_path)
        if cfg is None:
            return jsonify({
                "ok": False,
                "error": (
                    f"could not read openclaw.json for bot '{bot_id}' "
                    "(bot not found or no read access)"
                ),
            }), 404

        plugins_block = cfg.get("plugins")
        if not isinstance(plugins_block, dict):
            plugins_block = {}
        slots_block = plugins_block.get("slots")
        if not isinstance(slots_block, dict):
            slots_block = {}

        prior_value = slots_block.get("memory")  # may be None / str
        # Map prior_value → caller-facing "currently enabled" semantics:
        # "none" is the only explicit disable. Anything else (incl. None
        # for "unset") means the default memory plugin is active.
        currently_disabled = prior_value == "none"
        target_value = "none" if not enabled else None

        already = (
            (enabled and not currently_disabled)
            or (not enabled and currently_disabled)
        )
        if already:
            return jsonify({
                "ok": True,
                "bot_id": bot_id,
                "enabled": enabled,
                "prior_value": prior_value,
                "new_value": prior_value,
                "status": "already",
                "reason": reason or None,
            })

        # Mutate plugins.slots.memory and write back via safe_write_bot_config.
        import json as _json
        new_cfg = _json.loads(_json.dumps(cfg))  # cheap deepcopy
        new_plugins = new_cfg.setdefault("plugins", {})
        new_slots = new_plugins.setdefault("slots", {})
        if target_value is None:
            new_slots.pop("memory", None)
        else:
            new_slots["memory"] = target_value

        try:
            from evolve_admin.deploy import (
                restart_gateway,
                safe_write_bot_config,
            )
            from evolve_admin.config import load_network, bot_home
        except ImportError as exc:
            return jsonify({
                "ok": False,
                "error": f"deploy module unavailable: {exc}",
            }), 500

        try:
            network = load_network(network_path)
        except OSError as exc:
            return jsonify({
                "ok": False,
                "error": f"could not load network.json: {exc}",
            }), 500

        bots = network.get("bots") or {}
        bot_cfg = bots.get(bot_id) or {}
        bot_user = (
            bot_cfg.get("user") if isinstance(bot_cfg, dict) else None
        ) or bot_id

        write_reason = (
            f'bot_action(action="set_auto_memory", enabled={enabled})'
            + (f" — {reason}" if reason else "")
        )
        ok, err = safe_write_bot_config(
            bot_id, new_cfg, reason=write_reason, bot_user=bot_user,
        )
        if not ok:
            return jsonify({
                "ok": False,
                "error": f"safe_write_bot_config failed: {err}",
                "bot_id": bot_id,
                "prior_value": prior_value,
            }), 500

        # Kickstart so OC reloads the slot config.
        kickstart_warning: str | None = None
        try:
            restart_gateway(bot_id, bot_user=bot_user)
        except Exception as exc:  # noqa: BLE001
            # The write landed; surface the kickstart failure as a soft
            # warning rather than rolling back — operator can restart
            # manually, the new config is durable.
            kickstart_warning = (
                f"gateway restart failed: {type(exc).__name__}: {exc}"
            )

        # Record intent so drift generators see this as deliberate.
        try:
            from evolve_admin.config_intent import set_intent
            set_intent(
                bot_id,
                "plugins.slots.memory",
                target_value if target_value is not None else "",
                reason=reason or (
                    f"operator {'disabled' if not enabled else 'enabled'} "
                    "auto-memory via evo tool"
                ),
                set_by='bot_action(action="set_auto_memory")',
                set_by_detail=(
                    "Toggles plugins.slots.memory between 'none' (disable) "
                    "and the OC default (enable)."
                ),
                actor="evo",
            )
        except Exception:  # noqa: BLE001 — hygiene metadata, never blocking
            log.exception(
                "admin_bot_set_auto_memory: set_intent failed for %s", bot_id,
            )

        resp = {
            "ok": True,
            "bot_id": bot_id,
            "enabled": enabled,
            "prior_value": prior_value,
            "new_value": target_value,
            "status": "set",
            "reason": reason or None,
        }
        if kickstart_warning:
            resp["kickstart_warning"] = kickstart_warning
        return jsonify(resp)

    @app.get("/api/admin/_peer-identity")
    @require_trusted_peer(network_path=network_path)
    def admin_peer_identity():
        """Smoke-test endpoint: return the calling peer's identity.

        Used by tests and by ``evolve-admin admin-daemon-ping`` (a CLI
        helper added later) to verify the unix-socket auth path is
        wired correctly without needing a per-bot fixture.

        Reachable only over the unix socket from the trusted user list,
        same as every other route registered here.
        """
        from flask import request
        return jsonify({
            "peer_uid": int(request.environ.get("REMOTE_PEER_UID", -1)),
            "peer_gid": int(request.environ.get("REMOTE_PEER_GID", -1)),
            "transport": request.environ.get("REMOTE_TRANSPORT", "tcp"),
        })

    log.info(
        "admin_bot_routes: registered C1 + C2 + C4(breakers trip/reset) + "
        "C5(infra-restart) + applications/coherence-check + "
        "bot/<id>/auto-memory + _peer-identity"
    )
