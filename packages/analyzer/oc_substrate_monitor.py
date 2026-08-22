"""oc_substrate_monitor — Signal-producing freshness check for OpenClaw substrate.

Two adjacent OpenClaw substrate daemons live outside the
``ai.evolve.evolve.*`` / ``ai.openclaw.evolve.*`` naming convention that
[packages/analyzer/monitor_coverage.py] watches, so a silence in either
historically went unnoticed by the in-pod monitoring stack and was only
caught by the out-of-tree ``openclaw-watchdog.py`` script (running as
the pod-admin macOS account) — which itself escalated to Telegram only
via a security-bot daily briefing that has been winding down. This
monitor closes that gap.

Watched substrate (both writers live in ``/Users/Shared/...``, so the
``evolve`` user can read them directly, no sudo):

  1. **OpenClaw auto-updater** —
     ``/Users/Shared/openclaw-updater-state.json`` is rewritten by the
     ``ai.openclaw.updater`` LaunchAgent on every check (default
     cadence: ~hourly). Its ``last_check`` field is the source of truth;
     when the agent hangs, ``last_check`` stops advancing while the file
     itself sits on disk. We treat ``last_check`` older than 120 min as
     ``oc_updater_stale``. The deeper consequence (OC version drifting
     too far behind) is already detected by
     :mod:`generators.sysadmin_watchdog.detectors.platform.version`,
     but that's the slow signal; this is the early one.

  2. **OpenClaw usage-collector** —
     ``ai.openclaw.usage-collector`` LaunchAgent writes
     ``/Users/Shared/openclaw-usage/all-YYYY-MM-DD.json`` every 30 min.
     The downstream cost stack
     (``cost_watchdog``/``spend_alert``/``session_economics``) consumes
     these files but does not alert when the collector itself goes
     silent. On 2026-06-01 we observed that the latest ``all-*.json``
     was from 2026-05-20 — twelve days of silent staleness, exactly the
     failure mode this monitor catches. We treat a missing file or a
     file older than 120 min (after 9am local) as
     ``usage_collector_stale``.

Producer: ``oc_substrate_monitor``
Signal types:

  - ``oc_updater_stale``        — updater hasn't checked in (pod-scoped)
  - ``usage_collector_stale``   — collector hasn't written today's file (pod-scoped)

Both severities default to ``warn``. The condition is operationally
important (substrate is rotting) but not page-the-operator urgent — the
deeper consequences each have their own Signals already.

Auto-resolves via :func:`signals.store.sweep_resolve` on the next run
once the underlying file is fresh again.

Cadence: hourly. Both upstream daemons advertise 30–60 min cadences, so
hourly detection bounds silent staleness to ~2.5h worst-case.

Pure Python, no LLM. Runs as the ``evolve`` user, same shape as the
other ``ai.openclaw.evolve.*`` monitors.

Replaces ``check_updater_freshness`` and ``check_collector_freshness``
from the out-of-tree ``/opt/homebrew/bin/openclaw-watchdog.py``.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import get_shared_dir, load_config
from platform_profile import get_profile
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "oc_substrate_monitor"

SIGNAL_TYPE_UPDATER_STALE = "oc_updater_stale"
SIGNAL_TYPE_COLLECTOR_STALE = "usage_collector_stale"

# Both watched writers — the ``ai.openclaw.updater`` and
# ``ai.openclaw.usage-collector`` LaunchAgents — are macOS user-domain
# LaunchAgents (``/Library/LaunchAgents/ai.openclaw.*.plist``) that run
# under the pod-admin account. There is no Linux (systemd) equivalent
# anywhere in the codebase: nothing writes ``openclaw-updater-state.json``
# or ``openclaw-usage/all-*.json`` on a Linux pod. So on Linux both files
# are PERMANENTLY absent — not stale, just non-existent by design — and a
# blind re-path to ``{shared_dir}`` would simply move the false-fire to a
# new never-written path. The monitor is therefore platform-gated to
# macOS (see ``collect()``); on Linux it is a clean no-op.
#
# The literal ``/Users/Shared`` paths below are the canonical macOS
# locations the LaunchAgents write to. They are kept byte-identical for
# macOS — the gate, not a path change, is what makes Linux correct.
UPDATER_STATE_PATH = Path("/Users/Shared/openclaw-updater-state.json")
USAGE_COLLECTOR_DIR = Path("/Users/Shared/openclaw-usage")

# Thresholds match the watchdog's prior values — both upstream daemons
# advertise 30–60 min cadences; 120 min absorbs one missed cycle without
# false-firing on a deliberate restart.
UPDATER_MAX_AGE_MINUTES = 120
COLLECTOR_MAX_AGE_MINUTES = 120

# Don't flag a missing-today file before this local hour. The collector
# rolls into a new file at midnight; without this grace window we'd fire
# every night between 00:00 and the first write of the day.
COLLECTOR_GRACE_HOUR = 9


# ─────────────────────────────────────────────────────────────────────────────
# Detectors — pure functions returning Signal-spec dicts or None
# ─────────────────────────────────────────────────────────────────────────────


def detect_updater_stale(
    state_path: Path = UPDATER_STATE_PATH,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Return a Signal spec when the OC auto-updater is silent.

    Two failure modes are flagged at the same severity:

      * State file missing entirely → updater LaunchAgent never wrote it
        (likely never bootstrapped or was unloaded).
      * ``last_check`` older than ``UPDATER_MAX_AGE_MINUTES`` → agent is
        loaded but not advancing (hung process, sleep loop, etc.).

    A corrupt/unreadable state file falls into the missing-file branch.
    """
    now = now or datetime.now(timezone.utc)

    if not state_path.exists():
        body = (
            f"OpenClaw auto-updater state file missing: `{state_path}`.\n\n"
            "The `ai.openclaw.updater` LaunchAgent writes this file on "
            "every check. A missing file means the agent never ran (not "
            "bootstrapped, unloaded, or crashing immediately at start).\n\n"
            "Check on the host:\n"
            "  `launchctl list | grep ai.openclaw.updater`\n"
            "  `ls -la ~<pod-admin-user>/Library/LaunchAgents/ai.openclaw.updater.plist`"
        )
        return _spec(
            sig_type=SIGNAL_TYPE_UPDATER_STALE,
            title="OpenClaw auto-updater state file missing",
            body=body,
            details={"reason": "state_file_missing", "state_path": str(state_path)},
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        body = (
            f"OpenClaw auto-updater state file is unreadable: `{state_path}`.\n\n"
            f"Error: `{exc}`\n\n"
            "The file exists but doesn't parse as JSON. Likely a write "
            "was interrupted mid-flight; check whether the agent is "
            "still running."
        )
        return _spec(
            sig_type=SIGNAL_TYPE_UPDATER_STALE,
            title="OpenClaw auto-updater state file unreadable",
            body=body,
            details={"reason": "state_file_unreadable", "state_path": str(state_path)},
        )

    last_check_str = (state.get("last_check") or "").strip()
    if not last_check_str:
        body = (
            f"OpenClaw auto-updater state file has no `last_check` field: "
            f"`{state_path}`.\n\nThe agent may be starting up or in a "
            "broken state. Check the agent's stderr log."
        )
        return _spec(
            sig_type=SIGNAL_TYPE_UPDATER_STALE,
            title="OpenClaw auto-updater state has no last_check",
            body=body,
            details={"reason": "missing_last_check", "state_path": str(state_path)},
        )

    try:
        last_check = datetime.fromisoformat(last_check_str)
    except ValueError:
        body = (
            f"OpenClaw auto-updater `last_check` does not parse as ISO 8601: "
            f"`{last_check_str}`."
        )
        return _spec(
            sig_type=SIGNAL_TYPE_UPDATER_STALE,
            title="OpenClaw auto-updater last_check is malformed",
            body=body,
            details={
                "reason": "malformed_last_check",
                "last_check_raw": last_check_str[:60],
            },
        )

    if last_check.tzinfo is None:
        last_check = last_check.replace(tzinfo=timezone.utc)

    age_minutes = (now - last_check).total_seconds() / 60
    if age_minutes <= UPDATER_MAX_AGE_MINUTES:
        return None

    body = (
        f"OpenClaw auto-updater `last_check` is "
        f"{age_minutes:.0f} min old (threshold: "
        f"{UPDATER_MAX_AGE_MINUTES} min).\n\n"
        f"Last check: `{last_check_str}`\n"
        f"State file: `{state_path}`\n\n"
        "The `ai.openclaw.updater` LaunchAgent is loaded but not "
        "advancing. Either the process is hung or the LaunchAgent "
        "is unloaded. Check on the host:\n"
        "  `launchctl list | grep ai.openclaw.updater`\n"
        "  `tail -50 ~<pod-admin-user>/Library/Logs/openclaw-updater.log` "
        "(or the path set in the LaunchAgent's StandardErrorPath)\n\n"
        "While the updater is silent OpenClaw will drift behind "
        "upstream releases — version_behind Signals will follow if "
        "the silence persists."
    )
    return _spec(
        sig_type=SIGNAL_TYPE_UPDATER_STALE,
        title=f"OpenClaw auto-updater silent for {age_minutes:.0f} min",
        body=body,
        details={
            "reason": "last_check_stale",
            "age_minutes": round(age_minutes, 1),
            "threshold_minutes": UPDATER_MAX_AGE_MINUTES,
            "last_check": last_check_str,
            "last_applied_version": state.get("last_applied_version"),
            "state_path": str(state_path),
        },
    )


def detect_collector_stale(
    usage_dir: Path = USAGE_COLLECTOR_DIR,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Return a Signal spec when the OC usage-collector is silent.

    Looks at today's ``all-YYYY-MM-DD.json`` — the rollup file the
    usage-collector LaunchAgent writes every 30 min. Two cases:

      * File missing after the morning grace window → collector hasn't
        written anything today.
      * File older than ``COLLECTOR_MAX_AGE_MINUTES`` → collector loaded
        a stale write and stopped.

    Before ``COLLECTOR_GRACE_HOUR`` local time we don't fire missing-
    today, because the rollover into a new file happens at midnight and
    the first write of the day may legitimately be hours away.
    """
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone()

    today = local_now.strftime("%Y-%m-%d")
    today_file = usage_dir / f"all-{today}.json"

    if not today_file.exists():
        # Before the grace hour, missing today's file is normal — we
        # just rolled over at midnight and the collector hasn't written
        # its first rollup yet.
        if local_now.hour < COLLECTOR_GRACE_HOUR:
            return None
        # After the grace hour, find the most-recent file for context.
        latest = _most_recent_collector_file(usage_dir)
        latest_note = (
            f"Latest file in the directory: `{latest.name}` "
            f"(modified {_age_str(latest)})"
            if latest is not None
            else "The directory has no `all-*.json` files at all."
        )
        body = (
            f"OpenClaw usage-collector has not written today's rollup: "
            f"`{today_file.name}`.\n\n"
            f"{latest_note}\n\n"
            "The `ai.openclaw.usage-collector` LaunchAgent writes "
            "`all-YYYY-MM-DD.json` every 30 min. A missing file past "
            f"{COLLECTOR_GRACE_HOUR}am means the agent is unloaded or "
            "crashing on every wake.\n\n"
            "Check on the mini:\n"
            "  `launchctl list | grep ai.openclaw.usage-collector`\n"
            f"  `tail -50 {usage_dir / 'collector-stderr.log'}`\n\n"
            "While the collector is silent, Cost/Usage views will show "
            "yesterday's numbers but no current activity — the "
            "downstream cost-burst and per-bot cap detectors operate on "
            "the live JSONL feed instead, so this Signal is the only "
            "place this specific staleness surfaces."
        )
        return _spec(
            sig_type=SIGNAL_TYPE_COLLECTOR_STALE,
            title="OpenClaw usage-collector silent today",
            body=body,
            details={
                "reason": "today_file_missing",
                "expected_path": str(today_file),
                "latest_file": latest.name if latest is not None else None,
            },
        )

    age_minutes = (now.timestamp() - today_file.stat().st_mtime) / 60
    if age_minutes <= COLLECTOR_MAX_AGE_MINUTES:
        return None

    body = (
        f"OpenClaw usage-collector last wrote "
        f"`{today_file.name}` {age_minutes:.0f} min ago "
        f"(threshold: {COLLECTOR_MAX_AGE_MINUTES} min).\n\n"
        "The file exists but is stale; the `ai.openclaw.usage-collector` "
        "LaunchAgent is no longer cycling. Check on the mini:\n"
        "  `launchctl list | grep ai.openclaw.usage-collector`\n"
        f"  `tail -50 {usage_dir / 'collector-stderr.log'}`"
    )
    return _spec(
        sig_type=SIGNAL_TYPE_COLLECTOR_STALE,
        title=f"OpenClaw usage-collector silent for {age_minutes:.0f} min",
        body=body,
        details={
            "reason": "today_file_stale",
            "age_minutes": round(age_minutes, 1),
            "threshold_minutes": COLLECTOR_MAX_AGE_MINUTES,
            "expected_path": str(today_file),
        },
    )


def _most_recent_collector_file(usage_dir: Path) -> Path | None:
    try:
        files = sorted(usage_dir.glob("all-*.json"))
    except OSError:
        return None
    return files[-1] if files else None


def _age_str(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "unknown age"
    age_hours = (time.time() - mtime) / 3600
    if age_hours < 24:
        return f"{age_hours:.0f}h ago"
    return f"{age_hours / 24:.0f}d ago"


def _spec(*, sig_type: str, title: str, body: str, details: dict) -> dict:
    return dict(
        signature=make_signature(PRODUCER, sig_type, "pod"),
        producer=PRODUCER,
        type=sig_type,
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title=title,
        body=body,
        details=details,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner — call detectors and write Signals
# ─────────────────────────────────────────────────────────────────────────────


def collect() -> list[dict]:
    """Run all detectors and return the Signal specs.

    Platform-gated to macOS: the watched ``ai.openclaw.updater`` and
    ``ai.openclaw.usage-collector`` LaunchAgents are macOS-only (see the
    module-level note on ``UPDATER_STATE_PATH``). On Linux both source
    files are absent by design, so the monitor returns no specs — a clean
    no-op, never a false ``oc_updater_stale`` / ``usage_collector_stale``.
    """
    if get_profile().name != "macos":
        return []

    specs: list[dict] = []
    for detector in (detect_updater_stale, detect_collector_stale):
        try:
            spec = detector()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[oc_substrate_monitor] {detector.__name__} crashed: {exc}",
                flush=True,
            )
            continue
        if spec is not None:
            specs.append(spec)
    return specs


def run(shared_dir: Path, *, dry_run: bool = False) -> tuple[set[str], int, int]:
    """Collect findings, write Signals, sweep-resolve cleared conditions.

    Returns ``(kept_signatures, n_fired, n_resolved)``.
    """
    specs = collect()
    kept: set[str] = set()
    n_fired = 0
    for spec in specs:
        kept.add(spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[oc_substrate_monitor] observe failed for "
                f"{spec['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: substrate freshness restored",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[oc_substrate_monitor] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "oc_substrate_monitor — Signal-producing freshness check for "
            "the OpenClaw auto-updater and usage-collector LaunchAgents."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=None,
        help="Override the shared dir (default: from network.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Signal specs instead of writing them.",
    )
    args = parser.parse_args()

    # get_shared_dir requires the loaded network config (it reads
    # config["sharedDir"]); a no-arg call is a TypeError. Match the ~40
    # other daemons: load_config() first, then resolve. (W10-G #3.)
    config = load_config()
    shared_dir = args.shared_dir or get_shared_dir(config)
    kept, n_fired, n_resolved = run(shared_dir, dry_run=args.dry_run)
    print(
        f"[oc_substrate_monitor] kept={len(kept)} fired={n_fired} "
        f"resolved={n_resolved}",
        flush=True,
    )


if __name__ == "__main__":
    main()
