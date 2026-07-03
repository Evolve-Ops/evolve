#!/usr/bin/env python3
"""
cron_alert.py — Dead-man's switch for Evolve cron jobs.

Checks each bot's launchd cron jobs against network.json `alerts.watchedCrons`
patterns. If a watched job hasn't fired in `alerts.cronSilenceThresholdDays`
days, fires an alert via openclaw message send.

Alert deduplication: one flag file per job per calendar day prevents repeat
alerts for the same silence window.

Flag files: {shared_dir}/alerts/cron-{job_label}-{YYYY-MM-DD}.flag

Usage:
    python3 cron_alert.py                    # check all watched crons
    python3 cron_alert.py --dry-run          # report without alerting
    python3 cron_alert.py --network /path/to/network.json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from evolve_config import load_config, get_shared_dir, get_members, get_alerts, resolve_network_path
from runtime.agent_runtime import get_runtime

SCRIPT_VERSION = "0.1.0"


def _send_alert(
    *,
    shared_dir: Path,
    network: dict,
    bot_id: str,
    label: str,
    reason: str,
    dry_run: bool,
) -> None:
    """Route a stalled-cron alert through the alerts dispatcher.

    Phase F4: passes structured payload; catalog body_template renders
    "⚠️ Stalled cron / Bot: {bot_id}  Job: {label} / Reason: {reason}"
    plus the cli action (kickstart the watched job).
    """
    if dry_run:
        print(f"[cron_alert] DRY RUN — would alert: bot={bot_id} "
              f"label={label} reason={reason}")
        return

    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity,
        )
    except Exception as exc:
        print(f"[cron_alert] dispatcher import failed; alert dropped: {exc}",
              file=sys.stderr)
        return

    try:
        _dispatch_send(
            shared_dir=shared_dir,
            network=network,
            source="cron_alert",
            severity=Severity.WARNING,
            dedup_key=f"cron_alert/{label}",
            catalog_event="system.stalled_cron",
            payload={"bot_id": bot_id, "label": label, "reason": reason},
        )
    except Exception as exc:
        print(f"[cron_alert] dispatcher.send raised; alert dropped: {exc}",
              file=sys.stderr)


def _flag_path(shared_dir: Path, label: str, date_str: str) -> Path:
    """Return the dedup flag file path for a job+date."""
    # Sanitize label for use in filename
    safe_label = label.replace("/", "_").replace(" ", "_")
    return shared_dir / "alerts" / f"cron-{safe_label}-{date_str}.flag"


def _write_flag(path: Path) -> None:
    """Atomically write a flag file (creates parent dirs as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(datetime.now(timezone.utc).isoformat())
    tmp.replace(path)


def _delivery_monitored_keys(config: dict) -> set:
    """(bot_id, label-or-action-id) pairs the delivery monitor watches.

    §11 boundary of docs/spec-proactive-delivery-monitor-2026-06-10.md:
    a watched cron that maps to a monitored scheduled action is the
    delivery monitor's beat (window-level, minutes) — cron_alert skips
    it so one miss never double-pages the operator. Best-effort: if the
    monitored set can't be built, cron_alert keeps its full coverage
    (a dead-man switch must fail toward watching, not toward silence).
    """
    try:
        from delivery_monitor import Probes, build_monitored_set
        monitored, _coverage, _ws_errors = build_monitored_set(config, Probes())
    except Exception as exc:
        print(f"[cron_alert] delivery-monitor map unavailable ({exc}); "
              "skip guard off", file=sys.stderr)
        return set()
    keys = set()
    for a in monitored:
        keys.add((a.bot_id, a.action_id))
        if a.label:
            keys.add((a.bot_id, a.label))
    return keys


def check_cron_alerts(config: dict, dry_run: bool = False) -> int:
    """
    Check all bots' cron jobs against watchedCrons patterns.
    Returns count of alerts fired.
    """
    alerts_cfg = get_alerts(config)
    silence_days = float(alerts_cfg.get("cronSilenceThresholdDays", 2))
    watched_patterns = alerts_cfg.get("watchedCrons", [])
    shared_dir = Path(get_shared_dir(config))

    if not watched_patterns:
        print("[cron_alert] No watchedCrons patterns configured — nothing to check.")
        return 0

    members = get_members(config)
    now = datetime.now(timezone.utc)
    silence_td = timedelta(days=silence_days)
    today_str = now.strftime("%Y-%m-%d")
    alerts_fired = 0
    delivery_monitored = _delivery_monitored_keys(config)

    # Import read_jobs_state lazily (same cross-package pattern as _send_alert)
    try:
        from evolve_admin.applications.cron_manager import read_jobs_state as _read_jobs_state
    except Exception as _exc:
        print(f"[cron_alert] could not import read_jobs_state: {_exc}", file=sys.stderr)
        _read_jobs_state = None

    runtime = get_runtime()
    for bot_id in members:
        jobs_raw = runtime.cron_list(bot_id)
        if jobs_raw is None:
            print(f"[cron_alert] bot={bot_id}: OC CLI unavailable, skipping")
            continue

        # Run history is read directly from jobs-state.json (keyed by job UUID).
        # openclaw cron runs requires --id and has no --json flag; CLI calls don't work.
        jobs_state: dict = _read_jobs_state(bot_id) if _read_jobs_state else {}

        for job in (jobs_raw or []):
            jid = job.get("id") or job.get("job_id") or job.get("name") or ""
            label = job.get("label") or job.get("name") or jid

            # Only check jobs matching a watched pattern
            if not any(fnmatch.fnmatch(label, p) for p in watched_patterns):
                continue

            # §11 skip guard: app-backed jobs the delivery monitor
            # already watches per-window must not also dead-man-page here.
            if (bot_id, label) in delivery_monitored or (bot_id, jid) in delivery_monitored:
                print(f"[cron_alert] bot={bot_id} label={label}: covered by "
                      "delivery monitor, skipping")
                continue

            job_state = (jobs_state.get(jid) or {}).get("state") or {}
            last_run_ms = job_state.get("lastRunAtMs")
            last_run_at: str | None = None
            if last_run_ms:
                last_run_at = datetime.fromtimestamp(
                    last_run_ms / 1000, tz=timezone
                ).isoformat()

            should_alert = False
            reason = ""
            if last_run_at is None:
                should_alert = True
                reason = "never run"
            else:
                try:
                    last_dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                    age = now - last_dt
                    if age > silence_td:
                        should_alert = True
                        reason = f"last run {int(age.total_seconds() // 3600)}h ago (threshold: {silence_days}d)"
                except Exception as e:
                    print(f"[cron_alert] bot={bot_id} label={label}: bad timestamp {last_run_at!r}: {e}")
                    continue

            if not should_alert:
                print(f"[cron_alert] bot={bot_id} label={label}: OK (last_run={last_run_at})")
                continue

            # Check dedup flag
            flag = _flag_path(shared_dir, label, today_str)
            if flag.exists():
                print(f"[cron_alert] bot={bot_id} label={label}: alert already sent today (flag exists)")
                continue

            print(f"[cron_alert] ALERT bot={bot_id} label={label}: {reason}")
            _send_alert(
                shared_dir=shared_dir, network=config,
                bot_id=bot_id, label=label, reason=reason,
                dry_run=dry_run,
            )

            if not dry_run:
                _write_flag(flag)
            alerts_fired += 1

    return alerts_fired


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dead-man's switch for Evolve cron jobs",
        epilog="Example: python3 cron_alert.py --dry-run",
    )
    parser.add_argument("--network", default=None, help="Path to network.json")
    parser.add_argument("--dry-run", action="store_true", help="Report without sending alerts or writing flags")
    args = parser.parse_args()

    network_path = resolve_network_path(Path(args.network) if args.network else None)
    config = load_config(network_path)

    count = check_cron_alerts(config, dry_run=args.dry_run)
    print(f"[cron_alert] Done. {count} alert(s) fired.")
    sys.exit(0 if count == 0 else 1)


if __name__ == "__main__":
    main()
