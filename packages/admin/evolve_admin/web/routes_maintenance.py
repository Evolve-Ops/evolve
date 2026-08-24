"""HTTP routes for the Maintenance surface.

/api/launchd/jobs               list all Evolve/OpenClaw launchd jobs
/api/oc/version                 installed vs npm-latest OpenClaw version
/api/oc/safe-upgrade/check      spawn background preflight check
/api/oc/safe-upgrade/report/*   fetch preflight report
/api/oc/upgrade/apply           apply a passing preflight as a background job
/api/gateway/logs/<bot_id>      last N lines of a bot's gateway log
/api/debug/openclaw-json/<user> read openclaw.json for a user (debug)

Extracted from server.py (Batch E, Step 5).
"""

from __future__ import annotations

import json
import subprocess
import time as _time
from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import DEFAULT_SHARED_DIR, load_network
from ..runtime import LaunchdScheduler, get_launchd_scheduler
from ..telemetry import get_logger
from .server import resolve_bot_paths

_log = get_logger("web.routes_maintenance")

# Scheduler-seam probe handle (S2 migration). /api/launchd/jobs reads the
# pod's launchd job table sudo-less (the historical argv — the admin
# daemon's launchd session already sees the system domain) with the
# historical 10s timeout, hence a dedicated adapter instance rather than
# get_scheduler()'s sudo-by-default singleton. raw() is used because
# neither probe has a verb equivalent (the tri-state PID/last-exit columns
# from one `launchctl list`, and the per-label disabled registry from
# `print-disabled system`, are both launchd-verbatim — see the call-site
# comments).
#
# Portability (bite 3): these raw() probes are launchd-only — there is no
# systemd job-visibility verb on the Scheduler Protocol yet (a GA follow-up,
# not this bite). On a Linux pod the whole route platform-gates OUT and
# returns an empty job list, so the Infra-Jobs admin page shows nothing-to-
# show rather than a 500 or false-clean data. On macOS the probe routes
# through get_launchd_scheduler (the FAIL-FAST launchd-typed accessor): it
# raises loudly if a non-launchd adapter is somehow active, which the route's
# platform gate makes impossible. Tests monkeypatch ``_probe_scheduler`` with
# a fake-runner LaunchdScheduler — set, that instance is reused verbatim.
_probe_scheduler: LaunchdScheduler | None = None


def _launchctl_probe(*args: str) -> tuple[int, str, str]:
    """Run a read-only launchctl probe via the Scheduler seam.

    argv shape is unchanged from the pre-seam code:
    ``/bin/launchctl <args…>`` (no sudo) with a 10s timeout; exceptions
    surface as ``(1, "", str(e))`` (the seam runner's contract).

    A test-injected ``_probe_scheduler`` is reused as-is. Otherwise the
    handle is derived from the FAIL-FAST launchd-typed seam accessor
    (raises on a non-launchd adapter — the /api/launchd/jobs route gates
    this whole path to macOS, so that never fires in production). When the
    accessor returns a test fake (custom runner), reuse its runner so the
    injected fake still intercepts; otherwise build a fresh unsudo'd 10s
    adapter with the byte-identical historical argv.
    """
    global _probe_scheduler
    if _probe_scheduler is None:
        base = get_launchd_scheduler()
        if base._custom_runner:
            _probe_scheduler = LaunchdScheduler(
                use_sudo=False, runner=base._runner, timeout=10.0,
            )
        else:
            _probe_scheduler = LaunchdScheduler(use_sudo=False, timeout=10.0)
    return _probe_scheduler.raw(*args)


# StepEvent levels → background-job log levels. Identity today; the map is the
# seam so a new event level can't silently render as an unstyled line.
_JOB_LEVELS = {
    "info": "info",
    "success": "success",
    "warning": "warning",
    "error": "error",
}

_NPM_LATEST_URL = "https://registry.npmjs.org/openclaw/latest"


def _npm_latest_version(timeout: int = 8) -> tuple[str | None, str | None]:
    """(version, error) for openclaw's npm ``latest`` tag."""
    import urllib.request as _ureq

    try:
        with _ureq.urlopen(_NPM_LATEST_URL, timeout=timeout) as resp:
            return json.loads(resp.read()).get("version"), None
    except Exception as e:
        return None, str(e)


