"""evolve_admin.fd_limits — raise the process's soft NOFILE limit at startup.

Why this exists (the 2026-07-28 incident): the admin daemon runs under
launchd, whose macOS default soft ``RLIMIT_NOFILE`` is 256. Under werkzeug's
thread-per-request amplification (plus per-bot subprocess fan-out on status
endpoints), request bursts hit EMFILE — ``OSError: [Errno 24] Too many open
files`` — for hours at a time. The TCP side recovered per-request, but the
unix-socket listener's accept path died during a storm and stayed dead for
10 days (bound fd alive, backlog full, every ``connect()`` → ECONNREFUSED).

Two-layer defense, both in this PR:
  1. The launchd plist / systemd unit sets the limit at the supervisor level
     (``JobSpec.soft_file_limit`` → ``SoftResourceLimits.NumberOfFiles`` /
     ``LimitNOFILE``) — but only takes effect after the next deploy rewrites
     the job file.
  2. This module raises the limit from *inside* the process at serve startup
     — effective immediately on the next daemon restart, and covers any
     launch path that bypasses the rendered job file (manual ``evolve-admin
     serve`` in a terminal inherits the shell's limit, often also 256).

Best-effort by design: a failure to raise the limit is logged and never
blocks startup. Platform note: the ``resource`` module exists on every POSIX
Python (macOS + Linux — the only pod platforms); the import guard is for
completeness, not an expected path.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Matches the JobSpec.soft_file_limit the admin-ui plist/unit sets. 4096 is
# comfortably above the observed burst ceiling (~63 steady-state fds; storms
# blew through 256) while staying under default kernel per-process maxima
# (macOS kern.maxfilesperproc 10240+, Linux fs.nr_open 2**20).
DEFAULT_NOFILE_TARGET = 4096


def raise_nofile_limit(target: int = DEFAULT_NOFILE_TARGET) -> "tuple[int, int] | None":
    """Best-effort raise of the soft RLIMIT_NOFILE to ``min(target, hard)``.

    Returns the resulting ``(soft, hard)`` pair, or ``None`` when the limit
    could not be read/raised (logged, never raises). Never *lowers* an
    already-higher soft limit.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover — non-POSIX platform only
        log.warning("fd_limits: resource module unavailable; NOFILE unchanged")
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # RLIM_INFINITY compares as -1 on Linux — min() against it would
        # produce a nonsense negative soft limit, so special-case it.
        ceiling = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if ceiling <= soft:
            log.info(
                "fd_limits: soft NOFILE already %d (hard=%d, target=%d) — unchanged",
                soft, hard, target,
            )
            return soft, hard
        resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
        log.info(
            "fd_limits: raised soft NOFILE %d -> %d (hard=%s)",
            soft, ceiling,
            "infinity" if hard == resource.RLIM_INFINITY else hard,
        )
        return ceiling, hard
    except (ValueError, OSError) as exc:
        log.warning("fd_limits: could not raise NOFILE to %d: %s", target, exc)
        return None
