"""local_backup — read-only Time Machine status surface.

Phase 2 of the backup architecture rework
(spec internal/spec-backup-and-data-classification-2026-05-28.md).

Wraps ``tmutil`` to answer the questions an operator has when they open
Backup → Local:

  - Is Time Machine configured at all?
  - What destination(s)? Local disk, NAS, or something else?
  - When was the last successful backup?
  - Is a backup running right now?
  - Is the pod's shared directory included (not excluded) from TM?

We deliberately do NOT initiate or modify TM configuration from here.
First-time setup requires interaction in System Settings — there's no
``tmutil`` command to add an initial destination. The admin UI's job
is to make state visible and deep-link the operator to System Settings
when they need to act.

Same testability pattern as ``backup_visibility``: subprocess injection
via a ``_run`` parameter, so tests substitute fake tmutil output.
"""

from __future__ import annotations

import datetime as _dt
import plistlib
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# ─── Constants ─────────────────────────────────────────────────────────────

_TMUTIL = "/usr/bin/tmutil"
_TM_TIMEOUT_S = 8.0  # tmutil is local; even network destinations should be fast

# Deep-link URL macOS opens directly in System Settings → Time Machine.
# Phase 4 polish: this gets rendered as a button on the Local tab.
SETTINGS_DEEPLINK = "x-apple.systempreferences:com.apple.preference.timemachine"

# ``tmutil latestbackup`` returns a path like
# ``/Volumes/Backup/Backups.backupdb/MyMac/2026-05-28-120000``.
# The trailing path component is the snapshot timestamp; parse it rather
# than os.stat-ing the directory because mtime can drift on network shares.
_SNAPSHOT_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{6})$")


# ─── Data shapes ───────────────────────────────────────────────────────────


@dataclass
class TMDestination:
    name: str
    kind: str            # "Local" | "Network" | "Unknown"
    mount_point: str | None = None
    id: str | None = None
    bytes_available: int | None = None
    bytes_used: int | None = None
    last_destination: bool = False


@dataclass
class LocalBackupStatus:
    available: bool                        # tmutil exists on this system
    configured: bool                       # at least one destination set
    destinations: list[TMDestination] = field(default_factory=list)
    last_backup_at: str | None = None      # ISO UTC
    last_backup_hours_ago: float | None = None
    in_progress: bool = False
    excluded_pod_paths: list[str] = field(default_factory=list)
    settings_deeplink: str = SETTINGS_DEEPLINK
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["destinations"] = [asdict(x) for x in self.destinations]
        return d


# ─── Internals ─────────────────────────────────────────────────────────────


def _default_run(args: list[str], *, timeout: float = _TM_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _parse_snapshot_path(path: str) -> str | None:
    """Return ISO-UTC timestamp parsed from a snapshot path.

    Returns None if the path doesn't match the expected shape.
    """
    if not path:
        return None
    tail = path.rstrip("/").split("/")[-1]
    m = _SNAPSHOT_NAME_RE.match(tail)
    if not m:
        return None
    y, mo, d, hms = m.groups()
    try:
        ts = _dt.datetime(
            int(y), int(mo), int(d),
            int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
            tzinfo=_dt.timezone.utc,
        )
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _hours_ago(iso_ts: str | None, *, now: _dt.datetime | None = None) -> float | None:
    if not iso_ts:
        return None
    try:
        ts = _dt.datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc,
        )
    except ValueError:
        return None
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return round((now - ts).total_seconds() / 3600, 1)


def _parse_destinations(plist_bytes: bytes) -> list[TMDestination]:
    """Parse the XML plist from ``tmutil destinationinfo -X``."""
    if not plist_bytes:
        return []
    try:
        data = plistlib.loads(plist_bytes)
    except (plistlib.InvalidFileException, ValueError, OSError):
        return []
    raw = data.get("Destinations") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[TMDestination] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(TMDestination(
            name=str(entry.get("Name") or "").strip() or "(unnamed)",
            kind=str(entry.get("Kind") or "Unknown"),
            mount_point=(entry.get("MountPoint") or None),
            id=(entry.get("ID") or None),
            bytes_available=entry.get("BytesAvailable"),
            bytes_used=entry.get("BytesUsed"),
            last_destination=bool(entry.get("LastDestination", False)),
        ))
    return out


