"""log_cap.py — cap flat-file logs by size with copy-then-truncate rotation.

Several Evolve daemons write to flat ``.log`` / ``.jsonl`` files that
grow without bound:

  - ``/Users/Shared/evolve/logs/audit.log`` — audit.py (15-min cron),
    direct ``open(..., "a")`` writes from ``_log()``.
  - ``/Users/Shared/evolve/logs/better_engine.log`` — better_engine_refresh.py
    (15-min cron) writes via ``_log()`` AND launchd captures the same
    path via ``StandardOutPath``. The launchd fd stays open for the
    lifetime of the daemon, so any rename-style rotation would leave
    launchd writing to the orphaned inode forever.
  - ``/Users/Shared/evolve/logs/audit-warns.jsonl`` — JSONL warn-finding
    log appended by audit.py.
  - ``/Users/Shared/evolve/logs/evolve-admin-ui.err.log`` — the admin-ui
    LaunchDaemon's ``StandardErrorPath``. Same open-fd constraint as
    better_engine.log. It carries no timestamps of its own, so an old
    incident stays indistinguishable from a live one: the 2026-07-21..26
    EMFILE storm (fixed in #3446) still filled 140k of the file's 152k
    lines weeks later and read as a current failure on inspection.
    Capping it bounds both the growth and that confusion.

This module rotates each file when it exceeds ``max_bytes`` via
copy-then-truncate:

  1. Drop ``<name>.<keep>`` if it exists (oldest backup).
  2. Shift ``<name>.<N>`` → ``<name>.<N+1>`` for N in ``keep-1..1``.
  3. Copy ``<name>`` to ``<name>.1``.
  4. Truncate ``<name>`` to 0 bytes via ``os.truncate``.

``os.truncate`` preserves the inode of the original file. That's the
load-bearing property: launchd-managed ``StandardOutPath`` files keep
their open fd valid (the fd points at the inode, not the path), and
new writes from the daemon land in the now-empty file on disk. A
``rename + new empty file`` rotation would let launchd keep writing
to the rotated-out copy forever.

Idempotent and safe to re-run. Files smaller than the cap are
untouched. Missing files are skipped silently.

Daily cadence is enough for current producers (15-min daemons emitting
a handful of lines per run; the 27 MB ``audit.log`` accumulated over
months). If a future producer grows faster, lower the launchd interval
rather than touching the per-writer code.

CLI:

    python3 -m log_cap --max-bytes 10485760 --keep 3 PATH [PATH ...]

With no positional ``PATH``, falls back to the built-in
``DEFAULT_TARGETS`` list — matches the wiring in
``deploy._install_launchd_log_cap``.

Matches the cron pattern used by ``signals.retention`` — installed as
a daily launchd job by ``deploy.py``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from evolve_config import CANONICAL_SHARED_DIR

# 10 MB cap with 3 rotations → ~40 MB total floor per file family. Matches
# telemetry.LOG_FILE's RotatingFileHandler (10 MB × 5 backups) closely
# enough that operators don't have to remember two different policies.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_KEEP = 3

# Built-in target list. New uncapped flat-file logs should be added here
# rather than spawning a parallel cron. The queued gateway-log-cap and
# dispatcher-suppressed rotation tasks will add their paths to this list.
# Resolved from the platform profile rather than hardcoded, so the Linux
# pod caps the same files under its own shared dir (8.3 port, §6). These
# are the platform DEFAULTS, matching what _install_launchd_log_cap wires
# up — a pod with a non-default `sharedDir` passes explicit paths on the
# CLI instead.
_LOG_DIR = CANONICAL_SHARED_DIR / "logs"

DEFAULT_TARGETS: tuple[Path, ...] = (
    _LOG_DIR / "audit.log",
    _LOG_DIR / "better_engine.log",
    _LOG_DIR / "audit-warns.jsonl",
    _LOG_DIR / "evolve-admin-ui.err.log",
)


@dataclass
class CapResult:
    path: Path
    rotated: bool
    # None when the file did not exist (skipped silently).
    size_before: int | None
    backups_kept: int
    error: str | None = None


def cap_log(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> CapResult:
    """Rotate-then-truncate ``path`` if it exceeds ``max_bytes``.

    Inode-preserving: uses ``shutil.copyfile`` to create the ``.1``
    backup and ``os.truncate`` to empty the original in place, so any
    process holding ``path`` open (notably launchd's
    ``StandardOutPath`` fd on ``better_engine.log``) keeps writing to
    the same on-disk file after the rotation.
    """
    if keep < 1:
        return CapResult(path=path, rotated=False, size_before=None,
                         backups_kept=0, error=f"keep must be >= 1, got {keep}")

    try:
        st = path.stat()
    except FileNotFoundError:
        return CapResult(path=path, rotated=False, size_before=None,
                         backups_kept=0)
    except OSError as exc:
        return CapResult(path=path, rotated=False, size_before=None,
                         backups_kept=0, error=f"stat failed: {exc}")

    if not _is_regular_file(st.st_mode):
        return CapResult(path=path, rotated=False, size_before=st.st_size,
                         backups_kept=_count_backups(path, keep),
                         error="not a regular file")

    size = st.st_size
    if size <= max_bytes:
        return CapResult(path=path, rotated=False, size_before=size,
                         backups_kept=_count_backups(path, keep))

    # Step 1: drop the oldest backup if it's at the cap.
    oldest = _backup_path(path, keep)
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Best-effort. If we can't unlink it, the shift below will
        # overwrite it via os.replace anyway.
        pass

    # Step 2: shift existing backups down: .N → .{N+1} for N in keep-1..1.
    for n in range(keep - 1, 0, -1):
        src = _backup_path(path, n)
        dst = _backup_path(path, n + 1)
        try:
            os.replace(src, dst)
        except FileNotFoundError:
            pass

    # Step 3: copy current contents to .1.
    backup1 = _backup_path(path, 1)
    try:
        shutil.copyfile(path, backup1)
    except OSError as exc:
        return CapResult(path=path, rotated=False, size_before=size,
                         backups_kept=_count_backups(path, keep),
                         error=f"copy to backup failed: {exc}")

    # Step 4: truncate original in place.
    try:
        os.truncate(path, 0)
    except OSError as exc:
        # Roll back the copy so the rotated content isn't double-stored.
        try:
            backup1.unlink()
        except OSError:
            pass
        return CapResult(path=path, rotated=False, size_before=size,
                         backups_kept=_count_backups(path, keep),
                         error=f"truncate failed: {exc}")

    return CapResult(path=path, rotated=True, size_before=size,
                     backups_kept=_count_backups(path, keep))


def cap_logs(
    targets: Iterable[Path],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> list[CapResult]:
    """Apply :func:`cap_log` to each path in ``targets``."""
    return [cap_log(p, max_bytes=max_bytes, keep=keep) for p in targets]


def _backup_path(path: Path, n: int) -> Path:
    return path.with_name(f"{path.name}.{n}")


def _count_backups(path: Path, keep: int) -> int:
    count = 0
    for i in range(1, keep + 1):
        if _backup_path(path, i).exists():
            count += 1
    return count


def _is_regular_file(mode: int) -> bool:
    import stat
    return stat.S_ISREG(mode)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cap flat-file logs by size; rotate via copy-then-truncate "
            "so any open fd (e.g. launchd StandardOutPath) stays valid."
        ),
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Rotate when file exceeds N bytes (default: {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"Number of rotated backups to retain (default: {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files to cap. Defaults to the built-in DEFAULT_TARGETS list.",
    )
    args = parser.parse_args(argv)

    targets = list(args.paths) if args.paths else list(DEFAULT_TARGETS)
    results = cap_logs(targets, max_bytes=args.max_bytes, keep=args.keep)

    rotated = sum(1 for r in results if r.rotated)
    under_cap = sum(1 for r in results if not r.rotated and r.size_before is not None and r.error is None)
    missing = sum(1 for r in results if r.size_before is None and r.error is None)
    errors = sum(1 for r in results if r.error)
    print(
        f"log_cap: rotated={rotated} under_cap={under_cap} "
        f"missing={missing} errors={errors} "
        f"max_bytes={args.max_bytes} keep={args.keep}"
    )
    for r in results:
        if r.rotated:
            print(
                f"  rotated {r.path} "
                f"({r.size_before:,} bytes → 0; backups={r.backups_kept})"
            )
        elif r.error:
            print(f"  error {r.path}: {r.error}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
