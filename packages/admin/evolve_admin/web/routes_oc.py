"""HTTP routes for the OC (OpenClaw CLI) and Security surface.

/api/oc/*              fleet-wide OC CLI aggregation (status, models, cron, channels, audit, …)
/api/security/*        security audit, backup, drift, quarantine, identity, config health
/api/backup/*          cloud and local backup status, config, keys, data classification
/api/integrations/*    channel + API-key integration health probes
/api/signals/refresh   re-run live monitor sweep (integration_probe, host_health, …)
/api/config/health     per-bot openclaw.json health checks

Extracted from server.py (Batch C, Step 1).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, Response
from .http_errors import log_request_error

from ..runtime import get_scheduler
from platform_profile import get_profile

# ── Analyzer import helper (canonical version lives in server.py) ───────────
# _import_analyzer and ensure_workspace_git_init are resolved through the
# server module object at call time (inside register_oc_routes) so that
# monkeypatch.setattr("evolve_admin.web.server.<X>", ...) is honored in tests.
# _ANALYZER_DIR is kept as a filesystem path (backup.py lives there); the
# modules themselves come from the installed evolve-analyzer package.
_ANALYZER_DIR = Path(__file__).parent.parent.parent.parent / "analyzer"


from evolve_util import now_iso as _now_iso

from ..config import load_network, save_network, get_bot_user
from ..telemetry import get_logger
from .. import audit_state as _audit_state
# _github_api was lifted out of register_admin_routes's closure into
# routes_admin_shared.py (4.1b Increment 0), so it is now importable here.
# Cycle-safe: routes_admin_shared imports only stdlib + ..config + a few
# never-patched server names, and _github_api itself pulls in no server name.
from .routes_admin_shared import _github_api  # noqa: E402

_log = get_logger("web.routes_oc")

# ── Mutable state shared with other server.py functions via _audit_state ────
# _audit_cache is the same dict object as _audit_state._state (as in server.py)
_audit_cache: dict = _audit_state._state
# Background audit job state — owned by this module (only used in register_oc_routes)
_audit_job: dict = {"running": False, "bots": [], "done": 0, "total": 0, "started_at": 0.0}
_audit_job_lock = threading.Lock()

# ── Helper functions (verbatim copies from server.py) ───────────────────────

# _JUDGE_PRICE_PER_MTOK_USD / _estimate_judge_cost_usd /
# _summarize_test_telemetry were removed 2026-06-08 — app-test surface
# killed per internal/decision-app-tests-2026-06-08.md.


def _glob_manifests(dir_path: Path) -> list[str]:
    """Return manifest paths in ``dir_path`` — filters hidden + history files."""
    return [
        str(f) for f in dir_path.glob("*.json")
        if not f.name.startswith(".") and "_history" not in f.name
    ]


def _bot_manifests_dir(bot_id: str, user: str | None = None) -> Path:
    """Resolve manifests dir via resolve_bot_paths so non-standard home dirs work."""
    try:
        paths = resolve_bot_paths(bot_id, user=user)
        return Path(paths["workspace"]) / "manifests"
    except Exception:
        actual = user or bot_id
        try:
            import pwd as _pwd
            home = _pwd.getpwnam(actual).pw_dir
        except Exception:
            home = f"/Users/{actual}"
        return Path(home) / ".openclaw" / "workspace" / "manifests"


def resolve_bot_paths(bot_id: str, user: str | None = None) -> dict:
    """Resolve all important file paths for a bot by reading its own openclaw.json.

    bot_id  — the logical bot name (key in network.json)
    user    — the actual system username for sudo/pwd lookups.
              If omitted, falls back to bot_id (works when they match).

    Falls back to standard paths if config read fails. Never constructs paths
    from assumptions — always reads from the bot's actual config.

    Returns dict with keys:
      oc_config, workspace, agent_dir, auth_profiles, turns_dir,
      turns_dir_fallback, logs_dir, user
    """
    actual_user = user or bot_id
    try:
        import pwd as _pwd
        bot_home = _pwd.getpwnam(actual_user).pw_dir
    except KeyError:
        bot_home = f"/Users/{actual_user}"

    oc_config_path = f"{bot_home}/.openclaw/openclaw.json"
    workspace = bot_home + "/.openclaw/workspace"
    agent_dir = bot_home + "/.openclaw/agents/main/agent"

    # Read openclaw.json to get configured paths (direct first, then sudo /bin/cat as root)
    oc_text: str | None = None
    try:
        oc_text = Path(oc_config_path).read_text()
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", oc_config_path],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                oc_text = r.stdout
        except Exception:
            pass
    except OSError:
        pass

    if oc_text:
        try:
            data = json.loads(oc_text)
            ws = data.get("agents", {}).get("defaults", {}).get("workspace")
            if ws:
                workspace = ws
            agents_list = data.get("agents", {}).get("list", [])
            main_agent = next((a for a in agents_list if a.get("id") == "main"), None)
            if main_agent and main_agent.get("agentDir"):
                agent_dir = main_agent["agentDir"]
        except Exception:
            pass

    # Build the list of candidate turns dirs in priority order.
    # We search multiple locations because deployments vary:
    #   - New-style: /Users/Shared/evolve/{bot}/turns/  (world-readable, preferred)
    #   - Workspace memory: {workspace}/memory/           (bot-owned, sudo needed)
    #   - Legacy workspace memory under home: {home}/.openclaw/workspace/memory/
    # Using a list lets callers try all of them without hardcoding assumptions.
    turns_dir_candidates: list[str] = []
    shared_turns = f"/Users/Shared/evolve/{bot_id}/turns"
    turns_dir_candidates.append(shared_turns)
    workspace_memory = workspace + "/memory"
    turns_dir_candidates.append(workspace_memory)
    # If workspace was read from config and differs from the home-derived default,
    # also add the home-derived path as an extra fallback.
    home_derived_memory = bot_home + "/.openclaw/workspace/memory"
    if home_derived_memory not in turns_dir_candidates:
        turns_dir_candidates.append(home_derived_memory)

    return {
        "oc_config": oc_config_path,
        "workspace": workspace,
        "agent_dir": agent_dir,
        "auth_profiles": agent_dir + "/auth-profiles.json",
        "turns_dir": shared_turns,               # primary (new-style shared dir)
        "turns_dir_fallback": workspace_memory,  # secondary (bot workspace)
        "turns_dir_candidates": turns_dir_candidates,  # all candidates for robust search
        "logs_dir": bot_home + "/.openclaw/logs",
        "user": actual_user,
    }


# _list_manifests_as_bot lives canonically in server.py. The routes below call
# it via ``_module._list_manifests_as_bot`` (see register_oc_routes) so test
# patches on ``evolve_admin.web.server._list_manifests_as_bot`` are honored —
# a local copy here would shadow those patches (it did, breaking backup_data).


def register_oc_routes(app: Flask, network_path: Path) -> None:
    """
    Register /api/oc/* routes — fleet-wide OC CLI aggregation.

    Each endpoint runs oc_cli calls for all bots in parallel (max 4 workers).
    Individual bot failures return null for that bot rather than a 500.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeoutError, as_completed

    # Resolve patchable helpers through the server module so that
    # monkeypatch.setattr("evolve_admin.web.server.<X>", ...) is honored in
    # tests.  The module is fully loaded by the time any route fires, so
    # _module is stable.  Mirror the same pattern as routes_admin.py.
    import sys as _sys_for_shims
    _module = _sys_for_shims.modules["evolve_admin.web.server"]

    def _all_bots() -> list[str]:
        return list(load_network(network_path).get("bots", {}).keys())

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    def _run_parallel(fn, bots: list[str], timeout: int = 20) -> dict:
        results: dict = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(fn, bid): bid for bid in bots}
            for fut in as_completed(futures, timeout=timeout + 5):
                bid = futures[fut]
                try:
                    results[bid] = fut.result(timeout=timeout)
                except Exception:
                    results[bid] = None
        # Ensure every bot has an entry (may be missing if timeout hit)
        for bid in bots:
            if bid not in results:
                results[bid] = None
        return results

    @app.get("/api/oc/status")
    def api_oc_status() -> Response:
        """Fleet OC status — {bot_id: openclaw status --json result}"""
        from runtime.agent_runtime import get_runtime
        return jsonify(_run_parallel(get_runtime().status, _all_bots()))

    @app.get("/api/oc/models")
    def api_oc_models() -> Response:
        """Fleet OC models — {bot_id: openclaw models list --json result}"""
        from runtime.agent_runtime import get_runtime
        return jsonify(_run_parallel(get_runtime().models, _all_bots()))

    @app.get("/api/oc/cron")
    def api_oc_cron() -> Response:
        """Fleet OC cron jobs — enriched with run history and status.
        Returns {bot_id: {jobs: [{label, schedule, last_run, last_exit_code, next_run, status}], error: null}}
        Status values: ok | warning | error | never_run
        """
        import datetime
        import fnmatch
        from runtime.agent_runtime import get_runtime
        _runtime = get_runtime()

        net = load_network(network_path)
        alerts_cfg = net.get("alerts", {})
        silence_days = float(alerts_cfg.get("cronSilenceThresholdDays", 2))
        watched_patterns = alerts_cfg.get("watchedCrons", [])
        now = datetime.datetime.now(datetime.timezone.utc)
        silence_td = datetime.timedelta(days=silence_days)

        def _classify_cli_error(exc_str: str) -> str:
            s = exc_str.lower()
            if "no such file" in s or "not found" in s or "command not found" in s:
                return "Cannot find OC CLI in PATH — install openclaw or check your PATH"
            if "permission" in s or "access denied" in s or "eperm" in s:
                return "Permissions error — check that admin user can run OC CLI commands"
            if "timeout" in s:
                return "OC CLI timed out — gateway may be unresponsive"
            if "gateway closed" in s or "abnormal closure" in s:
                return f"Gateway closed unexpectedly — the bot gateway may have crashed or be restarting. {exc_str}"
            if "connection refused" in s or "connect" in s:
                return "Connection refused — gateway not running on expected port"
            return exc_str

        def _enrich(bot_id: str) -> dict:
            from ..applications.cron_manager import read_jobs_state
            err_out: list = []
            jobs_raw = _runtime.cron_list(bot_id, _err_out=err_out)
            if jobs_raw is None:
                detail = err_out[0] if err_out else "openclaw not found or returned no output"
                classified = _classify_cli_error(detail)
                s = detail.lower()
                err_type = "gateway_error" if ("gateway" in s or "abnormal closure" in s) else "cli_unavailable"
                return {"jobs": [], "error": classified, "error_type": err_type}
            if not isinstance(jobs_raw, list):
                if isinstance(jobs_raw, dict):
                    if isinstance(jobs_raw.get("jobs"), list):
                        jobs_raw = jobs_raw["jobs"]
                    elif "error" in jobs_raw:
                        return {"jobs": [], "error": str(jobs_raw["error"]), "error_type": "format_error"}
                    else:
                        return {"jobs": [], "error": "Unexpected response from cron list (got dict)",
                                "error_type": "format_error"}
                else:
                    return {"jobs": [], "error": f"Unexpected response from cron list (got {type(jobs_raw).__name__})",
                            "error_type": "format_error"}

            # Run history comes from jobs-state.json on disk (keyed by job UUID).
            # openclaw cron runs requires --id and has no --json flag, so CLI
            # calls are not usable for fleet-wide enrichment.
            jobs_state = read_jobs_state(bot_id)

            jobs = []
            for job in (jobs_raw or []):
                jid = job.get("id") or job.get("job_id") or job.get("name") or ""
                label = job.get("label") or job.get("name") or jid
                schedule = job.get("schedule") or job.get("cron") or ""

                job_state = (jobs_state.get(jid) or {}).get("state") or {}
                last_run_ms = job_state.get("lastRunAtMs")
                last_run_at: str | None = None
                if last_run_ms:
                    last_run_at = datetime.datetime.fromtimestamp(
                        last_run_ms / 1000, tz=datetime.timezone.utc
                    ).isoformat()

                run_status = job_state.get("lastRunStatus")  # "ok" | "error" | …
                last_exit_code = 0 if run_status == "ok" else (1 if run_status else None)

                next_run_ms = job_state.get("nextRunAtMs")
                next_run: str | None = None
                if next_run_ms:
                    next_run = datetime.datetime.fromtimestamp(
                        next_run_ms / 1000, tz=datetime.timezone.utc
                    ).isoformat()
                else:
                    next_run = job.get("next_run") or job.get("nextRun") or None

                # Status logic
                if last_run_at is None:
                    status = "never_run"
                elif last_exit_code is not None and last_exit_code != 0:
                    status = "error"
                else:
                    status = "ok"
                    is_watched = any(fnmatch.fnmatch(label, p) for p in watched_patterns)
                    if is_watched and last_run_at:
                        try:
                            last_dt = datetime.datetime.fromisoformat(
                                last_run_at.replace("Z", "+00:00")
                            )
                            if now - last_dt > silence_td:
                                status = "warning"
                        except Exception:
                            pass

                jobs.append({
                    "label": label,
                    "schedule": schedule,
                    "last_run": last_run_at,
                    "last_exit_code": last_exit_code,
                    "next_run": next_run,
                    "status": status,
                })

            return {"jobs": jobs, "error": None}

        bots = _all_bots()
        results: dict = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_enrich, bid): bid for bid in bots}
            for fut in as_completed(futs, timeout=25):
                bid = futs[fut]
                try:
                    results[bid] = fut.result(timeout=20)
                except Exception as e:
                    results[bid] = {"jobs": [], "error": _classify_cli_error(str(e)), "error_type": "exception"}
        for bid in bots:
            if bid not in results:
                results[bid] = {"jobs": [], "error": "OC CLI timed out", "error_type": "timeout"}
        return jsonify(results)

    @app.get("/api/oc/cron/runs")
    def api_oc_cron_runs() -> Response:
        """Fleet OC cron runs — {bot_id: openclaw cron runs --json result}.

        Requires ``?job_id=`` because openclaw's ``cron runs --id`` flag is
        a Commander ``requiredOption`` — fan-out without a job_id used to
        silently return None per bot (every subprocess failed with
        ``error: required option '--id <job_id>'`` for 49 log lines
        between 2026-05-04 → 2026-05-13 before the UI caller stopped
        hitting this endpoint). See diagnosis-spend-alert-openclaw-flag-2026-05-21.md.
        """
        from runtime.agent_runtime import get_runtime
        _runtime = get_runtime()
        job_id = (request.args.get("job_id") or "").strip()
        if not job_id:
            return jsonify({"error": "job_id query param required"}), 400
        return jsonify(_run_parallel(lambda bid: _runtime.cron_runs(bid, job_id), _all_bots()))

    @app.get("/api/oc/channels")
    def api_oc_channels() -> Response:
        """Fleet OC channels — {bot_id: openclaw channels list --json result}"""
        from runtime.agent_runtime import get_runtime
        return jsonify(_run_parallel(get_runtime().channels, _all_bots()))

    # ── Audit helpers (module-accessible so background thread can call them) ──

    def _audit_run_one(bot_id: str, net: dict) -> dict:
        import datetime as _dt
        from runtime.agent_runtime import get_runtime
        _runtime = get_runtime()

        _CAT_MAP = {
            "gateway": "channel", "tools": "exec", "fs": "config",
            "models": "config", "security": "auth", "session": "session",
            "auth": "auth", "summary": "config",
        }
        _CONFIG_INVALID_MARKERS = (
            "Config invalid", "Unrecognized key", "Unrecognized keys",
            "Legacy config keys", "Legacy config key",
        )

        err_out: list = []
        raw = _runtime.security_audit(bot_id, _err_out=err_out)
        if not isinstance(raw, dict):
            err_text = err_out[0] if err_out else None
            if err_text and any(m in err_text for m in _CONFIG_INVALID_MARKERS):
                _runtime.doctor_fix(bot_id)
                retry_err: list = []
                raw = _runtime.security_audit(bot_id, _err_out=retry_err)
                if not isinstance(raw, dict):
                    return {"unavailable": True, "error_type": "config_invalid", "error": err_text}
            else:
                return {"unavailable": True, "error": err_text}

        summary = raw.get("summary", {})
        crit = summary.get("critical", 0)
        warn = summary.get("warn", 0)
        info = summary.get("info", 0)

        gateway_bind = "127.0.0.1"
        primary_model = ""
        try:
            oc_json = _module._bot_home(bot_id, net) / ".openclaw" / "openclaw.json"
            oc_cfg = json.loads(oc_json.read_text())
            gateway_bind = oc_cfg.get("gateway", {}).get("bind", "127.0.0.1")
            primary_model = (
                oc_cfg.get("agents", {}).get("defaults", {})
                      .get("model", {}).get("primary", "") or ""
            )
        except Exception:
            pass
        routing_enabled = bool(
            net.get("models", {}).get("routing", {}).get("enabled", False)
        )

        # Normalize each raw OC finding through the analyzer's single-source
        # helper (analyzer/audit.py::normalize_oc_finding). It owns every
        # operator-facing drop/demote rule — the Linux ACL-mask false-positive
        # drop, the member-bot "full" exec drop, and the multi-user / proxy-
        # header / below-recommended prose demotions — so this Security-page
        # forwarder and the evo tray forwarder can't drift (the very hazard
        # that caused the mask-FP bug; see
        # memory:feedback_three_oc_audit_forwarders_must_share_suppression).
        # We pass this surface's config context (gateway_bind, routing_enabled,
        # primary_model) so the two context-dependent demotions apply here; the
        # tray passes none and they no-op there. No-op on macOS for the mask
        # drop (the Perms seam returns False); genuine exposures (a real
        # other::r world-readable, the creds-dir #3213 critical) are excluded
        # from the mask-prone set and still fire. Score-counter bookkeeping
        # stays local: a drop decrements the bucket the advisory came from and a
        # demote moves one warn→info, matching the pre-single-source behavior.
        try:
            _normalize = _module._import_analyzer("audit").normalize_oc_finding
        except Exception:
            _normalize = None

        findings = []
        for f in raw.get("findings", []):
            check_id = f.get("checkId", "")
            cat_key = check_id.split(".")[0] if check_id else "config"

            if _normalize is None:
                # Fail-open: if the analyzer helper can't be imported, surface
                # the finding un-normalized (warn→warning) rather than hide it.
                sev = "warning" if f.get("severity") == "warn" else f.get("severity", "info")
                findings.append({
                    "severity": sev,
                    "category": _CAT_MAP.get(cat_key, "config"),
                    "message": f.get("title", ""),
                    "recommendation": f.get("remediation", ""),
                })
                continue

            decision = _normalize(
                f,
                gateway_bind=gateway_bind,
                routing_enabled=routing_enabled,
                primary_model=primary_model,
            )
            if decision.drop:
                # Decrement the bucket the advisory came from so the score and
                # badge counts reflect only real findings.
                if decision.raw_severity == "critical":
                    crit = max(0, crit - 1)
                elif decision.raw_severity == "warning":
                    warn = max(0, warn - 1)
                continue

            if decision.demoted:
                # A warning/critical demoted to info — move the count.
                warn = max(0, warn - 1)
                info += 1

            findings.append({
                "severity": decision.severity,
                "category": _CAT_MAP.get(cat_key, "config"),
                "message": f.get("title", ""),
                "recommendation": f.get("remediation", ""),
            })

        ts_ms = raw.get("ts", 0)
        generated_at = (
            _dt.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts_ms else _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        # v24 (manifest-v7 Slice 2): per-app data-boundary projection —
        # "what this app collects, who can reach it" from the structured
        # privacy{} / audience_scoping{} blocks. This is U4.3's data
        # source; structured fields the Security page renders, NOT
        # plain-language bullets (that pattern was retired —
        # internal/roadmap-user-value-2026-06-10.md U4.3). Best-effort: a
        # manifest-read failure must not break the security audit.
        app_data_boundaries: list[dict] = []
        try:
            from ..applications.manifest import list_manifests
            from ..applications.privacy_scoping_validator import (
                project_data_boundary,
            )
            for m in list_manifests(_audit_shared_dir(net), bot_id):
                app_data_boundaries.append(
                    project_data_boundary(m.to_dict())
                )
            app_data_boundaries.sort(key=lambda b: b.get("app_id") or "")
        except Exception:
            app_data_boundaries = []

        return {
            "score": max(0, 100 - crit * 20 - warn * 5),
            "findings": findings,
            "warned": warn,
            "critical": crit,
            "info": info,
            "generated_at": generated_at,
            "run_at": generated_at,
            "app_data_boundaries": app_data_boundaries,
        }

    def _audit_shared_dir(net: dict) -> Path:
        """Resolve the shared dir for audit-cache disk persistence.
        Mirrors the resolution used by every other shared-dir reader
        on this module."""
        return Path(net.get("sharedDir", "/Users/Shared/evolve"))

    def _run_audit_background(bots: list, net: dict) -> None:
        """Background thread: audit each bot, write incremental results to cache."""
        partial: dict = dict(_audit_cache.get("data") or {})
        sd = _audit_shared_dir(net)

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_audit_run_one, bid, net): bid for bid in bots}
            for fut in as_completed(futs):
                bid = futs[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    result = {"unavailable": True, "error": str(exc) or None}
                partial[bid] = result
                _audit_cache["data"] = dict(partial)
                # Mirror the incremental write to disk so cross-process
                # readers (MCP server's pod_state.audit) see fresh data
                # as the sweep progresses, not only at sweep end.
                _audit_state.persist(sd)
                with _audit_job_lock:
                    _audit_job["done"] = _audit_job.get("done", 0) + 1

        _audit_cache["cached_at"] = _time.time()
        # Final persist captures the cached_at bump (per-bot persists
        # above already wrote the data side).
        _audit_state.persist(sd)
        with _audit_job_lock:
            _audit_job["running"] = False

    def _run_single_bot_background(bot_id: str, net: dict) -> None:
        """Background thread for single-bot re-run; patches only that bot's cache entry."""
        try:
            result = _audit_run_one(bot_id, net)
        except Exception as exc:
            result = {"unavailable": True, "error": str(exc) or None}
        data = dict(_audit_cache.get("data") or {})
        data[bot_id] = result
        _audit_cache["data"] = data
        _audit_cache["cached_at"] = _time.time()
        _audit_state.persist(_audit_shared_dir(net))
        with _audit_job_lock:
            _audit_job["running"] = False

    @app.get("/api/security/audit")
    def api_security_audit() -> Response:
        """Returns cached audit results immediately. Trigger a fresh run via POST /refresh."""
        data = _audit_cache.get("data") or {}
        cached_at = _audit_cache.get("cached_at")
        with _audit_job_lock:
            running = _audit_job.get("running", False)
        return jsonify({"data": data, "cached_at": cached_at, "running": running})

    @app.post("/api/security/audit/refresh")
    def api_security_audit_refresh() -> Response:
        """Kick off a background audit. ?bot=<id> to re-run a single bot. Returns 202."""
        bot_id = request.args.get("bot", "").strip() or None
        with _audit_job_lock:
            if _audit_job.get("running"):
                return jsonify({"status": "already_running",
                                "done": _audit_job["done"],
                                "total": _audit_job["total"]}), 200
            net = load_network(network_path)
            if bot_id:
                bots = [bot_id] if bot_id in _all_bots() else []
                if not bots:
                    return jsonify({"error": f"unknown bot: {bot_id}"}), 404
                _audit_job.update({"running": True, "bots": bots,
                                   "done": 0, "total": 1,
                                   "started_at": _time.time()})
                t = threading.Thread(target=_run_single_bot_background,
                                     args=(bot_id, net), daemon=True)
            else:
                bots = _all_bots()
                _audit_job.update({"running": True, "bots": bots,
                                   "done": 0, "total": len(bots),
                                   "started_at": _time.time()})
                t = threading.Thread(target=_run_audit_background,
                                     args=(bots, net), daemon=True)

        t.start()
        return jsonify({"status": "started", "total": len(bots)}), 202

    @app.get("/api/security/audit/refresh/status")
    def api_security_audit_refresh_status() -> Response:
        """Returns background job progress."""
        with _audit_job_lock:
            snap = dict(_audit_job)
        cached_at = _audit_cache.get("cached_at")
        return jsonify({**snap, "cached_at": cached_at})

    # ── Security: persistent mute ───────────────────────────────────────

    def _muted_path() -> Path:
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        return shared / "security" / "muted.json"

    def _load_muted() -> dict:
        p = _muted_path()
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            return {}

    def _save_muted(data: dict) -> None:
        p = _muted_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            import shutil as _sh
            _sh.copy2(tmp, p)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @app.get("/api/security/muted")
    def api_security_muted_get() -> Response:
        """Return muted advisory keys as {bot_id: [msg, ...]}."""
        return jsonify(_load_muted())

    @app.post("/api/security/muted")
    def api_security_muted_post() -> Response:
        """Body: {bot: str, message: str, action: 'mute'|'unmute'|'clear'}."""
        body = request.get_json(silent=True) or {}
        data = _load_muted()
        action = body.get("action", "mute")
        bot = body.get("bot", "")
        msg = body.get("message", "")
        if action == "clear":
            data.pop(bot, None) if bot else data.clear()
        elif action == "unmute" and bot and msg:
            lst = data.get(bot, [])
            data[bot] = [m for m in lst if m != msg]
        elif action == "mute" and bot and msg:
            lst = data.get(bot, [])
            if msg not in lst:
                lst.append(msg)
            data[bot] = lst
        else:
            return jsonify({"error": "invalid action or missing fields"}), 400
        _save_muted(data)
        return jsonify({"ok": True})

    # Phase 4f endpoint consolidation: /api/security/backup-* and
    # /api/maintenance/backup-now are dual-registered under /api/backup/cloud/*
    # so external clients (curl scripts, dashboards, evo context packs) keep
    # working while the frontend uses the new namespace. The old paths can be
    # retired in a future cleanup once nothing in the codebase references them.
    # fd-burst guard (2026-07-28 incident): each request to the backup
    # status endpoint fans out ~3 git subprocesses per bot (freshness log
    # + two drift-detection reads), each with a 5s timeout, serially —
    # a slow fleet scan can hold a werkzeug thread for 15s+. With
    # werkzeug's unbounded thread-per-request, every concurrent poller
    # (multiple tabs, retrying UI) held a socket AND spawned its own full
    # git fan-out, stacking transient subprocess fds toward launchd's
    # 256 soft NOFILE cap. Single-flight lock + short TTL cache collapses
    # concurrent/rapid polls into one scan; 10s staleness is invisible
    # next to the >26h staleness the endpoint reports on.
    _backup_status_lock = threading.Lock()
    _backup_status_cache: dict = {"at": 0.0, "payload": None}
    _BACKUP_STATUS_TTL_SECONDS = 10.0

    @app.get("/api/backup/cloud/status")
    @app.get("/api/security/backup-status", endpoint="api_security_backup_status_alias")
    def api_security_backup_status() -> Response:
        """Return git backup freshness per bot.

        Reads last backup commit timestamp from each bot's own workspace repo
        (looks for commits with '[backup]' in the message).
        Returns {bot_id: {last_backup: ISO|null, hours_ago: float|null, stale: bool}}
        """
        # Single-flight: concurrent pollers queue on the lock; whoever
        # enters after a fresh compute gets the cached payload instead of
        # re-running the per-bot git fan-out (fd-burst guard, see above).
        with _backup_status_lock:
            age = _time.monotonic() - _backup_status_cache["at"]
            if (
                _backup_status_cache["payload"] is not None
                and age < _BACKUP_STATUS_TTL_SECONDS
            ):
                return jsonify(_backup_status_cache["payload"])
            payload = _compute_backup_status()
            _backup_status_cache["payload"] = payload
            _backup_status_cache["at"] = _time.monotonic()
            return jsonify(payload)

    def _compute_backup_status() -> dict:
        import subprocess as _sp, datetime as _dt
        net = load_network(network_path)
        bots_cfg = net.get("bots", {})
        bots = _all_bots()
        result: dict = {}

        # Per-bot run-state files written by backup.py after each attempt.
        # Each bot's daemon (running as the bot user) writes its own
        # state into its own workspace; admin UI reads via the existing
        # `set_evolve_read_acl` grant. Folded into each bot's status
        # entry so the UI can render "✗ push failed Xh ago" with the
        # actual error string — distinct from "✓ last commit Xh ago"
        # which only knows about successes.

        def _read_run_state(_bot_id: str) -> dict:
            try:
                workspace = _module._bot_home(_bot_id, net) / ".openclaw" / "workspace"
                return json.loads((workspace / "evolve-backup" / "state.json").read_text())
            except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
                return {}

        # Lazy-load the classifier so the import cost only hits this
        # endpoint. Module-level import via _import_analyzer is cached.
        try:
            _bd = _module._import_analyzer("backup_diagnostics")
        except Exception:
            _bd = None

        def _graft_run_state(entry: dict, rs: dict) -> dict:
            """Splice run-state fields onto a backup-status entry.

            ``classified_cause`` is derived from ``last_error`` via the
            backup_diagnostics classifier — same {cause_id, title,
            fix_steps} shape the backup_signal monitor uses. The admin
            UI's expandable diagnostic panel (Backup → Status and
            Maintenance → Bot Versions) consumes this field directly.
            None when last_error is empty or unrecognized.
            """
            if rs:
                last_error = rs.get("last_error")
                entry["last_attempt_at"] = rs.get("last_attempt_at")
                entry["last_attempt_status"] = rs.get("last_attempt_status")
                entry["last_success_at"] = rs.get("last_success_at")
                entry["last_error"] = last_error
                entry["consecutive_failures"] = int(rs.get("consecutive_failures") or 0)
                entry["classified_cause"] = (
                    _bd.classify(last_error) if (_bd and last_error) else None
                )
            return entry

        def _parse_iso_utc(ts: "str | None") -> "_dt.datetime | None":
            """Parse an ISO-8601 timestamp into an aware UTC datetime.

            Returns None on missing/unparseable input. Naive timestamps
            (shouldn't happen — git %aI carries an offset, state.json
            uses Z-suffixed UTC) are assumed UTC rather than raising.
            """
            if not ts:
                return None
            try:
                # Z-suffix normalization: fromisoformat only accepts "Z"
                # from 3.11; the repo floor is 3.10 (same pattern as
                # safety_summary / tool_suspend).
                parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed

        for bot_id in bots:
            workspace = _module._bot_home(bot_id, net) / ".openclaw" / "workspace"
            bot_cfg = bots_cfg.get(bot_id, {}) if isinstance(bots_cfg, dict) else {}
            backup_configured = bool(bot_cfg.get("backupRepoUrl"))
            run_state = _read_run_state(bot_id)
            # state.json::last_success_at is the primary freshness source:
            # it advances on BOTH "ok" (commit pushed) and "no-changes"
            # (ran, nothing to save) attempts, so a healthy daemon never
            # reads as stale just because the workspace was quiet. The git
            # commit timestamp below is the fallback for bots whose
            # state.json is missing/unreadable (pre-run-state era, ACL gap).
            last_success = _parse_iso_utc(run_state.get("last_success_at"))
            try:
                r = _sp.run(
                    # -c safe.directory=* lets the evolve user read git repos owned
                    # by bot users — git 2.35.2+ rejects cross-owner repos otherwise.
                    #
                    # No path filter on this log: the ^\[backup\] grep alone
                    # identifies backup commits. The old `-- evolve-backup/`
                    # limiter looked belt-and-suspenders but was load-bearing
                    # wrong — when any other committer in the workspace swept
                    # up the state.json diff first (the 2026-07-28 incident:
                    # a bot-authored nightly job committing seconds
                    # after ours), our [backup] commits stopped touching that
                    # dir and a healthy bot rendered stale for 10 days.
                    ["git", "-c", "safe.directory=*", "-C", str(workspace),
                     "log", "--format=%aI", "-1",
                     "--grep", r"^\[backup\]"],
                    capture_output=True, text=True, timeout=5,
                )
                ts_str = r.stdout.strip()
                if r.returncode != 0 or not ts_str:
                    git_err = r.stderr.strip()[:200] if r.returncode != 0 else None
                    # Distinguish "workspace isn't a git repo yet" — the UI surfaces
                    # this as an actionable "Init now" chip rather than a generic error.
                    needs_init = bool(git_err and "not a git repository" in git_err.lower())
                    entry: dict = {
                        "last_backup": None, "hours_ago": None,
                        # Only "stale" applies when the user opted into backup.
                        "stale": backup_configured and not needs_init,
                        "backup_configured": backup_configured,
                    }
                    if needs_init:
                        entry["needs_init"] = True
                    elif git_err:
                        entry["git_error"] = git_err
                    # Git couldn't answer (error OR no matching commit) but
                    # the run state can: a recorded success is better
                    # evidence than an unreadable/quiet repo. (needs_init
                    # stays authoritative — a state.json claiming success
                    # inside a non-repo is contradictory, and init is the
                    # fix either way.)
                    if not needs_init and last_success is not None:
                        hours_ago = (
                            _dt.datetime.now(_dt.timezone.utc) - last_success
                        ).total_seconds() / 3600
                        entry["last_backup"] = run_state.get("last_success_at")
                        entry["hours_ago"] = round(hours_ago, 1)
                        entry["stale"] = backup_configured and hours_ago > 26
                    result[bot_id] = _graft_run_state(entry, run_state)
                    continue
                last_backup = _parse_iso_utc(ts_str) or _dt.datetime.now(_dt.timezone.utc)
                # Freshness = the newer of (last successful attempt, last
                # backup commit). last_backup keeps reporting the commit
                # timestamp — "when did a backup commit last land" — while
                # staleness answers "is the daemon doing its job".
                freshness = max(
                    (t for t in (last_backup, last_success) if t is not None),
                )
                hours_ago = (_dt.datetime.now(_dt.timezone.utc) - freshness).total_seconds() / 3600
                result[bot_id] = _graft_run_state({
                    "last_backup": ts_str,
                    "hours_ago": round(hours_ago, 1),
                    # >26h = missed last nightly window. Only meaningful if backup is opted into.
                    "stale": backup_configured and hours_ago > 26,
                    "backup_configured": backup_configured,
                }, run_state)
            except Exception as e:
                result[bot_id] = _graft_run_state({
                    "last_backup": None, "hours_ago": None,
                    "stale": backup_configured,
                    "backup_configured": backup_configured, "error": str(e),
                }, run_state)

        # Live drift detection per bot — same logic heal uses, but folded into
        # this endpoint so the UI can render the "Accept as baseline" button
        # without an extra fan-out call.
        try:
            heal_mod = _module._import_analyzer("heal")
            for bot_id in bots:
                try:
                    os_user = get_bot_user(bot_id, net)
                    keys = heal_mod.detect_backup_drift_keys(bot_id, Path(net.get("sharedDir", "/Users/Shared/evolve")), net, os_user)
                    result.setdefault(bot_id, {})["drifted_keys"] = keys
                except Exception:
                    # Keep this endpoint robust — a single bot's drift check
                    # failing shouldn't blank the entire backup status table.
                    pass
        except Exception:
            pass

        any_configured = any(
            isinstance(bots_cfg.get(b), dict) and bots_cfg[b].get("backupRepoUrl")
            for b in bots
        )
        return {"bots": result, "any_configured": any_configured}

    @app.get("/api/backup/cloud/config")
    @app.get("/api/security/backup-config", endpoint="api_security_backup_config_get_alias")
    def api_security_backup_config_get() -> Response:
        """Return per-bot backup config + pubkeys.

        Per-bot: {bots: {bot_id: {backupRepoUrl, pubkey}}}
          pubkey is read from /Users/Shared/evolve/pubkeys/<bot>.pub when
          present (written by the Distribute Key endpoint as part of
          per-bot key setup), falling back to
          /Users/evolve/.ssh/evolve-backup-{bot_id}.pub for compatibility
          with pods that still have legacy admin-generated keys.

        With per-bot daemons, the SAME pubkey may appear under multiple
        bot entries — the operator copy-pasted one key value into each
        bot's per-bot file. The UI collapses that into a "shared key"
        indicator visually; the backend just returns what's there.
        """
        net = load_network(network_path)
        bots_cfg = net.get("bots", {}) if isinstance(net.get("bots"), dict) else {}
        all_ids = list(dict.fromkeys(list(bots_cfg.keys()) + ["evolve"]))
        shared_pub_dir = Path(net.get("sharedDir", "/Users/Shared/evolve")) / "pubkeys"
        result: dict = {}
        for bot_id in all_ids:
            cfg = bots_cfg.get(bot_id, {}) if isinstance(bots_cfg, dict) else {}
            # Prefer the shared-readable pubkey copy; fall back to evolve's
            # .ssh/ (legacy admin-generated keys).
            pubkey: str | None = None
            for p in (shared_pub_dir / f"{bot_id}.pub",
                      Path(f"/Users/evolve/.ssh/evolve-backup-{bot_id}.pub")):
                try:
                    if p.exists():
                        pubkey = p.read_text().strip()
                        break
                except (PermissionError, OSError):
                    continue
            result[bot_id] = {
                "backupRepoUrl": cfg.get("backupRepoUrl", ""),
                "pubkey": pubkey,
            }
        # Default backup account — used by the UI to render a specific URL
        # placeholder (git@github.com:<account>/<bot>-workspace.git) instead
        # of a generic one. Per-bot URLs can still point elsewhere
        # (e.g. team_bot_a lives under evolve-ops).
        default_account = (net.get("defaultBackupAccount") or "").strip() or None
        return jsonify({
            "bots": result,
            "default_account": default_account,
        })

    @app.patch("/api/backup/cloud/config")
    @app.patch("/api/security/backup-config", endpoint="api_security_backup_config_patch_alias")
    def api_security_backup_config_patch() -> Response:
        """Set backupRepoUrl for a bot. PATCH {botId, backupRepoUrl}

        Writes bots.{botId}.backupRepoUrl in network.json.  Passing an empty
        string clears the field (removes key).

        **Phase-1 visibility guard (spec-backup-and-data-classification-2026-05-28):**
        When setting a non-empty URL, verify the GitHub repo is private
        before saving. Reject with a structured error + settings deep-link
        when the repo is confirmed public. Allow when visibility can't be
        determined (PAT missing or lookup failed), but include a warning —
        the push-time guard in backup.py will still block actual data
        movement until verification succeeds. Clearing the URL never goes
        through the guard.
        """
        body = request.get_json() or {}
        bot_id = body.get("botId", "").strip()
        backup_url = body.get("backupRepoUrl", "").strip()
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        net = load_network(network_path)

        warning: str | None = None
        if backup_url:
            try:
                bv = _module._import_analyzer("backup_visibility")
                pat = bv.load_pat(net)
                if not pat:
                    warning = (
                        "Visibility cannot be verified: no GitHub PAT is "
                        "configured (keystore key `github_pat`). The URL was "
                        "saved, but pushes are blocked until a PAT is set — "
                        "run the Backup → Cloud onboarding to store one."
                    )
                else:
                    vis = bv.check_repo_visibility(backup_url, pat=pat)
                    if vis == "public":
                        parsed = bv.parse_github_repo(backup_url)
                        settings = (
                            f"https://github.com/{parsed[0]}/{parsed[1]}/settings"
                            if parsed else None
                        )
                        return jsonify({
                            "ok": False,
                            "error": (
                                "Refusing to save: the backup repo is public. "
                                "Set it to private first, then retry."
                            ),
                            "code": "repo_public",
                            "settings_url": settings,
                        }), 400
                    if vis == "unknown":
                        warning = (
                            "Visibility could not be confirmed (lookup "
                            "returned 'unknown'). Verify the PAT has read "
                            "scope for this repo. Pushes are blocked until "
                            "the repo is confirmed private."
                        )
            except Exception as exc:  # noqa: BLE001
                # Guard is best-effort at config time. The push-time guard is
                # the safety net; don't block a config save on a transient
                # importer/network blip here.
                warning = f"Visibility check skipped: {exc!s}"

        net.setdefault("bots", {}).setdefault(bot_id, {})
        if backup_url:
            net["bots"][bot_id]["backupRepoUrl"] = backup_url
        else:
            net["bots"][bot_id].pop("backupRepoUrl", None)
            # Remove the bot entry entirely if it's now empty — an empty entry
            # causes _all_bots() to surface the bot in OC status/audit calls.
            if not net["bots"][bot_id]:
                del net["bots"][bot_id]
        save_network(net, network_path)
        resp = {"ok": True, "botId": bot_id, "backupRepoUrl": backup_url}
        if warning:
            resp["warning"] = warning
        return jsonify(resp)

    @app.post("/api/backup/cloud/keys/generate")
    @app.post("/api/security/backup-config/generate-key", endpoint="api_security_backup_generate_key_alias")
    def api_security_backup_generate_key() -> Response:
        """Ensure the canonical pod-wide backup keypair exists, return its pubkey.

        Under the unified shared-key model the ``botId`` body field is
        retained for back-compat but ignored — every bot uses the same
        canonical source at ``/Users/evolve/.ssh/evolve-backup-shared``.
        Legacy callers that passed ``{botId: "X"}`` get the canonical
        pubkey back, which is the correct answer for any bot.

        POST {botId?} → {ok, pubkey, generated}
        """
        from .. import backup_keys
        body = request.get_json() or {}
        bot_id = (body.get("botId") or "").strip()
        try:
            ok, generated, err = backup_keys.ensure_shared_source_generated()
            if not ok:
                return jsonify({"ok": False, "error": err or "Key generation failed"}), 500
            pubkey = backup_keys.read_canonical_pubkey()
            if not pubkey:
                return jsonify({"ok": False, "error": "Canonical pubkey missing after generate"}), 500
            return jsonify({
                "ok": True,
                "botId": bot_id,
                "pubkey": pubkey,
                "generated": generated,
            })
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/backup/cloud/keys/distribute")
    @app.post("/api/security/backup-distribute-key", endpoint="api_security_backup_distribute_key_alias")
    def api_security_backup_distribute_key() -> Response:
        """Reconcile every bot to the unified shared-key model.

        Thin wrapper around ``backup_keys.reconcile_pod(force_distribute=True)``,
        which is the only writer for the canonical paths. The reconciler:

          1. Generates the canonical Ed25519 source at
             ``/Users/evolve/.ssh/evolve-backup-shared{,.pub}`` if missing.
          2. For every bot with ``backupRepoUrl`` set, copies the shared
             pair into ``/Users/<user>/.ssh/evolve-backup-<bot>{,.pub}``
             via the standard /tmp staging + sudo cp pattern, and
             mirrors the pubkey to ``{shared_dir}/pubkeys/<bot>.pub``.
          3. If a pod-wide GitHub PAT is available, registers the canonical
             pubkey as a deploy key on every backup repo (clause 4 of the
             invariant). Existing per-bot deploy keys are LEFT IN PLACE
             — operator preference recorded 2026-06-07.
          4. If every bot aligns, writes the ``network.json::backup.key_mode = "shared"``
             sentinel so the periodic reconciler knows the pod has migrated.

        See ``internal/spec-backup-key-distribution-unification-2026-06-08.md``
        for the full invariant and migration story.

        Returns ``ReconcileReport.to_dict()`` — the legacy
        ``{ok, pubkey, results: [{bot_id, status}]}`` shape is preserved
        in the ``pubkey`` and ``results`` aliases for any consumer that
        still reads them.
        """
        from .. import backup_keys
        body = request.get_json(silent=True) or {}
        # `generate` and `source_path` survive as no-op back-compat:
        # the reconciler always uses the canonical source.
        _ = body.get("generate", True), body.get("source_path", "")

        net = load_network(network_path)
        token: str | None = None
        try:
            # Keystore-first with legacy network.json fallback (2.8).
            _bv = _module._import_analyzer("backup_visibility")
            token = _bv.load_pat(net)
        except Exception:  # noqa: BLE001
            token = None

        def _save(updated: dict) -> None:
            save_network(updated, network_path)

        # `_github_api` was lifted out of register_admin_routes's closure into
        # routes_admin_shared.py (4.1b Increment 0) and is now importable, so
        # this route uses the canonical caller (richer error handling + shared
        # timeout config) instead of backup_keys' module-private
        # `_default_github_api`. Same `GithubApiFn` shape.
        report = backup_keys.reconcile_pod(
            net,
            force_distribute=True,
            github_token=token,
            network_save=_save,
            github_api=_github_api,
        )

        legacy_results = [
            {
                "bot_id": r.bot_id,
                "status": (
                    "distributed" if r.status == backup_keys.BotSyncStatus.DISTRIBUTED
                    else r.status.value
                ),
                **({"error": r.error} if r.error else {}),
            }
            for r in report.distributed
        ]
        # Back-compat: graft `github_status` onto each result entry so
        # the UI's post-Distribute Key reporting still finds the field.
        #
        # Post-2026-06-08 the registration is one pod-wide POST to
        # /user/keys (NOT one per-repo deploy key). So `registered`
        # holds at most ONE result with bot_id="" (the user-key
        # outcome) plus optional per-bot URL-parse skips. Every bot
        # that successfully distributed inherits the pod-wide user-key
        # status — registered / already_present / failed — except when
        # that bot has its own skipped_reason (bad URL).
        user_key = next(
            (r for r in report.registered if r.bot_id == ""), None,
        )
        per_bot_skips = {
            r.bot_id: r for r in report.registered
            if r.bot_id and r.skipped_reason
        }
        if user_key is None:
            pod_status = "skipped:no_url"  # no targets had a backupRepoUrl
        elif user_key.skipped_reason:
            pod_status = f"skipped:{user_key.skipped_reason}"
        elif user_key.error:
            pod_status = f"failed:{user_key.error[:200]}"
        elif user_key.added:
            pod_status = "registered"
        else:
            pod_status = "already_present"

        for entry in legacy_results:
            bot_id = entry["bot_id"]
            if entry.get("status") != "distributed":
                entry["github_status"] = "skipped:distribute_failed"
                continue
            if not token:
                entry["github_status"] = "skipped:no_pat"
                continue
            # Per-bot URL skip overrides the pod-wide outcome — the
            # user needs to know which specific bot is misconfigured.
            skip = per_bot_skips.get(bot_id)
            if skip:
                entry["github_status"] = f"skipped:{skip.skipped_reason}"
            else:
                entry["github_status"] = pod_status

        out = report.to_dict()
        out["ok"] = report.error is None
        out["pubkey"] = report.canonical_pubkey
        out["source_path"] = str(backup_keys.SHARED_SOURCE_PRIV)
        out["results"] = legacy_results
        out["pat_available"] = bool(token)
        status_code = 200 if report.error is None else 500
        return jsonify(out), status_code

    @app.post("/api/backup/cloud/keys/push-test")
    def api_backup_cloud_keys_push_test() -> Response:
        """Probe each bot's backup remote with `git ls-remote` using the
        canonical shared SSH key.

        Surfaces the post-distribute failure mode that user-key
        registration silently allows: when the registered SSH key
        authenticates as a user who can read but not push to the bot's
        backup repo (e.g. a team-bot whose backup repo lives under an
        org where the PAT user is a read-only collaborator), Distribute
        Key reports ``registered`` even though the next backup will fail.

        This probe runs ``git ls-remote`` — a successful row confirms
        the SSH identity reaches the repo at all. It does NOT prove
        write access (a true push test would need a temp work-tree);
        for ls-remote failures the operator gets the raw stderr so
        they can read the GitHub auth error verbatim.

        Uses the canonical key at ``backup_keys._shared_priv()`` (env-
        overridable for tests). Under the unified shared-key model the
        canonical bytes are identical to every bot's deployed
        ``~/.ssh/evolve-backup-<bot>`` copy, so the probe is faithful
        to what backup.py would do at run-time. If a bot's local copy
        has drifted, ``Distribute Key`` is the fix — but the probe
        result still reflects what GitHub would tell the deployed key.

        Body: ``{"botId": "<id>"}`` (optional — probe one bot) or
        ``{}`` (probe every bot with a backupRepoUrl).

        Returns ``{ok, results: [{bot_id, status, stderr?}], ...}`` where
        ``status`` is ``ok | failed | skipped:no_url``.
        """
        from .. import backup_keys
        import os as _os

        body = request.get_json(silent=True) or {}
        bot_filter = (body.get("botId") or "").strip() or None

        net = load_network(network_path)
        bots = net.get("bots", {}) or {}

        src_priv = backup_keys._shared_priv()
        if not src_priv.exists():
            return jsonify({
                "ok": False,
                "error": (
                    f"Canonical shared key missing at {src_priv}. "
                    "Run Distribute Key first to generate it."
                ),
                "results": [],
            }), 400

        results: list[dict] = []
        for bot_id, info in bots.items():
            if bot_filter and bot_id != bot_filter:
                continue
            url = ((info or {}).get("backupRepoUrl") or "").strip()
            if not url:
                results.append({"bot_id": bot_id, "status": "skipped:no_url"})
                continue
            # Use the canonical key directly. Empty HOME so SSH doesn't
            # silently pick up an unrelated agent identity in the
            # daemon's env. IdentitiesOnly=yes pins to the -i key.
            # StrictHostKeyChecking=accept-new lets the probe succeed
            # on a fresh deploy where known_hosts is empty.
            env = dict(_os.environ)
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {src_priv} -o BatchMode=yes "
                "-o StrictHostKeyChecking=accept-new "
                "-o IdentitiesOnly=yes"
            )
            env["HOME"] = "/tmp"
            try:
                proc = subprocess.run(
                    ["git", "ls-remote", url],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if proc.returncode == 0:
                    results.append({"bot_id": bot_id, "status": "ok"})
                else:
                    stderr = (proc.stderr or "").strip()[:600]
                    results.append({
                        "bot_id": bot_id,
                        "status": "failed",
                        "stderr": stderr or f"git ls-remote exit {proc.returncode}",
                    })
            except subprocess.TimeoutExpired:
                results.append({
                    "bot_id": bot_id,
                    "status": "failed",
                    "stderr": "git ls-remote timed out after 20s",
                })
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "bot_id": bot_id,
                    "status": "failed",
                    "stderr": str(exc)[:600],
                })

        return jsonify({
            "ok": True,
            "results": results,
            "source_path": str(src_priv),
        }), 200

    @app.post("/api/backup/cloud/init")
    @app.post("/api/security/backup-init", endpoint="api_security_backup_init_alias")
    def api_security_backup_init() -> Response:
        """Initialize a bot's workspace as a git repo (`git init -b main`).

        POST {botId} → {ok, status}
        Idempotent. status is one of: initialized, already-initialized,
        no-workspace, failed:<reason>, refused:repo_public.

        **Phase-1 visibility guard:** if the bot has a backupRepoUrl
        configured, verify it's not public before initializing. Refuses
        only on confirmed-public (the strongest signal). Unknown/missing-
        PAT cases pass through; the push-time guard catches them.
        """
        body = request.get_json() or {}
        bot_id = body.get("botId", "").strip()
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        try:
            net = load_network(network_path)
            backup_url = ((net.get("bots") or {}).get(bot_id) or {}).get("backupRepoUrl", "")
            if backup_url:
                try:
                    bv = _module._import_analyzer("backup_visibility")
                    pat = bv.load_pat(net)
                    if pat:
                        vis = bv.check_repo_visibility(backup_url, pat=pat)
                        if vis == "public":
                            parsed = bv.parse_github_repo(backup_url)
                            settings = (
                                f"https://github.com/{parsed[0]}/{parsed[1]}/settings"
                                if parsed else None
                            )
                            return jsonify({
                                "ok": False,
                                "botId": bot_id,
                                "status": "refused:repo_public",
                                "error": (
                                    "Refusing to initialize: the configured "
                                    "backup repo is public. Set it to "
                                    "private at the repo settings page, then "
                                    "retry."
                                ),
                                "code": "repo_public",
                                "settings_url": settings,
                            }), 400
                except Exception:
                    # Best-effort guard; the push-time guard is the safety net.
                    pass
            ok, status = _module.ensure_workspace_git_init(bot_id)
            return jsonify({"ok": ok, "botId": bot_id, "status": status})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/security/drift-detail")
    def api_security_drift_detail() -> Response:
        """Return live-vs-baseline drift keys for a bot, fresh from disk.

        Distinct from /api/security/drift-status, which reads historical
        incidents. This one runs the same comparison heal does, so the UI
        can show the operator exactly which openclaw.json top-level keys
        will be accepted before they click "Accept as baseline".
        """
        bot_id = (request.args.get("botId") or "").strip()
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        try:
            heal_mod = _module._import_analyzer("heal")
            os_user = get_bot_user(bot_id, net)
            keys = heal_mod.detect_backup_drift_keys(bot_id, shared, net, os_user)
            return jsonify({"botId": bot_id, "drifted_keys": keys})
        except Exception as e:
            return jsonify({"botId": bot_id, "drifted_keys": [], "error": str(e)}), 500

    @app.get("/api/backup/local/status")
    def api_backup_local_status() -> Response:
        """Phase 2: read-only Time Machine status.

        Wraps ``tmutil`` via ``analyzer.local_backup``. Returns the
        LocalBackupStatus.to_dict() shape. Cheap; safe to poll. On
        non-macOS environments, returns ``{"available": false}`` so
        the Local subtab can render a "not applicable" panel without
        special-casing the response.

        Checks two pod paths against TM exclusions:
          - the shared directory ({sharedDir} from network.json)
          - the deploy checkout at /Users/Shared/evolve-repo
        Either being excluded fires the ``workspace_excluded`` Signal
        from the ``local_backup_signal`` monitor.
        """
        try:
            lb = _module._import_analyzer("local_backup")
        except Exception as exc:  # noqa: BLE001
            return jsonify({
                "available": False,
                "configured": False,
                "error": f"local_backup module unavailable: {exc!s}",
            }), 200
        net = load_network(network_path)
        pod_paths = [
            Path(net.get("sharedDir", "/Users/Shared/evolve")),
            Path(get_profile().deploy_checkout_default),
        ]
        # Daemon hostname so the UI can tell whether the browser is on the
        # same Mac as Time Machine. The "Open Time Machine settings" button
        # uses an x-apple.systempreferences URL, which only affects the
        # machine running the browser — clicking it from a laptop viewing a
        # remote pod opens the laptop's settings, not the mini's.
        import socket
        try:
            host_short = socket.gethostname().split(".")[0].lower()
        except OSError:
            host_short = ""
        try:
            status = lb.get_local_backup_status(pod_paths=pod_paths)
            payload = status.to_dict()
            payload["hostname"] = host_short
            return jsonify(payload)
        except Exception as exc:  # noqa: BLE001
            # The wrapper itself catches expected failures; this branch is
            # truly unexpected and should surface clearly to the operator.
            return jsonify({
                "available": True,
                "configured": False,
                "error": f"tmutil wrapper crashed: {exc!s}",
                "hostname": host_short,
            }), 200

    @app.get("/api/backup/cloud/preflight")
    def api_backup_cloud_preflight() -> Response:
        """Phase 4d: estimate what the next cloud backup will push.

        Query: ?botId=<bot>

        Walks the bot's workspace, classifies each file via the same
        data_classification resolver backup.py uses, and returns per-
        classification counts + byte totals. The Cloud subtab fires this
        on tab activate so the operator sees "this will push ~N MB
        across M files" before clicking ``Backup now`` — useful for
        verifying classification changes did what was expected.

        Response shape mirrors backup_preflight.compute_preflight_summary's
        return value, plus a top-level ``bot_id`` echo.
        """
        bot_id = (request.args.get("botId") or "").strip()
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        try:
            preflight = _module._import_analyzer("backup_preflight")
            dc = _module._import_analyzer("data_classification")
        except Exception as exc:  # noqa: BLE001
            return jsonify({
                "bot_id": bot_id,
                "error": f"module unavailable: {exc!s}",
                "by_classification": {}, "total": {"files": 0, "bytes": 0},
            }), 200
        net = load_network(network_path)
        try:
            workspace = _module._bot_home(bot_id, net) / ".openclaw" / "workspace"
            manifests = dc.load_bot_manifests(workspace)
            resolver = dc.build_resolver(
                manifests=manifests,
                network=net,
                fallback_default="cloud",
            )
            summary = preflight.compute_preflight_summary(workspace, resolver)
        except Exception as exc:  # noqa: BLE001
            return jsonify({
                "bot_id": bot_id,
                "error": f"preflight failed: {exc!s}",
                "by_classification": {}, "total": {"files": 0, "bytes": 0},
            }), 200
        summary["bot_id"] = bot_id
        return jsonify(summary)

    @app.get("/api/backup/data/overview")
    def api_backup_data_overview() -> Response:
        """Phase 3b: per-bot per-app classification + pod-wide rules.

        Reads each bot's manifests from their workspace using the same
        discovery as the Apps tab (``_list_manifests_as_bot`` + v7-arc
        hydration + sudo-cat permission fallback). This guarantees the
        Data tab and Apps tab agree on which apps a bot has — the
        2026-05-29 bug where the Data tab silently dropped every v7-arc
        Instance manifest was traced to a stricter filter here.

        The pod-wide block comes from network.json::backup. Both
        sections are operator-editable from the Data tab via the
        per-app and pod PATCH endpoints.
        """
        try:
            dc = _module._import_analyzer("data_classification")
        except Exception as exc:  # noqa: BLE001
            return jsonify({
                "error": f"data_classification module unavailable: {exc!s}",
                "bots": {}, "pod_wide": {},
            }), 200
        from ..applications.manifest import (
            MANIFEST_SHAPE_V7_ARC,
            hydrate_v7_arc_instance,
        )
        net = load_network(network_path)
        bots_cfg = net.get("bots", {}) if isinstance(net.get("bots"), dict) else {}
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))

        bots_out: dict[str, dict] = {}
        for bot_id in sorted(bots_cfg.keys()):
            apps_out: list[dict] = []
            try:
                manifest_paths = _module._list_manifests_as_bot(bot_id)
            except Exception:
                manifest_paths = []
            for mf_path in manifest_paths:
                try:
                    try:
                        m = json.loads(Path(mf_path).read_text())
                    except PermissionError:
                        # Mirror Apps tab's robustness — bots not yet
                        # deployed via set_evolve_read_acl need sudo cat.
                        r = subprocess.run(
                            ["sudo", "/bin/cat", mf_path],
                            capture_output=True, text=True, timeout=5,
                        )
                        if r.returncode != 0:
                            continue
                        m = json.loads(r.stdout)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(m, dict):
                    continue
                # v7-arc Instances carry name/description on the bound Spec.
                # Hydrate so operator-visible labels match what the Apps tab
                # shows. Safe to call always — returns the instance unchanged
                # when no Spec is found.
                if m.get("manifest_shape") == MANIFEST_SHAPE_V7_ARC:
                    try:
                        m = hydrate_v7_arc_instance(m, shared)
                    except Exception:  # noqa: BLE001
                        pass
                # Derive id from the filename when missing (legacy
                # manifests + un-hydratable v7-arc instances).
                manifest_id = (m.get("id") or "").strip() or Path(mf_path).stem
                apps_out.append({
                    "id":   manifest_id,
                    "name": (
                        m.get("display_name") or m.get("name")
                        or manifest_id or "(unnamed)"
                    ).strip(),
                    "current_tier":            dc.infer_tier(m),
                    "app_files_privacy":       m.get("app_files_privacy") or "",
                    "default_for_unclassified": m.get("default_for_unclassified") or "",
                    "data_paths":              list(m.get("data_paths") or []),
                    "schema_version":          int(m.get("schema_version") or 0),
                })
            bot_cfg = bots_cfg.get(bot_id) or {}
            bots_out[bot_id] = {
                "apps": apps_out,
                "backup_default_tier": (bot_cfg.get("backup_default_tier") or "")
                    if isinstance(bot_cfg, dict) else "",
            }

        pod_backup = net.get("backup") if isinstance(net.get("backup"), dict) else {}
        pod_wide_out = {
            "data_paths":              list(pod_backup.get("data_paths") or []),
            "default_for_unclassified": pod_backup.get("default_for_unclassified") or "",
        }
        return jsonify({"bots": bots_out, "pod_wide": pod_wide_out})

    @app.patch("/api/backup/data/app")
    def api_backup_data_app_patch() -> Response:
        """Phase 3b: update one app manifest's v15 classification fields.

        Body: {bot_id, app_id, tier?, app_files_privacy?, data_paths?,
               default_for_unclassified?}

        When ``tier`` is one of the four shortcut names, the template
        from data_classification.TIER_TEMPLATES is applied first; any
        explicit field overrides on the same request are layered on top
        so the operator can fine-tune in a single call. Writes back to
        the manifest at /Users/<bot>/.openclaw/workspace/manifests/.
        """
        body = request.get_json() or {}
        bot_id = (body.get("bot_id") or "").strip()
        app_id = (body.get("app_id") or "").strip()
        if not bot_id or not app_id:
            return jsonify({"ok": False, "error": "bot_id + app_id required"}), 400

        try:
            dc = _module._import_analyzer("data_classification")
            from ..applications.manifest import load_manifest, save_manifest, VALID_PRIVACY
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"module unavailable: {exc!s}"}), 500

        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        manifest = load_manifest(app_id, bot_id, shared)
        if manifest is None:
            return jsonify({
                "ok": False, "error": f"manifest not found for {bot_id}/{app_id}",
            }), 404

        # Round-trip through dict for the tier helper, then re-hydrate.
        data = manifest.to_dict()

        # 1. Apply tier template (if requested).
        tier = (body.get("tier") or "").strip()
        if tier:
            try:
                data = dc.apply_tier_to_manifest(data, tier)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400

        # 2. Direct field overrides — layered on top of tier template.
        if "app_files_privacy" in body:
            afp = (body.get("app_files_privacy") or "").strip()
            if afp not in ("", "cloud", "local"):
                return jsonify({"ok": False, "error": f"invalid app_files_privacy: {afp!r}"}), 400
            data["app_files_privacy"] = afp
        if "default_for_unclassified" in body:
            d = (body.get("default_for_unclassified") or "").strip()
            if d not in ("", "cloud", "local", "ephemeral"):
                return jsonify({"ok": False, "error": f"invalid default_for_unclassified: {d!r}"}), 400
            data["default_for_unclassified"] = d
        if "data_paths" in body:
            raw_paths = body.get("data_paths") or []
            if not isinstance(raw_paths, list):
                return jsonify({"ok": False, "error": "data_paths must be a list"}), 400
            cleaned: list[dict] = []
            for entry in raw_paths:
                if not isinstance(entry, dict):
                    continue
                path_v = (entry.get("path") or "").strip()
                priv = (entry.get("privacy") or "").strip()
                if not path_v or priv not in VALID_PRIVACY:
                    return jsonify({
                        "ok": False,
                        "error": f"invalid data_paths entry: {entry!r}",
                    }), 400
                clean_entry = {"path": path_v, "privacy": priv}
                if entry.get("note"):
                    clean_entry["note"] = str(entry["note"])
                cleaned.append(clean_entry)
            data["data_paths"] = cleaned

        # 3. Persist.
        from ..applications.manifest import ApplicationManifest
        try:
            updated = ApplicationManifest.from_dict(data)
            save_manifest(updated, shared)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"save failed: {exc!s}"}), 500

        # Phase 4-review: per-app ``default_for_unclassified`` is stored but
        # not enforced at runtime — see the limitation note in
        # data_classification._rules_from_app_manifest. Surface a warning
        # so operators don't expect it to take effect on the next backup.
        resp = {
            "ok": True,
            "bot_id": bot_id,
            "app_id": app_id,
            "current_tier": dc.infer_tier(data),
            "app_files_privacy": data.get("app_files_privacy") or "",
            "default_for_unclassified": data.get("default_for_unclassified") or "",
            "data_paths": data.get("data_paths") or [],
        }
        if data.get("default_for_unclassified"):
            resp["warning"] = (
                "Per-app default_for_unclassified is stored on the manifest "
                "but is not yet enforced at runtime. Until app-scope "
                "discovery lands, declare explicit data_paths for each "
                "directory you want classified differently from the pod "
                "default. Pod-wide default_for_unclassified IS enforced."
            )
        return jsonify(resp)

    @app.patch("/api/backup/data/bot")
    def api_backup_data_bot_patch() -> Response:
        """Phase 3b extension (2026-05-29): per-bot default tier.

        Body: {bot_id, tier, apply_to_existing: bool}

        Stores ``network.json::bots[bot_id].backup_default_tier`` so
        future-installed apps can inherit a sensible default tier. When
        ``apply_to_existing=true``, iterates every manifest under the
        bot and applies the tier template, giving operators a one-click
        "set everything to local" affordance. ``apply_to_existing=false``
        only records the default for future use.

        Picking ``some_data_local`` is always a no-op on existing apps
        (operator authors per-path individually) and just stores the
        default.

        ``tier=""`` clears the per-bot default.
        """
        body = request.get_json() or {}
        bot_id = (body.get("bot_id") or "").strip()
        tier = (body.get("tier") or "").strip()
        apply_to_existing = bool(body.get("apply_to_existing"))
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        try:
            dc = _module._import_analyzer("data_classification")
            from ..applications.manifest import (
                ApplicationManifest,
                load_manifest,
                save_manifest,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"module unavailable: {exc!s}"}), 500

        if tier and tier not in dc.VALID_TIERS:
            return jsonify({
                "ok": False, "error": f"unknown tier: {tier!r}",
            }), 400

        net = load_network(network_path)
        bots = net.setdefault("bots", {})
        bot_cfg = bots.setdefault(bot_id, {})
        if tier:
            bot_cfg["backup_default_tier"] = tier
        else:
            bot_cfg.pop("backup_default_tier", None)
        save_network(net, network_path)

        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        apps_updated = 0
        # The ``some_data_local`` template is a no-op (operator authors
        # per-path) so we skip the iteration. Empty tier also skips.
        if apply_to_existing and tier and tier != dc.TIER_SOME_DATA_LOCAL:
            for mf_path in _module._list_manifests_as_bot(bot_id):
                try:
                    raw = json.loads(Path(mf_path).read_text())
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(raw, dict):
                    continue
                app_id = (raw.get("id") or "").strip() or Path(mf_path).stem
                manifest = load_manifest(app_id, bot_id, shared)
                if manifest is None:
                    continue
                data = manifest.to_dict()
                try:
                    data = dc.apply_tier_to_manifest(data, tier)
                    updated = ApplicationManifest.from_dict(data)
                    save_manifest(updated, shared)
                    apps_updated += 1
                except Exception:  # noqa: BLE001 — one bad manifest shouldn't blank the rest
                    continue

        return jsonify({
            "ok": True,
            "bot_id": bot_id,
            "backup_default_tier": tier or "",
            "apps_updated": apps_updated,
        })

    @app.patch("/api/backup/data/pod")
    def api_backup_data_pod_patch() -> Response:
        """Phase 3b: update pod-wide classification rules in network.json.

        Body: {data_paths?, default_for_unclassified?}

        Writes ``network.json::backup.{data_paths, default_for_unclassified}``.
        Same validation rules as the per-app PATCH.
        """
        body = request.get_json() or {}
        try:
            from ..applications.manifest import VALID_PRIVACY
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"module unavailable: {exc!s}"}), 500

        net = load_network(network_path)
        backup_block = dict(net.get("backup") or {}) if isinstance(net.get("backup"), dict) else {}

        if "default_for_unclassified" in body:
            d = (body.get("default_for_unclassified") or "").strip()
            if d not in ("", "cloud", "local", "ephemeral"):
                return jsonify({"ok": False, "error": f"invalid default_for_unclassified: {d!r}"}), 400
            if d:
                backup_block["default_for_unclassified"] = d
            else:
                backup_block.pop("default_for_unclassified", None)

        if "data_paths" in body:
            raw_paths = body.get("data_paths") or []
            if not isinstance(raw_paths, list):
                return jsonify({"ok": False, "error": "data_paths must be a list"}), 400
            cleaned: list[dict] = []
            for entry in raw_paths:
                if not isinstance(entry, dict):
                    continue
                path_v = (entry.get("path") or "").strip()
                priv = (entry.get("privacy") or "").strip()
                if not path_v or priv not in VALID_PRIVACY:
                    return jsonify({
                        "ok": False,
                        "error": f"invalid data_paths entry: {entry!r}",
                    }), 400
                clean_entry = {"path": path_v, "privacy": priv}
                if entry.get("note"):
                    clean_entry["note"] = str(entry["note"])
                cleaned.append(clean_entry)
            backup_block["data_paths"] = cleaned

        # Empty backup block → remove the key entirely so the absence is clean.
        if backup_block:
            net["backup"] = backup_block
        else:
            net.pop("backup", None)
        save_network(net, network_path)
        return jsonify({
            "ok": True,
            "data_paths":              backup_block.get("data_paths") or [],
            "default_for_unclassified": backup_block.get("default_for_unclassified") or "",
        })

    @app.post("/api/backup/cloud/run")
    @app.post("/api/maintenance/backup-now", endpoint="api_maintenance_backup_now_alias")
    def api_maintenance_backup_now() -> Response:
        """Trigger the nightly backup for one bot, or for every configured bot.

        Body: {botId?: str}. When botId is set, kicks that one bot's daemon.
        When omitted, kicks every bot's daemon for bots with backupRepoUrl set.

        Kickstart is async — the daemon runs as the bot user (not as
        evolve) so the admin server can't run backup.py directly. Returns
        {ok, results: [{bot_id, status}]} where status ∈
        {kicked, no-url, no-daemon, failed}. The actual backup outcome
        shows up in the per-bot state file on the next /api/security/
        backup-status poll.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("botId") or "").strip()
        net = load_network(network_path)
        bots_cfg = net.get("bots", {}) if isinstance(net.get("bots"), dict) else {}

        if bot_id:
            targets = [bot_id]
        else:
            targets = [
                b for b, c in bots_cfg.items()
                if isinstance(c, dict) and c.get("backupRepoUrl")
            ]

        results: list[dict] = []
        for bid in targets:
            cfg = bots_cfg.get(bid, {}) if isinstance(bots_cfg, dict) else {}
            url = cfg.get("backupRepoUrl", "") if isinstance(cfg, dict) else ""
            if not url:
                results.append({"bot_id": bid, "status": "no-url"})
                continue
            label = f"ai.evolve.{bid}.backup"  # system domain — the scheduler default
            try:
                ok, out = get_scheduler().restart(label)
                if ok:
                    results.append({"bot_id": bid, "status": "kicked"})
                else:
                    err = out.strip()[:400]
                    # Distinguish "daemon not installed" (label doesn't exist
                    # in launchd) from other failures so the operator knows
                    # whether to re-run deploy_bot or check sudoers.
                    status = "no-daemon" if "Could not find" in err else "failed"
                    results.append({"bot_id": bid, "status": status, "error": err})
            except Exception as e:
                results.append({"bot_id": bid, "status": "failed", "error": str(e)})

        return jsonify({"ok": True, "results": results})

    @app.post("/api/security/accept-drift")
    def api_security_accept_drift() -> Response:
        """Commit current openclaw.json as the new backup baseline (local only).

        Used when an operator has reviewed a heal-reported config_drift alert
        and chosen to accept it. Shells out to ``backup.py --commit-baseline-local``
        under ``sudo -H -u <bot_user>`` so the resulting commit, the ``.git/``
        writes (notably ``index.lock``) and ``evolve-backup/`` stay bot-owned —
        the workspace ``.git/`` dir is bot-owned with only a read ACL for evolve,
        so an in-process call from the admin daemon (evolve user) hits EACCES on
        ``index.lock``. Mirrors ``deploy._baseline_refresh_as_bot_user``.
        """
        body = request.get_json() or {}
        bot_id = (body.get("botId") or "").strip()
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        net = load_network(network_path)
        try:
            bot_user = get_bot_user(bot_id, net)
            backup_py = _ANALYZER_DIR / "backup.py"
            if not backup_py.exists():
                return jsonify({
                    "ok": False, "botId": bot_id,
                    "error": f"backup.py not found at {backup_py}",
                }), 500
            cmd = [
                "sudo", "-H", "-u", bot_user,
                "/usr/bin/python3", str(backup_py),
                "--commit-baseline-local", bot_id,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            out_lines = (r.stdout.strip() or r.stderr.strip() or "").splitlines()
            tail = out_lines[-1] if out_lines else f"rc={r.returncode}"
            if r.returncode == 0:
                return jsonify({"ok": True, "botId": bot_id, "message": tail})
            return jsonify({"ok": False, "botId": bot_id, "error": tail}), 500
        except subprocess.TimeoutExpired:
            return jsonify({
                "ok": False, "botId": bot_id, "error": "timed out after 30s",
            }), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/security/drift-status")
    def api_security_drift_status() -> Response:
        """Return config drift check status per bot (last incident per bot).

        Reads from shared_dir/incidents/ for config_drift type incidents.
        Returns {bot_id: {last_drift: ISO|null, drift_count_24h: int, ok: bool}}
        """
        import datetime as _dt
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        bots = _all_bots()
        incidents_dir = shared / "incidents"
        result: dict = {}

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)

        for bot_id in bots:
            last_drift_ts: str | None = None
            count_24h = 0
            if incidents_dir.exists():
                for day_dir in sorted(incidents_dir.iterdir(), reverse=True):
                    if not day_dir.is_dir():
                        continue
                    for f in sorted(day_dir.glob(f"{bot_id}-*-config_drift.json"), reverse=True):
                        try:
                            inc = json.loads(f.read_text())
                            # Coalesced incidents carry last_detected_at + a
                            # count (N identical detections collapsed into one
                            # file); fall back to the legacy single-shot shape.
                            ts = inc.get("last_detected_at") or inc.get("detected_at", "")
                            if not last_drift_ts:
                                last_drift_ts = ts
                            if ts:
                                inc_time = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                if inc_time >= cutoff:
                                    count_24h += int(inc.get("count", 1))
                        except (OSError, json.JSONDecodeError, ValueError):
                            pass
            result[bot_id] = {
                "last_drift": last_drift_ts,
                "drift_count_24h": count_24h,
                "ok": count_24h == 0,
            }

        return jsonify({"bots": result})

    @app.get("/api/security/quarantine")
    def api_security_quarantine() -> Response:
        """Return quarantined proposals (unsigned/tampered).

        Returns list of {id, target_bot, type, quarantine_reason, generated}
        sorted newest first. Max 50 entries.
        """
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        quarantine_dir = shared / "proposals" / "quarantine"
        items: list[dict] = []

        if quarantine_dir.exists():
            for f in sorted(quarantine_dir.glob("*.json"), reverse=True)[:50]:
                try:
                    p = json.loads(f.read_text())
                    items.append({
                        "id": p.get("id", f.stem),
                        "target_bot": p.get("target_bot", "?"),
                        "type": p.get("type", "?"),
                        "quarantine_reason": p.get("_quarantine_reason", "unknown"),
                        "generated": p.get("generated", ""),
                        "pattern_key": p.get("pattern_key", ""),
                    })
                except (OSError, json.JSONDecodeError):
                    pass

        return jsonify({"quarantined": items, "count": len(items)})

    @app.post("/api/security/alert-channel/test")
    def api_security_alert_channel_test() -> Response:
        """Send a test message via the standard alerts channel to verify security alerts work."""
        import subprocess as _sp
        net = load_network(network_path)
        alerts = net.get("alerts", {})
        channel = alerts.get("channel", "telegram")
        chat_id = alerts.get("chatId", "")

        if not chat_id:
            return jsonify({"ok": False, "error": "No alerts.chatId configured in network.json"}), 400

        try:
            msg = "🔐 Evolve Security Alert Channel — test message. If you see this, security alerts are working."
            r = _sp.run(
                ["openclaw", "message", "send", "--channel", channel, "--target", chat_id, "-m", msg],
                capture_output=True, text=True, timeout=15, cwd="/tmp",
            )
            if r.returncode == 0:
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": r.stderr.strip() or "openclaw message send failed"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/security/identity-status")
    def api_security_identity_status() -> Response:
        """Return identity hash check results per bot from the last audit.log.

        Reads the audit log and extracts the most recent identity finding per
        bot/file. Returns {bot_id: {files: {fname: {ok, detail}}, any_critical: bool}}
        """
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        audit_log = shared / "logs" / "audit.log"
        bots = _all_bots()

        # Parse last N lines of audit.log for identity entries
        latest: dict[str, dict] = {b: {"files": {}, "any_critical": False} for b in bots}
        try:
            lines = audit_log.read_text().splitlines()[-500:]
        except OSError:
            return jsonify({"bots": latest, "audit_log_found": False})

        for line in reversed(lines):
            for bot_id in bots:
                for fname in ("SOUL.md", "AGENTS.md", "HEARTBEAT.md"):
                    key = fname
                    if key in latest[bot_id]["files"]:
                        continue
                    if bot_id in line and fname in line and "identity" not in line:
                        # Match lines like: "[audit] OK: admin_bot: SOUL.md OK"
                        if "CRITICAL" in line and fname in line:
                            latest[bot_id]["files"][key] = {"ok": False, "detail": line.split("] ", 1)[-1]}
                            latest[bot_id]["any_critical"] = True
                        elif fname in line and ("OK" in line or "baseline" in line):
                            latest[bot_id]["files"][key] = {"ok": True, "detail": ""}

        return jsonify({"bots": latest, "audit_log_found": True})

    @app.get("/api/security/machine-status")
    def api_security_machine_status() -> Response:
        """Return machine-level security check results from the last audit.log.

        Returns {checks: [{name, ok, detail}], any_critical: bool, last_run: str|null}
        """
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        audit_log = shared / "logs" / "audit.log"

        checks: list[dict] = []
        last_run: str | None = None
        any_critical = False

        CHECK_NAMES = {
            "firewall": "Firewall",
            "SSH PasswordAuthentication": "SSH PasswordAuth",
            "SSH PermitRootLogin": "SSH PermitRootLogin",
            "user accounts": "User Accounts",
            "listening ports": "Listening Ports",
            "OC binary mtime": "OC Binary",
        }

        try:
            lines = audit_log.read_text().splitlines()[-500:]
        except OSError:
            return jsonify({"checks": [], "any_critical": False, "last_run": None,
                            "audit_log_found": False})

        seen: set[str] = set()
        for line in reversed(lines):
            if "machine:" in line or "CRITICAL" in line:
                ts = line.split(" ", 1)[0] if line else None
                if ts and not last_run:
                    last_run = ts
                for key, label in CHECK_NAMES.items():
                    if key in label or label in line or key.lower() in line.lower():
                        if label in seen:
                            continue
                        seen.add(label)
                        ok = "CRITICAL" not in line and "WARN" not in line
                        if "CRITICAL" in line:
                            any_critical = True
                        checks.append({
                            "name": label,
                            "ok": ok,
                            "detail": line.split("] ", 1)[-1] if "] " in line else line,
                        })

        return jsonify({"checks": checks, "any_critical": any_critical, "last_run": last_run,
                        "audit_log_found": True})

    # ── Host policy acceptances (Security → Host tab) ────────────────────
    # Operator-facing toggle for host-level security findings the
    # operator has deliberately accepted (e.g. "FileVault intentionally
    # off on this single-tenant dev mini"). Backed by
    # network.json::policy_acceptances which audit.py already consults
    # via ``policy_acceptance()`` — when an acceptance is recorded, the
    # corresponding check demotes from critical to ok and the firing
    # Signal is auto-resolved on the next audit cycle.
    #
    # Policies surfaced here:
    #   machine.filevault_off — opting out stops the FileVault-off alert
    #
    # Designed to extend to other host checks (macOS updates pending,
    # etc.) without new endpoints — add the policy descriptor to
    # ``_HOST_POLICY_DEFS`` and the UI picks it up.

    _HOST_POLICY_DEFS: dict[str, dict] = {
        "machine.filevault_off": {
            "title": "FileVault disk encryption",
        },
    }

    def _filevault_state() -> dict:
        """Read FileVault status via ``fdesetup status`` (no sudo needed).

        Returns ``{"state": "on"|"off"|"indeterminate"|"unknown", "detail": str}``.
        """
        try:
            r = subprocess.run(
                ["/usr/bin/fdesetup", "status"],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
            return {"state": "unknown", "detail": f"fdesetup unavailable: {e}"[:200]}
        out = (r.stdout or "").strip()
        out_low = out.lower()
        if "filevault is on" in out_low:
            state = "on"
        elif "filevault is off" in out_low:
            state = "off"
        else:
            state = "indeterminate"
        return {"state": state, "detail": out[:200]}

    def _host_policy_payload(policy_id: str, net: dict) -> dict:
        acceptances = net.get("policy_acceptances")
        if not isinstance(acceptances, dict):
            acceptances = {}
        entry = acceptances.get(policy_id)
        if not isinstance(entry, dict):
            entry = None
        descriptor = _HOST_POLICY_DEFS.get(policy_id, {})
        payload: dict = {
            "id": policy_id,
            "title": descriptor.get("title", policy_id),
            "acceptance": entry,
        }
        if policy_id == "machine.filevault_off":
            payload.update(_filevault_state())
        return payload

    @app.get("/api/security/host-policies")
    def api_security_host_policies() -> Response:
        """List host-level policies the operator can opt out of.

        Each entry includes the current host state (e.g. FileVault on/off)
        and the recorded acceptance (or ``null``). The UI renders one
        section per entry.
        """
        net = load_network(network_path)
        policies = [
            _host_policy_payload(pid, net) for pid in _HOST_POLICY_DEFS
        ]
        return jsonify({"policies": policies})

    @app.put("/api/security/host-policies/<policy_id>")
    def api_security_host_policy_set(policy_id: str) -> Response:
        """Set or clear an operator acceptance for a host-level policy.

        Body: ``{"intent": "use"|"opt_out", "reason": str?}``

        ``intent="use"`` clears any acceptance — the corresponding alert
        will resume firing on the next audit if the condition is present.

        ``intent="opt_out"`` writes ``policy_acceptances[<policy_id>]``
        with the operator's reason + timestamp. Any currently-firing
        Signal for this check is resolved immediately so the alert
        clears without waiting for the next 15-min audit cycle.
        """
        if policy_id not in _HOST_POLICY_DEFS:
            return jsonify({"error": f"unknown policy: {policy_id}"}), 404
        body = request.get_json(silent=True) or {}
        intent = body.get("intent")
        if intent not in {"use", "opt_out"}:
            return jsonify({"error": "intent must be 'use' or 'opt_out'"}), 400

        net = load_network(network_path)
        acceptances = net.get("policy_acceptances")
        if not isinstance(acceptances, dict):
            acceptances = {}

        if intent == "use":
            acceptances.pop(policy_id, None)
        else:
            reason = (body.get("reason") or "").strip() or "operator opted out via Security → Host"
            acceptances[policy_id] = {
                "reason": reason[:500],
                "accepted_at": datetime.now(timezone.utc).date().isoformat(),
                "accepted_by": "admin-ui",
            }

        if acceptances:
            net["policy_acceptances"] = acceptances
        else:
            net.pop("policy_acceptances", None)

        try:
            save_network(net, network_path)
        except Exception as exc:
            return jsonify({"error": f"failed to save network.json: {exc}"}), 500

        archived_signals = 0
        if intent == "opt_out":
            archived_signals = _resolve_host_policy_signals(policy_id, net)

        return jsonify({
            "ok": True,
            "policy": _host_policy_payload(policy_id, net),
            "archived_signals": archived_signals,
        })

    def _resolve_host_policy_signals(policy_id: str, net: dict) -> int:
        """Resolve any currently-firing audit Signal that matches this policy.

        We match by title keyword rather than reproducing the audit
        signature so we stay decoupled from audit.py's hashing scheme.
        Best-effort — failures here don't roll back the acceptance write.
        """
        keyword_by_policy = {
            "machine.filevault_off": "filevault",
        }
        keyword = keyword_by_policy.get(policy_id)
        if not keyword:
            return 0
        try:
            signals_store = _module._import_analyzer("signals.store")
        except Exception:
            return 0
        try:
            shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        except Exception:
            return 0
        count = 0
        try:
            active = list(signals_store.iter_active(shared, producer="audit"))
        except Exception:
            return 0
        for sig in active:
            title = (sig.title or "").lower()
            body = (sig.body or "").lower()
            if keyword not in title and keyword not in body:
                continue
            try:
                signals_store.apply_transition(
                    sig, "resolved", shared,
                    actor="user",
                    reason=f"operator opted out of {policy_id} via Security → Host",
                )
                count += 1
            except Exception:
                continue
        return count

    # ── Plain-language safety summary (B2.a) ────────────────────────────
    # Per-bot Carla-shape "What this bot can / cannot / unusual this week"
    # card. Replaces the jargon-heavy default of /page-security while
    # keeping the existing technical view behind an "Advanced view" toggle.
    # Spec: docs/evolve-mvp-sprint.md §Spec 2.

    def _resolve_cost_cap_usd(bot_id: str) -> float | None:
        """Best-effort: return this bot's effective daily hard cap in USD.

        Routed through better_engine_config so the cannot-do entry can
        say "Spend more than $X.XX/day" with a real number. Returns
        ``None`` on any error so the abstract phrasing wins — better
        to be vague than wrong.
        """
        try:
            from better_engine_config import load as load_be_config  # type: ignore
            shared = _shared_dir()
            cfg = load_be_config(shared)
            v = cfg.budget_hard_cap_usd(bot_id)
            return float(v) if v else None
        except Exception:
            return None

    def _build_one_safety_summary(bot_id: str) -> dict:
        from .. import safety_summary as _ss
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        try:
            home = _module._bot_home(bot_id, net)
        except Exception:
            # Last-resort fallback: resolve account name from network if possible.
            home = Path(f"/Users/{get_bot_user(bot_id, net)}")
        signals = _ss.signals_for_bot(shared, bot_id, days=7)
        cap = _resolve_cost_cap_usd(bot_id)
        summary = _ss.build_summary(
            bot_id,
            bot_home=home,
            signals=signals,
            cost_cap_usd=cap,
            bot_label=bot_id,
        )
        return summary.to_dict()

    @app.get("/api/security/bot-summary/<bot_id>")
    def api_security_bot_summary(bot_id: str) -> Response:
        """Plain-language safety card for one bot."""
        if bot_id not in _all_bots():
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        try:
            return jsonify(_build_one_safety_summary(bot_id))
        except Exception as exc:  # pragma: no cover — defensive
            log_request_error(exc)
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.get("/api/security/safety-summary")
    def api_security_safety_summary_all() -> Response:
        """Plain-language safety cards for every bot in the pod."""
        bots = _all_bots()
        out: dict[str, dict] = {}
        for bid in bots:
            try:
                out[bid] = _build_one_safety_summary(bid)
            except Exception as exc:
                out[bid] = {
                    "bot_id": bid,
                    "error": f"{type(exc).__name__}: {exc}",
                    "can_do": [],
                    "cannot_do": [],
                    "this_week_headline": "Couldn't check — see Advanced view",
                    "this_week_items": [],
                    "read_errors": [str(exc)],
                }
        return jsonify({"bots": out})

    @app.get("/api/security/upstream-version")
    def api_security_upstream_version() -> Response:
        """Per-bot OpenClaw version + fleet-wide minimum-floor rollup.

        Pillar V1.5-3 (Evolve v1.5 sprint). Surfaces the upstream OC
        version every bot is running plus whether the whole fleet meets
        the configured minimum (default ``2026.4.12`` — the release
        that shipped ``openclaw exec-policy``). The UI on the Safety
        page renders this as a fleet-status banner + a per-bot chip on
        each safety card.

        Optional query:
          * ``floor=YYYY.M.PATCH``  override the minimum-version floor
          * ``cli_fallback=0``      skip the ``openclaw --version``
                                    subprocess fallback (default: 1)
        """
        try:
            from .. import upstream_version as _uv
            from ..config import bot_home as _bot_home
        except ImportError as exc:  # pragma: no cover — defensive
            return jsonify({
                "ok": False, "error": f"upstream_version not importable: {exc}",
            }), 500

        net = load_network(network_path)
        floor = (request.args.get("floor") or _uv.DEFAULT_MINIMUM_VERSION).strip()
        allow_cli = (request.args.get("cli_fallback") or "1").strip() != "0"

        def _home_for(bid: str):
            try:
                return _bot_home(bid, net)
            except Exception:
                return Path(f"/Users/{bid}")

        readings = _uv.per_bot_versions(
            _all_bots(),
            network=net,
            bot_home_resolver=_home_for,
            allow_cli_fallback=allow_cli,
        )
        status = _uv.fleet_status(readings, minimum_version=floor)
        return jsonify({"ok": True, **status.to_dict()})

    # ── Config Health checks ────────────────────────────────────────────

    import urllib.request as _urllib_request
    import subprocess as _subproc

    @app.get("/api/config/health")
    def api_config_health() -> Response:
        """
        Per-bot config health checks — reads openclaw.json directly.

        Four high-stakes checks per bot:
          1. gateway_auth   — gateway.auth.token must be set
          2. compaction     — compaction.mode must not be "off"
          3. evolve_plugin  — plugin enabled + HTTP liveness
          4. primary_model  — primary model provider has an API key

        Returns {bot_id: {checks: [...], error: str|None}}
        """
        network_cfg = load_network(network_path)

        def _read_oc_cfg(bot_user: str, oc_path: str) -> dict:
            """Read openclaw.json as the admin user, falling back to sudo."""
            try:
                return json.loads(Path(oc_path).read_text())
            except PermissionError:
                pass
            except Exception:
                return {}
            try:
                r = _subproc.run(
                    ["sudo", "/bin/cat", oc_path],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    return json.loads(r.stdout)
            except Exception:
                pass
            return {}

        def _read_auth_profiles_raw(bot_user: str, profiles_path: str) -> dict:
            """Read auth-profiles.json — returns {profile_id: {...}} or {}."""
            raw_text = ""
            try:
                raw_text = Path(profiles_path).read_text()
            except PermissionError:
                try:
                    r = _subproc.run(
                        ["sudo", "/bin/cat", profiles_path],
                        capture_output=True, text=True, timeout=5,
                    )
                    if r.returncode == 0:
                        raw_text = r.stdout
                except Exception:
                    pass
            except Exception:
                pass
            if not raw_text.strip():
                return {}
            try:
                raw = json.loads(raw_text)
                # Canonical: {profiles: {id: {...}}}
                if isinstance(raw.get("profiles"), dict):
                    return raw["profiles"]
                # Flat: {id: {...}}
                if isinstance(raw, dict):
                    return raw
            except Exception:
                pass
            return {}

        def _check_bot(bot_id: str) -> dict:
            bot_cfg = network_cfg.get("bots", {}).get(bot_id, {})
            bot_user = bot_cfg.get("user") or bot_id
            port = bot_cfg.get("port")
            paths = resolve_bot_paths(bot_id, user=bot_user)

            oc_cfg = _read_oc_cfg(bot_user, paths["oc_config"])
            if not oc_cfg:
                return {"checks": [], "error": "Could not read openclaw.json"}

            auth_profiles = _read_auth_profiles_raw(bot_user, paths["auth_profiles"])
            # Also check auth embedded in openclaw.json
            oc_embedded = oc_cfg.get("auth", {}).get("profiles", {})
            if not auth_profiles and isinstance(oc_embedded, dict):
                auth_profiles = oc_embedded

            checks = []

            # ── Check 1: Gateway Auth ───────────────────────────────────
            gw_token = (oc_cfg.get("gateway", {}) or {}).get("auth", {}) or {}
            gw_token = gw_token.get("token", "") if isinstance(gw_token, dict) else ""
            if gw_token:
                checks.append({
                    "id": "gateway_auth", "severity": "critical",
                    "label": "Gateway Auth Configured",
                    "status": "ok", "icon": "✅",
                    "detail": "Gateway requires an auth token",
                    "fix": None, "fix_page": None,
                })
            else:
                checks.append({
                    "id": "gateway_auth", "severity": "critical",
                    "label": "Gateway Auth Configured",
                    "status": "critical", "icon": "❌",
                    "detail": "Gateway is unauthenticated — anyone on this machine can send requests to the bot",
                    "fix": "Run setup wizard or set gateway.auth.token manually",
                    "fix_page": "maintenance",
                })

            # ── Check 2: Context Compaction ─────────────────────────────
            comp = (oc_cfg.get("agents", {}) or {}).get("defaults", {}) or {}
            comp = (comp.get("compaction") or {})
            comp_mode = comp.get("mode", "") if isinstance(comp, dict) else ""
            comp_thresh = comp.get("threshold") if isinstance(comp, dict) else None
            if comp_mode == "off":
                checks.append({
                    "id": "compaction", "severity": "high",
                    "label": "Context Compaction Active",
                    "status": "critical", "icon": "❌",
                    "detail": "Compaction disabled — context can grow without limit. Long sessions may cost significantly more than expected.",
                    "fix": "Enable in Usage & Cost → Context Health → Compaction Settings",
                    "fix_page": "cost",
                })
            elif comp_mode:
                detail = f"mode: {comp_mode}"
                if comp_thresh is not None:
                    t = f"{comp_thresh:,}" if isinstance(comp_thresh, int) else str(comp_thresh)
                    detail += f", threshold: {t} tokens"
                checks.append({
                    "id": "compaction", "severity": "high",
                    "label": "Context Compaction Active",
                    "status": "ok", "icon": "✅",
                    "detail": detail,
                    "fix": None, "fix_page": None,
                })
            else:
                checks.append({
                    "id": "compaction", "severity": "high",
                    "label": "Context Compaction Active",
                    "status": "warn", "icon": "⚠️",
                    "detail": "Compaction mode not set — default behavior applies",
                    "fix": "Configure in Usage & Cost → Context Health → Compaction Settings",
                    "fix_page": "cost",
                })

            # ── Check 3: Evolve Plugin ──────────────────────────────────
            plugins_cfg = (oc_cfg.get("plugins", {}) or {}).get("entries", {}) or {}
            evolve_cfg = plugins_cfg.get("evolve", {}) if isinstance(plugins_cfg, dict) else {}
            plugin_enabled = bool(evolve_cfg.get("enabled")) if isinstance(evolve_cfg, dict) else False
            plugin_live = False
            if port:
                try:
                    with _urllib_request.urlopen(
                        f"http://localhost:{port}/evolve/status", timeout=3
                    ) as r:
                        plugin_live = json.loads(r.read()).get("bot_id") == bot_id
                except Exception:
                    pass

            if plugin_enabled and plugin_live:
                checks.append({
                    "id": "evolve_plugin", "severity": "high",
                    "label": "Evolve Plugin Installed and Active",
                    "status": "ok", "icon": "✅",
                    "detail": "Plugin installed and responding",
                    "fix": None, "fix_page": None,
                })
            elif plugin_enabled and not plugin_live:
                checks.append({
                    "id": "evolve_plugin", "severity": "high",
                    "label": "Evolve Plugin Installed and Active",
                    "status": "warn", "icon": "⚠️",
                    "detail": "Plugin configured but not responding — may be blocked (suspicious ownership) or gateway is down",
                    "fix": "See Maintenance → Plugin diagnostics",
                    "fix_page": "maintenance",
                })
            else:
                checks.append({
                    "id": "evolve_plugin", "severity": "high",
                    "label": "Evolve Plugin Installed and Active",
                    "status": "critical", "icon": "❌",
                    "detail": "Evolve plugin not installed — bot has no RSI loop, no CE, no quality tracking",
                    "fix": f"Run: evolve-admin deploy {bot_id}",
                    "fix_page": "maintenance",
                })

            # ── Check 4: Primary Model Reachable ────────────────────────
            model_cfg = (oc_cfg.get("agents", {}) or {}).get("defaults", {}) or {}
            model_cfg = model_cfg.get("model", "")
            if isinstance(model_cfg, dict):
                primary_model = model_cfg.get("primary", "")
            else:
                primary_model = str(model_cfg) if model_cfg else ""

            if not primary_model:
                checks.append({
                    "id": "primary_model", "severity": "high",
                    "label": "Primary Model Reachable",
                    "status": "warn", "icon": "⚠️",
                    "detail": "No primary model configured",
                    "fix": "Set model in AI Optimization → Model Catalog",
                    "fix_page": "ai-optimization",
                })
            else:
                provider = primary_model.split("/")[0] if "/" in primary_model else "anthropic"
                model_display = primary_model.replace(f"{provider}/", "")

                # Check auth-profiles for any key belonging to this provider
                provider_has_key = any(
                    (
                        pid.startswith(f"{provider}-") or
                        p.get("provider") == provider
                    ) and (p.get("key") or p.get("token") or p.get("value") or p.get("api_key"))
                    for pid, p in auth_profiles.items()
                    if isinstance(p, dict)
                )
                # Fallback: check auth order list — if provider is in auth.order, keys exist
                if not provider_has_key:
                    provider_has_key = bool(
                        oc_cfg.get("auth", {}).get("order", {}).get(provider)
                    )

                if provider_has_key:
                    checks.append({
                        "id": "primary_model", "severity": "high",
                        "label": "Primary Model Reachable",
                        "status": "ok", "icon": "✅",
                        "detail": f"{model_display} ({provider} key ✓)",
                        "fix": None, "fix_page": None,
                    })
                else:
                    checks.append({
                        "id": "primary_model", "severity": "high",
                        "label": "Primary Model Reachable",
                        "status": "critical", "icon": "❌",
                        "detail": f"Provider {provider!r} has no API key — sessions may fail or use wrong model. Model: {primary_model}",
                        "fix": f"Add {provider.title()} key in Integrations & Keys",
                        "fix_page": "integrations-keys",
                    })

            return {"checks": checks, "error": None}

        results = _run_parallel(_check_bot, _all_bots())
        return jsonify(results)

    _CHANNEL_DISPLAY = {
        "telegram": "Telegram", "slack": "Slack", "discord": "Discord",
        "email": "Email", "sms": "SMS", "whatsapp": "WhatsApp",
    }
    # Known AI/service providers that store credentials in auth-profiles.json
    _KNOWN_PROVIDERS = [
        "anthropic", "openai", "google", "xai", "mistral", "cohere",
        "groq", "perplexity", "together", "deepseek", "moonshot",
        "runway", "suno",
    ]

    def _scan_bot_integrations(bot_id: str) -> dict:
        """Return channels + api_keys for one bot. Callable from both GET and POST scan routes."""
        try:
            network_cfg = load_network(network_path)
            shared_dir = Path(network_cfg.get("sharedDir", "/Users/Shared/evolve"))
            bot_cfg = network_cfg.get("bots", {}).get(bot_id, {})
            port = bot_cfg.get("port")
            # bot_id is the logical name; system user may differ (e.g. bot="admin_bot", user="ocscout")
            bot_user = bot_cfg.get("user") or bot_id
            paths = resolve_bot_paths(bot_id, user=bot_user)

            # ── 1. Heal status file (written every 5min by heal.py as bot user) ────
            heal_status: dict = {}
            try:
                heal_status = json.loads((shared_dir / "status" / f"{bot_id}.json").read_text())
            except Exception:
                pass

            # ── 2. Evolve plugin liveness via HTTP ───────────────────────────────
            plugin_live = False
            if port:
                try:
                    with _urllib_request.urlopen(
                        f"http://localhost:{port}/evolve/status", timeout=5
                    ) as r:
                        plugin_live = json.loads(r.read()).get("bot_id") == bot_id
                except Exception:
                    pass

            # Check whether the plugin is configured in openclaw.json so we can
            # distinguish "never installed" from "installed but not responding".
            plugin_configured = False
            try:
                _oc_text = Path(paths["oc_config"]).read_text()
            except (PermissionError, OSError):
                try:
                    _r = _subproc.run(
                        ["sudo", "/bin/cat", paths["oc_config"]],
                        capture_output=True, text=True, timeout=5,
                    )
                    _oc_text = _r.stdout if _r.returncode == 0 else ""
                except Exception:
                    _oc_text = ""
            try:
                _oc_parsed = json.loads(_oc_text) if _oc_text.strip() else {}
                _plugin_entries = (_oc_parsed.get("plugins") or {})
                if isinstance(_plugin_entries, dict):
                    _plugin_entries = _plugin_entries.get("entries") or {}
                plugin_configured = bool(
                    isinstance(_plugin_entries, dict)
                    and _plugin_entries.get("evolve", {}).get("enabled")
                )
            except Exception:
                pass

            # ── 3. Channels from heal.py oc_channel_summary ──────────────────────
            channels: list[dict] = []
            for line in heal_status.get("oc_channel_summary") or []:
                line = str(line).strip()
                # Skip blank, indented, sub-item ("-" prefix), and lines without a colon
                if not line or line.startswith((" ", "-")) or ":" not in line:
                    continue
                name, _, rest = line.partition(":")
                name = name.strip(); rest_lower = rest.strip().lower()
                ch_id = name.lower()
                if any(w in rest_lower for w in ("configured", "active", "ok", "connected")):
                    ch_status, auth_state = "ok", "ok"
                elif any(w in rest_lower for w in ("not configured", "missing", "disconnected")):
                    ch_status, auth_state = "error", "missing"
                else:
                    ch_status, auth_state = "unknown", "unknown"
                channels.append({
                    "id": ch_id, "name": _CHANNEL_DISPLAY.get(ch_id, name.title()),
                    "status": ch_status, "auth_state": auth_state,
                    "details": rest.strip(), "source": "heal_status",
                    "last_message": None, "days_since_message": None,
                })

            # Fallback: gateway reachability if no channel summary yet
            if not channels and heal_status:
                gateway_ok = heal_status.get("gateway_reachable", False)
                channels.append({
                    "id": "gateway", "name": "OC Gateway",
                    "status": "ok" if gateway_ok else "error",
                    "auth_state": "ok" if gateway_ok else "unknown",
                    "details": heal_status.get("ts", ""), "source": "heal_status",
                    "last_message": None, "days_since_message": None,
                })

            # Evolve plugin HTTP probe entry — shows live/error for every bot including evolve
            if plugin_live:
                _plugin_status, _plugin_auth, _plugin_detail = "ok", "ok", f"live at :{port}"
            elif plugin_configured and not port:
                _plugin_status, _plugin_auth = "error", "missing"
                _plugin_detail = "plugin configured but no port in network.json — re-run setup wizard"
            elif plugin_configured:
                _plugin_status, _plugin_auth = "error", "missing"
                _plugin_detail = "configured but not responding — gateway may be down or restarting"
            else:
                _plugin_status, _plugin_auth = "error", "missing"
                _plugin_detail = f"not installed — run: sudo evolve-admin deploy {bot_id}"
            channels.append({
                "id": "oc_plugin", "name": "Evolve Plugin",
                "status": _plugin_status, "auth_state": _plugin_auth,
                "details": _plugin_detail,
                "source": "http_probe", "last_message": None, "days_since_message": None,
            })

            # ── 4. openclaw.json: channels section (primary) + plugins.entries ──
            oc_json_path = paths["oc_config"]
            oc_cfg_text: str | None = None
            try:
                oc_cfg_text = Path(oc_json_path).read_text()
            except PermissionError:
                try:
                    _r = _subproc.run(
                        ["sudo", "/bin/cat", oc_json_path],
                        capture_output=True, text=True, timeout=5,
                    )
                    if _r.returncode == 0 and _r.stdout.strip():
                        oc_cfg_text = _r.stdout
                except Exception:
                    pass
            except OSError:
                pass
            if oc_cfg_text:
                try:
                    oc_cfg = json.loads(oc_cfg_text)
                    seen_ids = {c["id"] for c in channels}
                    # channels section first — these are the real messaging channels.
                    # Guard: older openclaw.json versions write "channels": [] (a list)
                    # instead of a dict; treat any non-dict value as empty.
                    _channels_raw = oc_cfg.get("channels") or {}
                    if isinstance(_channels_raw, dict):
                        for ch_id, ch_cfg in _channels_raw.items():
                            if ch_id in seen_ids:
                                continue
                            configured = bool(ch_cfg) and ch_cfg != {}
                            enabled = ch_cfg.get("enabled", True) if configured else False
                            if not configured:
                                _status, _auth, _details = "warning", "missing", "empty config"
                            elif not enabled:
                                _status, _auth, _details = "warning", "disabled", "disabled"
                            else:
                                _status, _auth, _details = "ok", "ok", "configured"
                            channels.append({
                                "id": ch_id, "name": _CHANNEL_DISPLAY.get(ch_id, ch_id.title()),
                                "status": _status,
                                "auth_state": _auth,
                                "details": _details,
                                "source": "openclaw.json", "last_message": None, "days_since_message": None,
                            })
                            seen_ids.add(ch_id)
                    # plugins.entries second (skip channels already listed or covered by HTTP probe)
                    # "evolve" plugin is already represented by the "oc_plugin" HTTP probe entry — skip it
                    # to avoid a duplicate "Plugin: evolve" row alongside "Evolve Plugin".
                    # Guard: some openclaw.json versions write "plugins": [] (a list) or
                    # "plugins": {"entries": []} — treat any non-dict value as empty.
                    _PLUGIN_PROBE_IDS = {"evolve", "oc_plugin"}
                    _plugins_raw = oc_cfg.get("plugins") or {}
                    _entries_raw = _plugins_raw.get("entries") or {} if isinstance(_plugins_raw, dict) else {}
                    if isinstance(_entries_raw, dict):
                        for plugin_id, plugin_cfg in _entries_raw.items():
                            if plugin_id in seen_ids or plugin_id in _PLUGIN_PROBE_IDS:
                                continue
                            enabled = plugin_cfg.get("enabled", True)
                            channels.append({
                                "id": plugin_id, "name": f"Plugin: {plugin_id}",
                                "status": "ok" if enabled else "warning",
                                "auth_state": "ok" if enabled else "missing",
                                "details": "enabled" if enabled else "disabled",
                                "source": "plugins.entries", "last_message": None, "days_since_message": None,
                            })
                            seen_ids.add(plugin_id)
                except (json.JSONDecodeError, Exception):
                    pass

            # ── 5. auth-profiles.json: API key presence per provider ─────────────
            # Direct read (sudoers gives evolve /bin/cat grant for auth-profiles).
            api_keys: list[dict] = []
            auth_path = paths["auth_profiles"]
            try:
                _auth_text = None
                try:
                    _auth_text = Path(auth_path).read_text()
                except PermissionError:
                    _rp = _subproc.run(
                        ["sudo", "/bin/cat", auth_path],
                        capture_output=True, text=True, timeout=5
                    )
                    if _rp.returncode == 0:
                        _auth_text = _rp.stdout
                proc_ok = bool(_auth_text and _auth_text.strip())
                if proc_ok:
                    profiles: dict = json.loads(_auth_text).get("profiles", {})
                    seen_providers: set = set()
                    for pname, pdata in profiles.items():
                        provider = pdata.get("provider", pname)
                        ptype = pdata.get("type", "api_key")
                        has_value = bool(
                            pdata.get("api_key") or pdata.get("token") or
                            pdata.get("value") or pdata.get("key")
                        )
                        key = f"{provider}:{ptype}"
                        if key in seen_providers:
                            continue
                        seen_providers.add(key)
                        api_keys.append({
                            "id": key,
                            "name": f"{provider.title()} ({ptype.replace('_', ' ')})",
                            "provider": provider,
                            "type": ptype,
                            "status": "ok" if has_value else "error",
                            "auth_state": "ok" if has_value else "missing",
                            "details": "key present" if has_value else "no value set — key slot exists but is empty",
                            "source": "auth-profiles.json",
                            "last_checked": None,
                        })
                    # Surface any known providers that are NOT in profiles (not configured at all)
                    configured_providers = {p.get("provider") for p in profiles.values()}
                    for provider in _KNOWN_PROVIDERS:
                        if provider not in configured_providers:
                            api_keys.append({
                                "id": f"{provider}:not_configured",
                                "name": f"{provider.title()} (not configured)",
                                "provider": provider,
                                "type": "api_key",
                                "status": "warning",
                                "auth_state": "missing",
                                "details": "not found in auth-profiles.json",
                                "source": "auth-profiles.json",
                                "last_checked": None,
                            })
            except Exception:
                pass  # auth-profiles not readable — surface what we can

            ch_ok  = sum(1 for c in channels if c["status"] == "ok")
            ch_warn = sum(1 for c in channels if c["status"] == "warning")
            ch_err  = sum(1 for c in channels if c["status"] == "error")
            ak_ok   = sum(1 for k in api_keys  if k["status"] == "ok")
            ak_err  = sum(1 for k in api_keys  if k["status"] == "error")
            # ak "warning" = provider not configured at all (expected in single-provider deployments)
            # ak "error"   = slot exists but key value is empty — that's a real problem
            overall = (
                "error"   if (ch_err > 0 or ak_err > 0) else
                "warning" if ch_warn > 0 else
                "ok"
            )
            return {
                "channels": channels, "api_keys": api_keys,
                "summary": {
                    "channels_ok": ch_ok, "channels_warn": ch_warn, "channels_error": ch_err,
                    "api_keys_ok": ak_ok, "overall": overall,
                },
            }
        except Exception as e:
            return {"error": str(e)}

    def _emit_integration_signals(results: dict) -> None:
        """Mirror probe outcomes into the Signal store.

        Phase 5 of internal/spec-alerts-signal-store-2026-05-07.md. One
        Signal per (bot_id, integration_id, status_kind) — sweep at end
        clears any signal whose probe came back ok this run. Best-effort.
        """
        try:
            signals_store = _module._import_analyzer("signals.store")
            from schema.signal import make_signature
        except Exception:
            return

        kept: set[str] = set()
        for bot_id, payload in results.items():
            if not isinstance(payload, dict) or "error" in payload:
                continue
            for entry in (payload.get("channels") or []):
                _emit_one_integration_signal(
                    signals_store, make_signature, kept,
                    bot_id, entry, kind="channel",
                )
            for entry in (payload.get("api_keys") or []):
                _emit_one_integration_signal(
                    signals_store, make_signature, kept,
                    bot_id, entry, kind="api_key",
                )

        try:
            signals_store.sweep_resolve(
                _shared_dir(), producer="integration_probe",
                kept_signatures=kept,
                reason="auto-resolve: probe returned ok",
            )
        except Exception:
            pass

    def _emit_one_integration_signal(
        signals_store, make_signature, kept: set[str],
        bot_id: str, entry: dict, *, kind: str,
    ) -> None:
        """Map one channel/api_key probe outcome to a Signal.

        Only ``status == "error"`` emits a Signal. ``warning`` means the
        integration is configured but disabled (or the provider is in
        the UI's keys-modal scaffolding without a stored value) — those
        are deliberate configuration choices, not unusual conditions
        worth raising on the Alerts page. Disabled channels in
        particular are common (operator picks one messaging channel per
        bot; the others stay off); flagging them as "activity" was
        adding noise that trained the operator to ignore the feed.

        If you want to see configuration-state details, the Integrations
        page shows them with no Alerts-page footprint.
        """
        status = entry.get("status")
        if status != "error":
            return
        integration_id = entry.get("id") or entry.get("name") or "unknown"
        sig_type = "channel_down" if kind == "channel" else "api_key_missing"
        flavor = "maintenance"
        severity = "alert"
        signature = make_signature(
            "integration_probe", sig_type, f"{bot_id}:{integration_id}",
        )
        kept.add(signature)
        try:
            signals_store.observe(
                _shared_dir(),
                signature=signature,
                producer="integration_probe",
                type=sig_type,
                flavor=flavor,
                severity=severity,
                scope="integration",
                bot_id=bot_id,
                title=f"{entry.get('name') or integration_id} on {bot_id}",
                body=entry.get("details") or "",
                details={
                    "bot_id": bot_id,
                    "integration_id": integration_id,
                    "kind": kind,
                    "auth_state": entry.get("auth_state"),
                    "source": entry.get("source"),
                    "status": status,
                    # Severity framework: operations vector. Both
                    # channel_down + api_key_missing represent a single-
                    # bot integration that's broken (status=error always
                    # at this point). Magnitude 2 — real degradation
                    # but bot's other surfaces still function.
                    "vector": "operations",
                    "magnitude": 2,
                    "severity_active": True,
                },
            )
        except Exception:
            return

    @app.get("/api/integrations")
    def api_integrations() -> Response:
        """Per-bot integration health: channels + API key providers."""
        bots = _all_bots()
        results: dict = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_scan_bot_integrations, bid): bid for bid in bots}
            for fut in as_completed(futs, timeout=25):
                bid = futs[fut]
                try:
                    results[bid] = fut.result(timeout=20)
                except Exception as e:
                    results[bid] = {"error": str(e)}
        for bid in bots:
            if bid not in results:
                results[bid] = {"error": "timeout"}
        try:
            _emit_integration_signals(results)
        except Exception:
            pass
        return jsonify(results)

    @app.post("/api/integrations/scan")
    def api_integrations_scan() -> Response:
        """Force a fresh integration scan — re-reads openclaw.json, heal status, and auth-profiles for all bots."""
        bots = _all_bots()
        results: dict = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_scan_bot_integrations, bid): bid for bid in bots}
            for fut in as_completed(futs, timeout=30):
                bid = futs[fut]
                try:
                    results[bid] = fut.result(timeout=25)
                except Exception as exc:
                    results[bid] = {"error": str(exc), "channels": [], "api_keys": []}
        for bid in bots:
            if bid not in results:
                results[bid] = {"error": "timeout", "channels": [], "api_keys": []}
        try:
            _emit_integration_signals(results)
        except Exception:
            pass
        return jsonify({"ok": True, "scanned_at": _now_iso(), "bots": list(results.keys()), "data": results})

    @app.post("/api/integrations/test")
    def api_integrations_test() -> Response:
        """Test a specific integration. body: {botId, channelId}"""
        return jsonify({"status": "not_implemented", "message": "Connection testing coming soon"})

    # ── POST /api/signals/refresh ────────────────────────────────────────────
    # Re-runs the live monitors that emit Signals to the Alerts page so that
    # cleared conditions auto-archive (via each producer's sweep_resolve) and
    # newly-fired ones land before the UI re-reads /api/signals. The Refresh
    # buttons on the Alerts page wire here. Lives in this registrar (rather
    # than _register_signals_routes) because the integration_probe helpers
    # are closure-scoped here and we want one place that fans out.
    #
    # Scope: the five producers wired into the admin UI as live observers —
    # integration_probe, host_health, error_reporter, pod_health,
    # cron_exit_monitor. Scheduled
    # producers (audit, pod_report, security_warden, generator_runner) are
    # intentionally NOT triggered here; they run on their own cadence and
    # have side effects that aren't appropriate for an ad-hoc button.

    @app.post("/api/signals/refresh")
    def api_signals_refresh() -> Response:
        """Re-run live monitors and re-sweep their Signal-store contributions.

        Fans out to integration_probe, host_health, error_reporter,
        pod_health in parallel with a 10-second wall-clock budget.
        Returns per-monitor outcomes so the UI can surface partial
        success. Best-effort — never 5xxs from a probe failure.

        Response shape:
          {
            "ok": True,
            "ran_at": "...iso...",
            "monitors": [
              {"name": "integration_probe", "ok": True,  "ms": 4823},
              {"name": "host_health",       "ok": True,  "ms": 12},
              {"name": "error_reporter",    "ok": True,  "ms": 38},
              {"name": "pod_health",        "ok": False, "ms": 10000, "error": "timeout"},
              {"name": "cron_exit_monitor", "ok": True,  "ms": 95},
            ],
          }
        """
        import time as _time
        budget_seconds = 10.0

        def _run_integration_probe() -> None:
            bots = _all_bots()
            results: dict = {}
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(_scan_bot_integrations, bid): bid for bid in bots}
                for fut in as_completed(futs, timeout=budget_seconds - 1):
                    bid = futs[fut]
                    try:
                        results[bid] = fut.result(timeout=2)
                    except Exception as exc:
                        results[bid] = {"error": str(exc), "channels": [], "api_keys": []}
            for bid in bots:
                results.setdefault(bid, {"error": "timeout", "channels": [], "api_keys": []})
            _emit_integration_signals(results)

        def _run_host_health() -> None:
            from ..host_health import collect_host_health, emit_signals_from_snapshot
            snapshot = collect_host_health()
            emit_signals_from_snapshot(snapshot, _shared_dir())

        def _run_error_reporter() -> None:
            from .. import reporter as _reporter
            _reporter.emit_error_signals(_shared_dir())

        def _run_pod_health() -> None:
            from ..health import run_health_check
            # run_health_check itself emits + sweeps via _emit_health_signals.
            run_health_check(network_path=network_path)

        def _run_cron_exit_monitor() -> None:
            from ..cron_exit_monitor import emit_cron_exit_signals
            # Scheduled home is the hourly audit-scheduler tick; the
            # Refresh button re-probes so a fixed cron clears immediately.
            emit_cron_exit_signals(_shared_dir(), load_network(network_path))

        monitors = [
            ("integration_probe", _run_integration_probe),
            ("host_health", _run_host_health),
            ("error_reporter", _run_error_reporter),
            ("pod_health", _run_pod_health),
            ("cron_exit_monitor", _run_cron_exit_monitor),
        ]

        outcomes: list[dict] = []
        # NOTE: don't use `with ThreadPoolExecutor(...)` — its __exit__ calls
        # shutdown(wait=True), which blocks until still-running probes finish
        # (Python's threads can't be cancelled). That defeats the budget.
        # We shutdown(wait=False, cancel_futures=True) so unstarted tasks are
        # cancelled and running ones become orphan daemons that finish on
        # their own. The response returns within budget either way.
        ex = ThreadPoolExecutor(max_workers=len(monitors))
        try:
            started = {ex.submit(fn): (name, _time.monotonic()) for name, fn in monitors}
            try:
                for fut in as_completed(started, timeout=budget_seconds):
                    name, t0 = started[fut]
                    elapsed_ms = int((_time.monotonic() - t0) * 1000)
                    try:
                        fut.result(timeout=0.1)
                        outcomes.append({"name": name, "ok": True, "ms": elapsed_ms})
                    except Exception as exc:
                        outcomes.append({"name": name, "ok": False, "ms": elapsed_ms, "error": str(exc)[:200]})
            except FutTimeoutError:
                pass
            # Any monitor we never collected a result for hit the wall-clock budget.
            done_names = {o["name"] for o in outcomes}
            for fut, (name, t0) in started.items():
                if name in done_names:
                    continue
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                outcomes.append({"name": name, "ok": False, "ms": elapsed_ms, "error": "timeout"})
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        # Stable order for the UI.
        order = {n: i for i, (n, _) in enumerate(monitors)}
        outcomes.sort(key=lambda o: order.get(o["name"], 99))

        return jsonify({
            "ok": all(o["ok"] for o in outcomes),
            "ran_at": _now_iso(),
            "budget_seconds": budget_seconds,
            "monitors": outcomes,
        })