def _parse_status(plist_bytes: bytes) -> bool:
    """Parse ``tmutil status -X`` → True if a backup is currently running."""
    if not plist_bytes:
        return False
    try:
        data = plistlib.loads(plist_bytes)
    except (plistlib.InvalidFileException, ValueError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    # ``Running`` is the canonical and authoritative field.
    #
    # 2026-05-29 fix: an earlier implementation also checked
    # ``BackupPhase not in ("BackupNotRunning",)`` as a fallback for older
    # macOS, but ``BackupPhase`` carries many idle/transition values
    # (``Idle``, ``ThinningPostBackup``, ``Starting``, ``MeasuringFreeSpace``)
    # that aren't actually "backup actively running." Treating any non-
    # BackupNotRunning value as in_progress caused the Local subtab to
    # show "Backing up now" permanently on pods where TM was simply
    # attached. Trust ``Running`` alone.
    return data.get("Running") in (1, True, "1")


def _parse_isexcluded(stdout: str) -> bool | None:
    """``tmutil isexcluded`` prints ``[Excluded]`` or ``[Included]``. None if neither."""
    if not stdout:
        return None
    if "[Excluded]" in stdout:
        return True
    if "[Included]" in stdout:
        return False
    return None


# ─── Public entry point ────────────────────────────────────────────────────


def get_local_backup_status(
    pod_paths: list[Path] | None = None,
    *,
    _run: Callable[..., subprocess.CompletedProcess] = _default_run,
    _now: _dt.datetime | None = None,
) -> LocalBackupStatus:
    """Snapshot Time Machine state.

    ``pod_paths`` are filesystem paths the pod cares about (typically the
    shared directory and the deploy checkout). Each is checked against
    ``tmutil isexcluded``; any that come back ``[Excluded]`` are reported
    in ``excluded_pod_paths`` so the monitor can fire a Signal.

    ``_run`` and ``_now`` are test hooks.
    """
    if not Path(_TMUTIL).exists():
        # Non-macOS environment (CI, dev box). The endpoint treats this as
        # "feature not available" rather than an error.
        return LocalBackupStatus(available=False, configured=False)

    # 1. Destinations
    destinations: list[TMDestination] = []
    try:
        r = _run([_TMUTIL, "destinationinfo", "-X"])
        if r.returncode == 0 and r.stdout:
            destinations = _parse_destinations(r.stdout.encode("utf-8"))
        # rc != 0 typically means "No destinations configured." — leave the
        # list empty, configured=False below.
    except (subprocess.TimeoutExpired, OSError) as exc:
        return LocalBackupStatus(
            available=True, configured=False,
            error=f"tmutil destinationinfo failed: {exc!s}",
        )

    configured = len(destinations) > 0

    # 2. Latest backup timestamp (only meaningful if configured)
    last_backup_at: str | None = None
    if configured:
        try:
            r = _run([_TMUTIL, "latestbackup"])
            if r.returncode == 0 and r.stdout.strip():
                last_backup_at = _parse_snapshot_path(r.stdout.strip())
        except (subprocess.TimeoutExpired, OSError):
            # Best-effort; missing timestamp shows up as "never backed up."
            pass

    # 3. In-progress check
    in_progress = False
    if configured:
        try:
            r = _run([_TMUTIL, "status", "-X"])
            if r.returncode == 0 and r.stdout:
                in_progress = _parse_status(r.stdout.encode("utf-8"))
        except (subprocess.TimeoutExpired, OSError):
            pass

    # 4. Exclusion checks for pod paths
    excluded: list[str] = []
    for p in (pod_paths or []):
        path_str = str(p)
        try:
            r = _run([_TMUTIL, "isexcluded", path_str])
        except (subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode != 0:
            continue
        if _parse_isexcluded(r.stdout) is True:
            excluded.append(path_str)

    return LocalBackupStatus(
        available=True,
        configured=configured,
        destinations=destinations,
        last_backup_at=last_backup_at,
        last_backup_hours_ago=_hours_ago(last_backup_at, now=_now),
        in_progress=in_progress,
        excluded_pod_paths=excluded,
    )
