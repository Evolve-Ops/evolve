"""store_lock — cross-process advisory locking for the file-backed stores.

Spec: docs/spec-state-store-and-deploy-resilience-2026-06-10.md §1.4
(7.1 Phase A).

The Signal store (``signals.store``) and Proposal store
(``arbiter.store``) are mutated by many concurrent processes (admin-ui,
heal, audit, verify, repo-puller, signal-subscriber — plus the evo
gateway and per-bot daemons running as other macOS users). Their
per-file writes are atomic (temp-file + rename) but their *sequences*
are not: find-or-create in ``observe()``, the coalesce branch of
``write_proposal``, and every read-modify-write transition race across
processes. This module provides the serialization primitive both stores
wrap those sequences in.

Design points (all load-bearing — see the spec before changing):

- ``flock(2)``, not lockfile-existence: the lock evaporates with the fd
  on process death, so there is no stale-lock recovery path to get
  wrong.
- The lock fd is opened ``O_RDONLY`` — BSD flock permits ``LOCK_EX`` on
  a read-only fd, so locking an *existing* lock file needs only read
  access. That is what lets every writer class (evolve, evo via ACL,
  bot users) take the lock. Creation needs dir-write, which not every
  writer has (bot users cannot create in evolve-owned ``signals/``) —
  ``ensure_pod_perms`` pre-creates both store lock files evolve-owned
  mode 0666, and that pre-creation is load-bearing, not
  belt-and-suspenders.
- The lock file must NEVER be repaired by replace/rename — only
  chmod/chown in place. A renamed lock file splits mutual exclusion
  across two inodes silently.
- Reentrant per thread (depth counter keyed by canonical path) so e.g.
  ``sweep_resolve`` → ``apply_transition`` nests instead of
  self-deadlocking.
- Bounded acquisition: ``LOCK_NB`` retry loop with a deadline, raising
  :class:`StoreLockTimeout` (an ``OSError``) so a wedged holder can't
  hang every daemon. Store critical sections are milliseconds; the
  default 10 s deadline is generous headroom, not an expected wait.
- Lock ordering invariant: proposals → signals only. ``arbiter.store``
  calls into ``signals.store`` (backrefs); the reverse must never
  happen.
"""

from __future__ import annotations

import errno
import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_TIMEOUT_SECONDS = 10.0
_RETRY_SLEEP_SECONDS = 0.01

# Pre-created by ensure_pod_perms; lazily created here as a fallback for
# writers that have dir-write (see module docstring).
LOCK_FILE_MODE = 0o666


class StoreLockTimeout(OSError):
    """Raised when the store lock can't be acquired within the deadline.

    Subclasses ``OSError`` deliberately: store callers already harden
    against OSError from the rename/unlink paths, so a lock timeout
    surfaces through the same error handling instead of a new escape
    hatch. Direct store mutations fail closed (this propagates); the
    best-effort backref path swallows it by existing design.
    """


# Per-thread reentrancy state: canonical lock path → acquisition depth.
_local = threading.local()


def _depths() -> dict[str, int]:
    d = getattr(_local, "depths", None)
    if d is None:
        d = {}
        _local.depths = d
    return d


@contextmanager
def locked(
    lock_path: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock on ``lock_path``.

    Reentrant within a thread. Creates the lock file if missing (and
    permitted); pre-creation by ``ensure_pod_perms`` covers writers
    that can't create it. Raises :class:`StoreLockTimeout` if the lock
    is not acquired within ``timeout`` seconds.
    """
    key = os.path.realpath(str(lock_path))
    depths = _depths()
    if depths.get(key, 0) > 0:
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    # The store dirs exist on any deployed pod (created at deploy /
    # first write); this mkdir covers fresh test dirs and brand-new
    # pods. PermissionError means we're a writer without dir-create
    # rights — fine as long as the pre-created lock file exists.
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        fd = os.open(str(lock_path), os.O_RDONLY | os.O_CREAT, LOCK_FILE_MODE)
    except OSError as e:
        raise StoreLockTimeout(
            e.errno or errno.EACCES,
            f"store lock unavailable (open failed): {lock_path}: {e}",
        ) from e
    try:
        # We may have just created the file with the umask clamping the
        # mode; widen best-effort so other user classes can open it.
        # Failure is non-fatal: a pre-created 0666 file doesn't need it,
        # and non-owners can't chmod anyway.
        try:
            os.chmod(str(lock_path), LOCK_FILE_MODE)
        except OSError:
            pass

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise StoreLockTimeout(
                        errno.ETIMEDOUT,
                        f"store lock not acquired within {timeout}s: "
                        f"{lock_path}",
                    ) from e
                time.sleep(_RETRY_SLEEP_SECONDS)

        depths[key] = 1
        try:
            yield
        finally:
            del depths[key]
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)
