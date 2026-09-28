"""local_backup_signal — fire Signals on Time Machine health gaps.

Phase 2 of the backup architecture rework
(spec internal/spec-backup-and-data-classification-2026-05-28.md).

Reads the wrapper in ``local_backup`` once per cadence run and emits up
to three Signal types (all under producer ``local_backup_signal``):

  local_backup_not_configured — TM is not configured at all. info, pod scope.
                                 Resolves the moment a destination is added.

  local_backup_stale          — last backup is older than the warn/alert
                                 thresholds. warn at 48h, escalates to
                                 alert at 7d. pod scope. Resolves when a
                                 fresh backup completes.

  local_backup_workspace_excluded — pod paths (shared dir, deploy checkout)
                                 are excluded from TM. alert, pod scope.
                                 Auto-resolves when exclusions are cleared.

Sweep-style: ``run()`` computes the kept-signature set per pass, then
``sweep_resolve()`` archives signals no longer firing. Pure Python; the
underlying tmutil call lives in ``local_backup``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evolve_config import get_shared_dir, load_config
from platform_profile import get_profile
from local_backup import (
    LocalBackupStatus,
    SETTINGS_DEEPLINK,
    get_local_backup_status,
)
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "local_backup_signal"

# Thresholds. 48h gives nightly TM cadence two cycles of headroom before
# we wake the operator; 7d is the "you've lost meaningful state" line.
STALE_WARN_HOURS = 48
STALE_ALERT_HOURS = 24 * 7

# Deploy-checkout path checked alongside shared_dir for TM exclusions. Same
# constant the admin endpoint passes; duplicated here so the standalone
# monitor doesn't depend on Flask config. Platform-keyed:
# /Users/Shared/evolve-repo on macOS, /var/lib/evolve/repo on Linux.
_DEPLOY_CHECKOUT = Path(get_profile().deploy_checkout_default)


# ─── Signal builders ───────────────────────────────────────────────────────


def build_signal_not_configured() -> dict:
    return dict(
        signature=make_signature(PRODUCER, "local_backup_not_configured", None),
        producer=PRODUCER,
        type="local_backup_not_configured",
        flavor="maintenance",
        severity="info",
        scope="pod",
        title="Time Machine is not configured on this pod",
        body=(
            "No Time Machine destination is set, so there's no local "
            "snapshot history. If the pod's drive fails or someone "
            "accidentally deletes data, only cloud backups can restore "
            "it — and that path only covers what's classified for cloud.\n"
            "\n"
            "Plug in an external SSD or point Time Machine at a NAS, then "
            "configure it in System Settings → Time Machine. The admin UI "
            "will pick up the change within an hour.\n"
            "\n"
            "If you intentionally don't want local backup (cloud-only "
            "policy), snooze this Signal."
        ),
        details=dict(
            settings_deeplink=SETTINGS_DEEPLINK,
            what_it_means=(
                "Local backup catches the failures cloud backup can't — "
                "accidental deletes, ransomware-style corruption, drives "
                "going bad. Without TM, recovery is only as fine-grained "
                "as the nightly cloud commits."
            ),
            fix_steps=(
                "1. Plug in an external SSD (or set up a TM-compatible NAS)\n"
                "2. Open System Settings → Time Machine\n"
                "3. Add Backup Disk → pick the destination\n"
                "4. The first backup runs immediately; this Signal resolves "
                "on the next monitor pass (within ~1h)"
            ),
        ),
    )


def build_signal_stale(hours_ago: float | None, last_backup_at: str | None) -> dict:
    if hours_ago is None:
        title = "Time Machine is configured but has never run"
        h_display = "never"
    else:
        title = f"Time Machine last backed up {int(hours_ago)}h ago"
        h_display = f"{int(hours_ago)}h ago"
    severity = "alert" if (hours_ago or 0) >= STALE_ALERT_HOURS else "warn"
    body_lines = [
        f"Last Time Machine backup: **{h_display}**.",
        "",
        "Common causes:",
        "- External backup drive is unplugged or asleep",
        "- Network destination (NAS) is offline or unreachable",
        "- macOS paused TM after repeated failures",
        "",
        "Open System Settings → Time Machine, hit **Back Up Now**, and "
        "check the destination is reachable.",
    ]
    return dict(
        signature=make_signature(PRODUCER, "local_backup_stale", None),
        producer=PRODUCER,
        type="local_backup_stale",
        flavor="maintenance",
        severity=severity,
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details=dict(
            last_backup_at=last_backup_at,
            hours_ago=hours_ago,
            warn_threshold_hours=STALE_WARN_HOURS,
            alert_threshold_hours=STALE_ALERT_HOURS,
            settings_deeplink=SETTINGS_DEEPLINK,
            what_it_means=(
                "Each day that passes without a TM backup is a day of "
                "unsnapshotted change. Past the alert threshold (1 week), "
                "any recovery from a local issue will likely lose work."
            ),
        ),
    )


def build_signal_workspace_excluded(excluded_paths: list[str]) -> dict:
    body_lines = [
        "Time Machine is running, but the following pod paths are excluded "
        "from backup:",
        "",
    ]
    body_lines.extend(f"- `{p}`" for p in excluded_paths)
    body_lines += [
        "",
        "Anything in these paths won't appear in TM snapshots — local "
        "recovery from accidental deletes or corruption won't work for "
        "this data even though TM looks healthy elsewhere.",
        "",
        "Open System Settings → Time Machine → Options, and remove these "
        "paths from the exclusion list.",
    ]
    return dict(
        signature=make_signature(PRODUCER, "local_backup_workspace_excluded", None),
        producer=PRODUCER,
        type="local_backup_workspace_excluded",
        flavor="maintenance",
        severity="alert",
        scope="pod",
        title=f"Time Machine is excluding {len(excluded_paths)} pod path(s)",
        body="\n".join(body_lines),
        details=dict(
            excluded_paths=list(excluded_paths),
            settings_deeplink=SETTINGS_DEEPLINK,
            what_it_means=(
                "TM exclusions silently exempt directories from snapshots. "
                "An operator may have added a pod path during cleanup and "
                "forgotten, which leaves a hole in local recovery exactly "
                "where the bot's data lives."
            ),
        ),
    )


# ─── Collection ────────────────────────────────────────────────────────────


def collect_signals(status: LocalBackupStatus) -> list[dict]:
    """Build the Signal specs implied by a LocalBackupStatus snapshot."""
    # Non-macOS: nothing to monitor; nothing to clear.
    if not status.available:
        return []

    specs: list[dict] = []

    if not status.configured:
        specs.append(build_signal_not_configured())
        return specs

    # Stale check. ``last_backup_hours_ago`` is None when the destination
    # is set but no backup has ever completed — that's also a "stale"
    # condition (just with a "never" presentation).
    if status.last_backup_hours_ago is None or status.last_backup_hours_ago >= STALE_WARN_HOURS:
        specs.append(build_signal_stale(
            status.last_backup_hours_ago, status.last_backup_at,
        ))

    if status.excluded_pod_paths:
        specs.append(build_signal_workspace_excluded(status.excluded_pod_paths))

    return specs


# ─── Run loop (called by launchd/cron via main) ────────────────────────────


def run(
    shared_dir: Path,
    pod_paths: list[Path],
    *,
    dry_run: bool = False,
    status_getter=get_local_backup_status,
) -> tuple[set[str], int, int]:
    """Single-pass monitor cycle. Returns (kept_signatures, n_fired, n_resolved)."""
    status = status_getter(pod_paths=pod_paths)
    specs = collect_signals(status)

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
                f"[local_backup_signal] observe failed for "
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
                reason="auto-resolve: condition cleared",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[local_backup_signal] sweep_resolve failed: {exc}", flush=True)

    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="local_backup_signal — Time Machine health monitor",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    pod_paths: list[Path] = [shared_dir, _DEPLOY_CHECKOUT]
    kept, n_fired, n_resolved = run(
        shared_dir, pod_paths, dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"[local_backup_signal] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[local_backup_signal] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