def _report_stale_reason(report: dict) -> str | None:
    """Why *report* no longer describes the pod, or None if it still does.

    The same ``stale`` condition ``/api/oc/version`` computes for the banner —
    installed version moved, or npm's ``latest`` moved — phrased for an
    operator. Fail-closed: an unreachable registry means we cannot PROVE the
    gates still describe reality, so that reads as stale too. The root helper
    re-derives all of this as root (spec §4.2 step 2); this copy exists so the
    UI gets a legible 409 instead of a job that starts and immediately dies.
    """
    from .. import upstream_version as _uv

    recorded_installed = (report.get("current") or {}).get("installed_version")
    recorded_target = (report.get("candidate") or {}).get("resolved_version")
    if not recorded_target:
        return "it never resolved a candidate version"

    live_installed = _uv.installed_package_version()
    if live_installed and not _uv.same_version(live_installed, recorded_installed):
        return f"OpenClaw is now {live_installed}, not {recorded_installed}"

    latest, err = _npm_latest_version()
    if err or not latest:
        return f"the npm registry could not be re-checked ({err or 'no version'})"
    if not _uv.same_version(latest, recorded_target):
        return f"npm latest is now {latest}, not {recorded_target}"
    return None


def _register_maintenance_routes(app: Flask, network_path: Path) -> None:
    """Register Evolve Infrastructure Jobs, OC Version, and Gateway Log Viewer endpoints."""

    @app.get("/api/launchd/jobs")
    def api_launchd_jobs() -> Response:
        """List all Evolve/OpenClaw launchd jobs from /Library/LaunchDaemons."""
        import glob as _glob
        import plistlib

        # Launchd-only surface: the /Library/LaunchDaemons plist glob and the
        # raw launchctl probes below have no systemd analogue yet (a job-
        # visibility verb on the Scheduler Protocol is a GA follow-up, not
        # this bite). On a Linux pod, gate OUT and return an empty list so
        # the Infra-Jobs page renders "nothing to show" rather than 500ing on
        # a missing launchctl or surfacing false-clean data.
        from platform_profile import get_profile

        if get_profile().name != "macos":
            return jsonify({"jobs": [], "platform": get_profile().name})

        # Build launchctl status index
        launchctl_by_label: dict = {}
        try:
            # raw("list"): this route needs the PID and last-exit columns for
            # every label from ONE launchctl invocation; Scheduler.list()
            # returns labels only and status() would be a subprocess per label.
            _rc, out, _err = _launchctl_probe("list")
            for line in out.splitlines()[1:]:  # skip header
                parts = line.split("\t")
                if len(parts) >= 3:
                    pid_str, last_exit_str, label = parts[0], parts[1], parts[2]
                    launchctl_by_label[label] = {
                        "pid": None if pid_str == "-" else pid_str,
                        "last_exit": (
                            None if last_exit_str == "-"
                            else int(last_exit_str) if last_exit_str.lstrip("-").isdigit()
                            else last_exit_str
                        ),
                    }
        except Exception:
            pass

        # Build disabled-label set from launchctl print-disabled system
        # Output lines look like:  \t"ai.evolve.foo" => disabled
        disabled_labels: set = set()
        try:
            # raw("print-disabled"): no Scheduler verb exposes the disabled-
            # label registry (status()/list() report loaded state, not the
            # per-label disable flag).
            _rc, out, _err = _launchctl_probe("print-disabled", "system")
            for line in out.splitlines():
                line = line.strip()
                if "=>" not in line:
                    continue
                label_part, state_part = line.split("=>", 1)
                if state_part.strip() == "disabled":
                    disabled_labels.add(label_part.strip().strip('"'))
        except Exception:
            pass

        jobs = []
        seen: set = set()
        for pattern in [
            "/Library/LaunchDaemons/ai.evolve.*.plist",
            "/Library/LaunchDaemons/ai.openclaw.*.plist",
        ]:
            for plist_path in sorted(_glob.glob(pattern)):
                if plist_path in seen:
                    continue
                seen.add(plist_path)
                try:
                    with open(plist_path, "rb") as f:
                        plist = plistlib.load(f)
                    label = plist.get("Label", Path(plist_path).stem)
                    interval = plist.get("StartInterval")
                    cal = plist.get("StartCalendarInterval")
                    if interval:
                        schedule = f"every {interval}s"
                    elif cal:
                        parts_list = [f"{k}={cal[k]}" for k in ("Minute", "Hour", "Day", "Weekday", "Month") if k in cal]
                        schedule = "calendar: " + (", ".join(parts_list) if parts_list else str(cal))
                    else:
                        schedule = "on-demand"
                    lc = launchctl_by_label.get(label, {})
                    jobs.append({
                        "label": label,
                        "plist": plist_path,
                        "schedule": schedule,
                        "pid": lc.get("pid"),
                        "last_exit": lc.get("last_exit"),
                        "loaded": label in launchctl_by_label,
                        "enabled": label not in disabled_labels,
                        "user": plist.get("UserName"),
                        "program_args": plist.get("ProgramArguments"),
                        "stdout": plist.get("StandardOutPath"),
                        "stderr": plist.get("StandardErrorPath"),
                    })
                except Exception as e:
                    jobs.append({
                        "label": Path(plist_path).stem,
                        "plist": plist_path,
                        "schedule": "?",
                        "error": str(e),
                        "loaded": False,
                        "enabled": False,
                        "pid": None,
                        "last_exit": None,
                    })
        return jsonify({"jobs": jobs})

    @app.get("/api/oc/version")
    def api_oc_version() -> Response:
        """OC version status: installed version vs npm latest.

        Pod-wide ``installed`` comes from the canonical npm package.json at
        ``/opt/homebrew/lib/node_modules/openclaw/package.json`` — the
        path every gateway plist is pinned to and the same source the
        ``evolve-admin oc upgrade`` CLI trusts. This is the authoritative
        answer to "what OC is installed on this host."

        Per-bot rows also report this same ``installed`` value (since all
        gateways load from the canonical path) plus a diagnostic
        ``last_touched`` field — the bot's ``openclaw.json::meta.lastTouchedVersion``,
        which lags behind reality (gateway boot doesn't update it; only a
        real session/turn does) and would otherwise mislead operators
        immediately after an upgrade-and-restart. The earlier
        ``npm root -g`` lookup was rejected for a different reason —
        Cellar shadow installs (see
        internal/spec-safe-upgrade-2026-05-02.md §6.4) — but reading the
        fixed canonical path sidesteps both failure modes.
        """
        import urllib.request as _ureq
        from .. import upstream_version as _uv

        latest_version: str | None = None
        fetch_error: str | None = None
        checked_at = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        try:
            req = _ureq.urlopen("https://registry.npmjs.org/openclaw/latest", timeout=8)
            latest_version = json.loads(req.read()).get("version")
        except Exception as e:
            fetch_error = str(e)

        installed_version: str | None = _uv.installed_package_version()
        # Compare through the shared normalizer, never with a bare `!=`: the
        # two operands come from different readers, and a normalization
        # difference between them reads as a permanent, un-actionable
        # "update available" on a host that is already current.
        update_available_pod = bool(
            installed_version
            and latest_version
            and not _uv.same_version(installed_version, latest_version)
        )

        network = load_network(network_path)
        bots = network.get("bots", {})
        members = network.get("members", list(bots.keys()))
        readings = _uv.per_bot_versions(
            list(members), network=network, allow_cli_fallback=False,
        )
        per_bot = {
            bot_id: {
                "installed": installed_version,
                "last_touched": (
                    readings[bot_id].version if bot_id in readings else None
                ),
                "latest": latest_version,
                "update_available": update_available_pod,
                "checked_at": checked_at,
                "fetch_error": fetch_error,
            }
            for bot_id in members
        }
        update_count = len(per_bot) if update_available_pod else 0

        # Embed the latest safety report so the banner has it on first load
        # without a second round-trip. See internal/spec-safe-upgrade-2026-05-02.md §6.4.
        from .. import safe_upgrade as _su
        shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
        safety_check: dict | None = None
        latest_report = _su.load_latest_report(shared_dir=shared_dir)
        if latest_report:
            r_inst = (latest_report.get("current") or {}).get("installed_version")
            r_resv = (latest_report.get("candidate") or {}).get("resolved_version")
            # Two distinct reasons the banner won't offer an apply, kept
            # distinct in the payload: the pod MOVED under the report
            # ("stale" — re-check and you're fine), or a version could not be
            # read at all ("unknown" — re-checking changes nothing, and a
            # bare "stale" pill sends the operator round that loop forever).
            # Both suppress the button; only one is worth re-running.
            stale_reason: str | None = None
            if not r_inst or not installed_version:
                stale_reason = "installed_unknown"
            elif not _uv.same_version(r_inst, installed_version):
                stale_reason = "installed_changed"
            elif not r_resv:
                stale_reason = "candidate_unknown"
            elif latest_version and not _uv.same_version(r_resv, latest_version):
                stale_reason = "candidate_changed"
            safety_check = {
                "report_id": latest_report.get("report_id"),
                "checked_at": latest_report.get("checked_at"),
                "ok": latest_report.get("ok"),
                "summary": latest_report.get("summary"),
                "stale": stale_reason is not None,
                "stale_reason": stale_reason,
            }

        running_id = _su.inflight_report_id()
        if running_id:
            safety_check = (safety_check or {}) | {
                "running_report_id": running_id,
                "running": True,
            }

        return jsonify({
            "bots": per_bot,
            "update_available": update_count > 0,
            "update_count": update_count,
            "installed": installed_version,
            "latest": latest_version,
            "safety_check": safety_check,
        })

    # ── Safe-upgrade preflight (spec-safe-upgrade-2026-05-02.md §6) ──────────

    @app.post("/api/oc/safe-upgrade/check")
    def api_oc_safe_upgrade_check() -> Response:
        """Spawn a background preflight check and return its report_id.

        Body (optional): {"target": "2026.4.15"}.  Default target is "latest".
        Concurrency: a second POST while a check is running returns the
        in-flight report_id with status='running'.
        """
        from .. import safe_upgrade as _su

        body = request.get_json(silent=True) or {}
        target_spec = (body.get("target") or "latest").strip() or "latest"
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
        rid, status = _su.start_background_check(target_spec, network=network, shared_dir=shared_dir)
        return jsonify({"report_id": rid, "status": status, "target": target_spec})

    @app.get("/api/oc/safe-upgrade/report/latest")
    def api_oc_safe_upgrade_latest() -> Response:
        """Return the most recent safety report, or 404 if none exists yet."""
        from .. import safe_upgrade as _su
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
        data = _su.load_latest_report(shared_dir=shared_dir)
        if data is None:
            return jsonify({"error": "no safety report found"}), 404
        return jsonify(data)

    @app.get("/api/oc/safe-upgrade/report/<report_id>")
    def api_oc_safe_upgrade_report(report_id: str) -> Response:
        """Return a specific safety report by id, or 404 if not found.

        Returns {"status": "running"} if the id matches an in-flight check
        whose JSON has not yet been written.
        """
        from .. import safe_upgrade as _su
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
        data = _su.load_report(report_id, shared_dir=shared_dir)
        if data is None:
            if _su.inflight_report_id() == report_id:
                return jsonify({"report_id": report_id, "status": "running"})
            return jsonify({"error": "no such report", "report_id": report_id}), 404
        return jsonify(data)

    # ── Apply the upgrade (spec-oc-upgrade-from-ui-2026-07-28.md §6) ─────────

    @app.post("/api/oc/upgrade/apply")
    def api_oc_upgrade_apply() -> "Response | tuple[Response, int]":
        """Run the OpenClaw upgrade behind a passing preflight, as a job.

        Body: ``{"reportId": "<id>", "confirm": true}``. The report id is the
        ONLY thing that crosses into the privileged half — the root helper
        re-validates it, re-runs the gates, and resolves the version to install
        from the persisted report, so no request-controlled string ever reaches
        npm (spec §4).

        Every refusal is a legible 409 (400 for a malformed request). There is
        deliberately no ``force``: overriding a red preflight stays a CLI-only
        capability with a human at a terminal (spec §9).
        """
        import threading as _threading

        from .. import deploy_resilience as _dres
        from .. import oc_upgrade_apply as _apply
        from .. import safe_upgrade as _su
        from .server import _active_job_id, _job_finish, _job_log, _job_progress, _new_job

        body = request.get_json(silent=True) or {}
        report_id = str(body.get("reportId") or "").strip()
        if not report_id:
            return jsonify({"error": "reportId is required"}), 400
        if not body.get("confirm"):
            return jsonify({"error": "confirm must be true"}), 400

        # Single-active-job guard — same registry /api/upgrade uses, so an
        # upgrade and a redeploy can't interleave in one process.
        if _active_job_id:
            return jsonify({
                "error": "Another job is already running",
                "jobId": _active_job_id[0],
            }), 409

        network = load_network(network_path)
        # Platform-keyed default (config.DEFAULT_SHARED_DIR → /var/lib/evolve on
        # Linux); the older routes above still carry the macOS literal.
        shared_dir = Path(network.get("sharedDir") or DEFAULT_SHARED_DIR)

        # Canary safety guard — inherited verbatim from /api/upgrade. A direct
        # upgrade races the gated release pipeline; the message is pinned by
        # existing unit + route tests, so it is returned unmodified.
        # Fail-open on resolution error, matching /api/upgrade.
        try:
            from ..release_manager import canary_upgrade_block as _canary_block
            blk = _canary_block(network)
            if blk:
                resp = jsonify(blk)
                resp.status_code = 409
                return resp
        except Exception as exc:
            _log.warning("oc upgrade canary-guard failed (allowing): %s", exc)

        if _su.inflight_report_id():
            return jsonify({
                "error": "A safety check is still running — wait for it to "
                         "finish, then try again",
            }), 409

        report = _su.load_report(report_id, shared_dir=shared_dir)
        if report is None:
            return jsonify({
                "error": f"No safety report {report_id} — run the check again",
            }), 409
        if not report.get("ok"):
            return jsonify({
                "error": "That safety report did not pass its gates. Resolve "
                         "the blockers it lists and run the check again.",
            }), 409

        stale_reason = _report_stale_reason(report)
        if stale_reason:
            return jsonify({
                "error": f"That safety report is out of date ({stale_reason}) "
                         f"— re-run the check.",
            }), 409

        # C1 deploy lock — refuse an upgrade racing the puller redeploy sweep.
        # NON-BLOCKING → synchronous, legible 409. Held across the run,
        # released in the worker's finally.
        deploy_lock = _dres.try_acquire_deploy_lock(shared_dir)
        if deploy_lock is None:
            return jsonify({
                "error": "A deploy is already in progress (scheduled redeploy) "
                         "— try again in a moment",
            }), 409

        job_id = _new_job("oc-upgrade")
        installed = (report.get("current") or {}).get("installed_version")
        target = (report.get("candidate") or {}).get("resolved_version")
        _job_log(job_id, f"Upgrading OpenClaw {installed} → {target}…")

        def _on_event(event: dict) -> None:
            message = str(event.get("message") or "").strip()
            if message:
                _job_log(job_id, message, _JOB_LEVELS.get(
                    str(event.get("level")), "info"))
            step, total = event.get("step"), event.get("total")
            if isinstance(step, int) and isinstance(total, int) and total:
                _job_progress(job_id, step, total,
                              str(event.get("label") or event.get("phase") or ""))

        def _run_apply() -> None:
            try:
                ok, detail = _apply.stream_privileged_upgrade(report_id, _on_event)
                if ok:
                    _job_finish(job_id, result={
                        "reportId": report_id, "from": installed, "to": target,
                    })
                else:
                    _job_log(job_id, detail, "error")
                    _job_finish(job_id, error=detail)
            except Exception as e:  # noqa: BLE001 — a job must always terminate
                _job_log(job_id, f"Upgrade crashed: {e}", "error")
                _job_finish(job_id, error=str(e))
            finally:
                _dres.release_deploy_lock(deploy_lock)

        t = _threading.Thread(target=_run_apply, daemon=True)
        try:
            t.start()
        except Exception:
            # The thread never ran → its finally won't; avoid a leaked lock.
            _dres.release_deploy_lock(deploy_lock)
            _job_finish(job_id, error="could not start the upgrade job")
            raise
        return jsonify({"jobId": job_id, "status": "started",
                        "reportId": report_id, "from": installed, "to": target}), 202

    # NOTE: /api/oc/update intentionally removed.
    # OpenClaw is system infrastructure installed via Homebrew npm (owned by the
    # admin user, not the evolve process). Updates must be run manually as the
    # admin user: sudo npm install -g openclaw
    # See docs/archive/specs/spec-sudoers.md §"What Evolve Should NEVER Be Granted" and
    # docs/local-deployment-architecture.md §"OpenClaw Updates".

    @app.get("/api/gateway/logs/<bot_id>")
    def api_gateway_logs(bot_id: str) -> Response:
        """Last N lines of a bot's gateway log.

        ?log=stdout  (default) — /Users/<bot>/.openclaw/logs/gateway.log
        ?log=err               — /Users/<bot>/.openclaw/logs/gateway.err
        ?log=oc                — /tmp/openclaw-<uid>/openclaw-<date>.log (OC internal log;
                                 contains plugin logger.info() output)
        ?lines=N               — return last N lines (default 80)
        """
        network = load_network(network_path)
        bots = network.get("bots", {})
        if bot_id not in bots:
            return jsonify({"error": "unknown bot"}), 404
        bot_cfg = bots[bot_id]
        user = bot_cfg.get("user", bot_id)
        paths = resolve_bot_paths(bot_id, user=user)
        log_type = request.args.get("log", "stdout")
        n_lines = int(request.args.get("lines", "80"))

        def _read_log(path: str) -> str:
            try:
                return Path(path).read_text()
            except (PermissionError, OSError):
                res = subprocess.run(
                    ["sudo", "/bin/cat", path],
                    capture_output=True, text=True, timeout=10,
                )
                return res.stdout if res.returncode == 0 or res.stdout else ""

        if log_type == "oc":
            # OC writes its internal log (plugin logger output) to
            # /tmp/openclaw-{uid}/openclaw-{date}.log.  Find the uid by looking
            # up the bot user, then glob for the most recent log file.
            import pwd, glob as _glob, datetime as _dt
            try:
                uid = pwd.getpwnam(user).pw_uid
            except KeyError:
                uid = None
            log_path = None
            if uid is not None:
                tmp_dir = f"/tmp/openclaw-{uid}"
                today = _dt.date.today().strftime("%Y-%m-%d")
                candidates = sorted(_glob.glob(f"{tmp_dir}/openclaw-*.log"), reverse=True)
                # Prefer today's log; fall back to most recent
                for c in candidates:
                    if today in c:
                        log_path = c
                        break
                if not log_path and candidates:
                    log_path = candidates[0]
            if not log_path:
                return jsonify({"bot_id": bot_id, "lines": [],
                                "log_path": f"/tmp/openclaw-{uid}/openclaw-*.log",
                                "error": "OC log not found"})
            try:
                text = _read_log(log_path)
                lines = text.splitlines()[-n_lines:] if text else []
                return jsonify({"bot_id": bot_id, "lines": lines, "log_path": log_path})
            except Exception as e:
                return jsonify({"bot_id": bot_id, "lines": [], "log_path": log_path, "error": str(e)})

        log_filename = "gateway.err" if log_type == "err" else "gateway.log"
        log_path = paths["logs_dir"] + "/" + log_filename

        try:
            text = _read_log(log_path)
            lines = text.splitlines()[-n_lines:] if text else []
            return jsonify({"bot_id": bot_id, "lines": lines, "log_path": log_path})
        except Exception as e:
            return jsonify({"bot_id": bot_id, "lines": [], "log_path": log_path, "error": str(e)})

    @app.get("/api/debug/openclaw-json/<user>")
    def api_debug_openclaw_json(user: str) -> Response:
        """Read /Users/{user}/.openclaw/openclaw.json for debugging port conflicts."""
        path = f"/Users/{user}/.openclaw/openclaw.json"
        try:
            text = Path(path).read_text()
        except (PermissionError, OSError):
            res = subprocess.run(["sudo", "/bin/cat", path], capture_output=True, text=True, timeout=10)
            text = res.stdout if res.returncode == 0 else None
        if not text:
            return jsonify({"user": user, "error": "not found or permission denied", "path": path}), 404
        try:
            data = json.loads(text)
            return jsonify({"user": user, "path": path, "port": data.get("port"), "botId": data.get("botId"), "data": data})
        except Exception as e:
            return jsonify({"user": user, "path": path, "raw": text[:500], "error": str(e)})
