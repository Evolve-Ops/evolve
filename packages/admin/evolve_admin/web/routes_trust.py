"""HTTP routes for the Trust and Heartbeat surfaces.

GET  /api/trust            evidence-based per-module trust status
GET  /api/defer/health     defer-runner health snapshot
GET  /api/heartbeat/check  silence-first should-alert check

Extracted from server.py (Batch E, Step 2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, Response

from ..config import load_network, bot_home as _bot_home
from ..runtime import LaunchdScheduler, get_scheduler
from ..telemetry import get_logger

_log = get_logger("web.routes_trust")

# Scheduler-seam probe handle (S2 migration). The defer-runner presence
# check calls the ``status()`` Protocol verb — portable across launchd and
# systemd — with a 5s timeout. On macOS we keep the historical sudo-less
# argv (``/bin/launchctl list <label>``, no ``sudo`` prefix); on a Linux pod
# the process-wide ``get_scheduler()`` singleton is a SystemdScheduler, so
# ``status()`` answers truthfully via ``systemctl show`` instead of a launchd
# call that would crash → false ``runner_loaded``.
#
# Posture decision (bite 3): the no-sudo macOS argv is the historical posture
# but NOT load-bearing for correctness — ``launchctl list <system-label>``
# succeeds sudo-or-not from the admin daemon's system-domain launchd session,
# and a ``list *`` sudoers grant exists anyway. To keep macOS behavior
# byte-identical (the argv-parity test pins no-sudo) WITHOUT a launchd-typed
# bypass on Linux, we derive an unsudo'd LaunchdScheduler only when the active
# adapter is launchd, and otherwise call the injected adapter directly. Same
# guarded-derive shape as retire._probe_scheduler / service._user_scheduler.
# Tests monkeypatch this with LaunchdScheduler(runner=<fake>, …).
_probe_scheduler: LaunchdScheduler | None = None


def _get_probe_scheduler():
    """Return the adapter the defer-runner ``status()`` probe runs on.

    macOS: an unsudo'd 5s LaunchdScheduler (the historical posture), derived
    from the injected launchd singleton's runner so a test-injected fake still
    intercepts. Non-launchd (Linux/systemd): the injected adapter itself, so
    ``status()`` routes through SystemdScheduler and is truthful rather than
    crashing on a missing ``launchctl``.

    A test that monkeypatches ``_probe_scheduler`` with a concrete adapter
    pins that instance (the historical seam-injection point is preserved).
    """
    global _probe_scheduler
    if _probe_scheduler is not None:
        return _probe_scheduler
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        # Non-launchd adapter (systemd on a Linux pod): status() is portable,
        # so probe the injected adapter directly. No no-sudo derivation —
        # sudo posture is a launchd argv concept the systemd adapter ignores.
        return sched
    if sched._custom_runner:
        # Test path: reuse the injected fake runner so no real launchctl is
        # reached; the unsudo'd posture is irrelevant to the recording fake.
        _probe_scheduler = LaunchdScheduler(
            use_sudo=False, runner=sched._runner, timeout=5.0,
        )
    else:
        # Production macOS: fresh unsudo'd 5s adapter (byte-identical argv).
        _probe_scheduler = LaunchdScheduler(use_sudo=False, timeout=5.0)
    return _probe_scheduler


def _register_trust_routes(app: Flask, network_path: Path) -> None:
    """Register /api/trust and /api/heartbeat/check — evidence-based trust + silence-first heartbeats."""
    import datetime as _dt
    import time as _ts

    def _shared() -> Path:
        cfg = load_network(network_path)
        return Path(cfg.get("sharedDir", "/Users/Shared/evolve"))

    @app.get("/api/trust")
    def api_trust() -> Response:
        """
        Per-module validation status derived from actual evidence.
        'Deployed' != 'Working.' This shows what operators can actually trust.
        """
        network = load_network(network_path)
        shared = _shared()

        today = _dt.date.today()
        cutoff_7d = today - _dt.timedelta(days=7)
        now_epoch = _ts.time()

        try:
            from evolve_config import get_modules
            modules_cfg = get_modules(network)
        except Exception:
            modules_cfg = {}

        bots_cfg = network.get("bots", {})
        result: dict = {}

        # ── heal ──────────────────────────────────────────────────────────
        # Trusted if heal.py has been running recently (status files updated)
        heal_trusted = False
        heal_heals = 0
        heal_since: str | None = None

        for bot_id in bots_cfg:
            sf = shared / "status" / f"{bot_id}.json"
            if sf.exists():
                try:
                    data = json.loads(sf.read_text())
                    age_days = (now_epoch - data.get("ts_epoch", 0)) / 86400
                    if age_days <= 7:
                        heal_trusted = True
                        ts_str = data.get("ts", "")[:10]
                        if ts_str and (heal_since is None or ts_str < heal_since):
                            heal_since = ts_str
                except Exception:
                    pass

        incidents_dir = shared / "incidents"
        earliest_incident: str | None = None
        if incidents_dir.exists():
            for date_dir in sorted(incidents_dir.iterdir()):
                if not date_dir.is_dir():
                    continue
                if earliest_incident is None and any(date_dir.glob("*.json")):
                    earliest_incident = date_dir.name
                if date_dir.name >= str(cutoff_7d):
                    heal_heals += sum(1 for _ in date_dir.glob("*-restart_succeeded.json"))

        since_label = ""
        ref_date = earliest_incident or heal_since
        if ref_date:
            try:
                since_label = f"Running since {_dt.date.fromisoformat(ref_date).strftime('%b %d')}, "
            except ValueError:
                pass

        result["heal"] = {
            "name": "Gateway Healing",
            "status": "active",
            "validated": heal_trusted,
            "evidence": (f"{since_label}{heal_heals} successful heal(s) in last 7 days"
                         if heal_trusted else "Heal monitor not recently active"),
            "evidence_date": today.isoformat() if heal_trusted else None,
            "trust_level": "high" if heal_trusted else "unknown",
            "health": "Healthy" if heal_trusted else "Pending",
            "next_step": None if heal_trusted else "Awaiting first heal cycle (runs every 5 min)",
        }

        # ── spend_alerts ──────────────────────────────────────────────────
        alerts_cfg = network.get("alerts", {})
        threshold = float(alerts_cfg.get("spendThresholdUSD", 0))
        spend_trusted = threshold > 0
        spend_evidence_date: str | None = None

        if spend_trusted:
            alerts_dir = shared / "alerts"
            earliest_flag: str | None = None
            if alerts_dir.exists():
                for fp in sorted(alerts_dir.glob("spend-*.flag")):
                    # filename: spend-{bot_id}-{YYYY-MM-DD}.flag
                    parts = fp.stem.split("-")  # ['spend', bot, YYYY, MM, DD]
                    if len(parts) >= 5:
                        try:
                            date_str = "-".join(parts[-3:])
                            _dt.date.fromisoformat(date_str)
                            if earliest_flag is None or date_str < earliest_flag:
                                earliest_flag = date_str
                        except (ValueError, IndexError):
                            pass
            if earliest_flag:
                try:
                    d = _dt.date.fromisoformat(earliest_flag)
                    spend_evidence = f"Installed {d.strftime('%b %d')}, threshold ${threshold:.0f}/day"
                    spend_evidence_date = earliest_flag
                except ValueError:
                    spend_evidence = f"Configured, threshold ${threshold:.0f}/day"
                    spend_evidence_date = today.isoformat()
            else:
                spend_evidence = f"Configured, threshold ${threshold:.0f}/day"
                spend_evidence_date = today.isoformat()
        else:
            spend_evidence = "No spend threshold configured"

        result["spend_alerts"] = {
            "name": "Spend Alerts",
            "status": "active" if spend_trusted else "disabled",
            "validated": spend_trusted,
            "evidence": spend_evidence,
            "evidence_date": spend_evidence_date,
            "trust_level": "high" if spend_trusted else "unknown",
            "health": "Healthy" if spend_trusted else "Pending",
            "next_step": None if spend_trusted else "Set spendThresholdUSD in network.json alerts config",
        }

        # ── model_routing ─────────────────────────────────────────────────
        tier3_found = False
        tier_dir = shared / "cost" / "tier-usage"
        if tier_dir.exists():
            cutoff_str = str(cutoff_7d)
            outer: bool = False
            for bot_dir in tier_dir.iterdir():
                if not bot_dir.is_dir() or outer:
                    break
                for jf in bot_dir.glob("*.jsonl"):
                    if jf.stem[:10] < cutoff_str:
                        continue
                    try:
                        for line in jf.read_text().splitlines():
                            line = line.strip()
                            if line and json.loads(line).get("tier") == "tier3":
                                tier3_found = True
                                outer = True
                                break
                    except Exception:
                        pass
                    if outer:
                        break

        result["model_routing"] = {
            "name": "Model Routing",
            "status": "active",
            "validated": tier3_found,
            "evidence": ("Tier3 routing confirmed in annotation data" if tier3_found
                         else "Deployed, awaiting first measurement"),
            "evidence_date": today.isoformat() if tier3_found else None,
            "trust_level": "high" if tier3_found else "unknown",
            "health": "Healthy" if tier3_found else "Pending",
            "next_step": None if tier3_found else "Awaiting first classifier audit — the audit job runs weekly on Saturdays",
        }

        # ── detectors ────────────────────────────────────────────────────
        proposals_exist = False
        for stage in ("pending", "approved", "deployed", "rejected"):
            stage_dir = shared / "proposals" / stage
            if stage_dir.exists() and any(stage_dir.glob("*.json")):
                proposals_exist = True
                break

        analysis_cfg = modules_cfg.get("analysis", {})
        n_detectors = sum(
            1 for v in analysis_cfg.get("detectors", {}).values()
            if v.get("enabled", True)
        )

        result["detectors"] = {
            "name": f"Pattern Detectors ({n_detectors})",
            "status": "active",
            "validated": proposals_exist,
            "evidence": ("Analysis run confirmed, proposals generated" if proposals_exist
                         else "Awaiting first analysis run — runs weekly on Sundays"),
            "evidence_date": today.isoformat() if proposals_exist else None,
            "trust_level": "medium" if proposals_exist else "unknown",
            "health": "Healthy" if proposals_exist else "Pending",
            "next_step": None if proposals_exist else "Awaiting first analysis run — runs weekly on Sundays",
        }

        # ── continuity_engine ─────────────────────────────────────────────
        ce_enabled = bool(modules_cfg.get("continuity_engine", {}).get("enabled", False))
        result["continuity_engine"] = {
            "name": "Continuity Engine",
            "status": "active" if ce_enabled else "disabled",
            "validated": ce_enabled,
            "evidence": ("Enabled — continuity engine running" if ce_enabled
                         else "Disabled — enable after classifier validated"),
            "evidence_date": None,
            "trust_level": "high" if ce_enabled else "unknown",
            "health": "Healthy" if ce_enabled else "Pending",
            "next_step": None if ce_enabled else "Enable in network.json after session classifier is validated",
        }

        healthy = sum(1 for m in result.values() if m["health"] == "Healthy")
        failing = sum(1 for m in result.values() if m["health"] == "Failing")
        pending = len(result) - healthy - failing
        # backward-compat aliases
        trusted = healthy
        unknown = pending

        return jsonify({
            "modules": result,
            "summary": {
                "healthy": healthy, "pending": pending, "failing": failing,
                "trusted": trusted, "unknown": unknown,  # backward compat
                "total": len(result),
            },
        })

    @app.get("/api/defer/health")
    def api_defer_health() -> Response:
        """Defer-runner health snapshot for the Maintenance tab.

        Aggregates per-bot defer-archive.jsonl files (rows the runner has
        fired or failed) plus a launchd presence check on the runner
        daemon. Returns three numbers + a freshness timestamp; the
        operator decides if anything looks off. No live activity stream
        — that lives in the runner log.

        Returns:
          {
            "runner_loaded": bool,        # launchctl list … succeeded
            "last_fired_iso": str | None, # most-recent fired_at across all bots
            "minutes_since_last_fire": float | None,
            "fired_today": int,           # rows with fired_at on today's UTC date
            "failed_today": int,
            "rows_fired_total": int,      # lifetime, all bots
            "rows_failed_total": int,
          }
        """
        from datetime import datetime, timezone as _tz
        network = load_network(network_path)
        bots = list((network.get("bots") or {}).keys())
        if not bots:
            bots = list(network.get("members", []))

        # 1. Walk per-bot defer-archive.jsonl files. They live in the bot's
        #    home (under the read+write ACL set by deploy.set_evolve_read_acl);
        #    fall back to the shared dir for tests / partial setups.
        rows_fired = 0
        rows_failed = 0
        fired_today = 0
        failed_today = 0
        last_fired_iso: Optional[str] = None
        last_fired_dt: Optional[datetime] = None
        today_iso = datetime.now(_tz.utc).date().isoformat()
        for bot_id in bots:
            archive_path = _bot_home(bot_id, network) / ".openclaw" / "workspace" / "evolve" / "defer-archive.jsonl"
            if not archive_path.exists():
                continue
            try:
                for line in archive_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    status = row.get("status")
                    fired_at = row.get("fired_at")
                    if status == "fired":
                        rows_fired += 1
                        if fired_at and fired_at[:10] == today_iso:
                            fired_today += 1
                    elif status == "failed":
                        rows_failed += 1
                        if fired_at and fired_at[:10] == today_iso:
                            failed_today += 1
                    if fired_at:
                        try:
                            dt = datetime.fromisoformat(fired_at.replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=_tz.utc)
                            if last_fired_dt is None or dt > last_fired_dt:
                                last_fired_dt = dt
                                last_fired_iso = fired_at
                        except (ValueError, TypeError):
                            pass
            except (OSError, PermissionError):
                # Archive might not be readable from the admin server's
                # process context yet; ACL is best-effort.
                continue

        # 2. Is the runner daemon loaded? Cheap status() probe via the
        #    Scheduler seam — status()["managed"] is exactly "`launchctl
        #    list <label>` exited 0" on macOS (and LoadState=loaded on
        #    systemd); timeouts/spawn errors surface as managed=False inside
        #    the seam runner, i.e. not loaded, as before. The per-call
        #    timeout=5.0 keeps the systemd path bounded too (the launchd-
        #    derived adapter already carries a 5s constructor default).
        runner_loaded = bool(
            _get_probe_scheduler().status(
                "ai.openclaw.evolve.defer-runner", timeout=5.0,
            )["managed"]
        )

        minutes_since: Optional[float] = None
        if last_fired_dt is not None:
            now = datetime.now(_tz.utc)
            minutes_since = round((now - last_fired_dt).total_seconds() / 60.0, 1)

        return jsonify({
            "runner_loaded": runner_loaded,
            "last_fired_iso": last_fired_iso,
            "minutes_since_last_fire": minutes_since,
            "fired_today": fired_today,
            "failed_today": failed_today,
            "rows_fired_total": rows_fired,
            "rows_failed_total": rows_failed,
        })

    @app.get("/api/heartbeat/check")
    def api_heartbeat_check() -> Response:
        """
        Returns whether there's anything worth alerting about.
        Used by heartbeat handlers to decide whether to send a message.
        Silence is signal. Noise trains people to ignore alerts.
        """
        network = load_network(network_path)
        shared = _shared()
        today = _dt.date.today()
        now_epoch = _ts.time()
        reasons: list[str] = []
        checked_at = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        bots_cfg = network.get("bots", {})
        alerts_cfg = network.get("alerts", {})

        # 1. Bot gateway down (recent status file shows gateway not running)
        for bot_id, bot_info in bots_cfg.items():
            sf = shared / "status" / f"{bot_id}.json"
            if sf.exists():
                try:
                    data = json.loads(sf.read_text())
                    age_s = now_epoch - data.get("ts_epoch", 0)
                    if age_s < 1800 and data.get("gateway_running") is False:
                        reasons.append(f"{bot_id} gateway is down")
                except Exception:
                    pass

        # 2. Spend > 80% of daily threshold
        threshold = float(alerts_cfg.get("spendThresholdUSD", 0))
        if threshold > 0:
            today_str = today.isoformat()
            for bot_id in bots_cfg:
                mf = shared / "metrics" / today_str / f"{bot_id}.json"
                if mf.exists():
                    try:
                        data = json.loads(mf.read_text())
                        spend = float(data.get("total_cost_estimated", 0.0))
                        pct = spend / threshold
                        if pct >= 0.80:
                            reasons.append(
                                f"{bot_id} spend today: ${spend:.2f}"
                                f" (threshold: ${threshold:.2f}, {pct * 100:.0f}%)"
                            )
                    except Exception:
                        pass

        # 3. Cron job silent > cronSilenceThresholdDays
        silence_days = int(alerts_cfg.get("cronSilenceThresholdDays", 2))
        cron_cutoff = today - _dt.timedelta(days=silence_days)
        metrics_dir = shared / "metrics"
        if metrics_dir.exists():
            for bot_id, bot_info in bots_cfg.items():
                latest_metric: str | None = None
                try:
                    for day_dir in sorted(metrics_dir.iterdir(), reverse=True):
                        if day_dir.is_dir() and (day_dir / f"{bot_id}.json").exists():
                            latest_metric = day_dir.name
                            break
                except Exception:
                    pass
                if latest_metric is not None:
                    try:
                        if _dt.date.fromisoformat(latest_metric) < cron_cutoff:
                            reasons.append(
                                f"{bot_id} measure cron silent for >{silence_days} days"
                                f" (last: {latest_metric})"
                            )
                    except ValueError:
                        pass

        # 4. Proposals pending > 7 days
        # Sanctioned read path (spec-state-store-and-deploy-resilience
        # §1.1 Phase B): arbiter.store.iter_proposals, not a direct glob
        # of proposals/pending/.
        old_proposals = 0
        try:
            from arbiter import store as _arb_store  # type: ignore[import-not-found]
        except Exception:
            _arb_store = None  # type: ignore[assignment]
        if _arb_store is not None:
            cutoff_7d_str = str(today - _dt.timedelta(days=7))
            try:
                for prop in _arb_store.iter_proposals(shared, subdirs=("pending",)):
                    created = (getattr(prop, "created_at", "") or "")[:10]
                    if created and created < cutoff_7d_str:
                        old_proposals += 1
            except Exception:
                pass
        if old_proposals > 0:
            reasons.append(f"{old_proposals} proposal(s) pending for >7 days — needs review")

        return jsonify({
            "should_alert": len(reasons) > 0,
            "reasons": reasons,
            "checked_at": checked_at,
        })
