"""deploy_resilience — starved-box hardening for the deploy pipeline.

Carved out of deploy.py (a no-growth-capped hot-hazard file) so the
2026-06-24 starved-mini incident fixes live in their own module. Two
independent pieces, both consumed by deploy.py / repo_puller.py / the web
upgrade path:

Part A — :func:`plant_never_index_marker`: drop a ``.metadata_never_index``
marker so macOS Spotlight stops INDEXING the churny bot state trees (the
recursive deploy perm-passes were storming ``mds``; ``mds_stores`` held
582 MB). macOS-only; Linux no-ops.

Part C1 — the pod-wide deploy lock (:func:`try_acquire_deploy_lock` /
:func:`release_deploy_lock` / :func:`deploy_lock`): one advisory ``flock`` so a
manual web upgrade and the scheduled repo-puller redeploy sweep can't run their
recursive perm-passes concurrently and double-hammer a starved box.

C2/B (pre-deploy load/memory gate + retry-with-backoff replacing the hard
timeouts + incremental only-on-drift perm passes) is a separate later bite.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import subprocess
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR as _CANONICAL_SHARED_DIR
from platform_profile import get_profile as _get_profile

from .sudo_dest import redirect_refusal  # D-2 gate: dir-shaped bot-owned dest of a root touch
from .telemetry import get_logger

_log = get_logger("deploy_resilience")
_PROFILE = _get_profile()


# ── Part A: Spotlight-exclusion marker ────────────────────────────────────────
# An empty ``.metadata_never_index`` file tells macOS Spotlight (mds/mdworker) to
# stop INDEXING the directory it sits in and everything below it.
NEVER_INDEX_MARKER = ".metadata_never_index"


def plant_never_index_marker(parent: Path, *, via_sudo: bool, enabled: bool = True) -> bool:
    """Plant an empty macOS Spotlight-exclusion marker in ``parent``. macOS-only.

    ``enabled=False`` is an immediate no-op (returns ``False``) — lets a caller
    fold a guard like ``enabled=not check_only`` into the call without an extra
    ``if`` (the hourly drift monitor passes check_only and must not mutate).

    The bot state trees (``sessions.json``, the per-agent sqlite, ``logs/``)
    churn constantly; Spotlight indexing them is pure waste, and every recursive
    deploy perm-pass (``chown -R``/``chmod -R`` over ``.openclaw``) triggers an
    ``mds`` reindex storm — the 2026-06-24 starved-mini incident showed
    ``mds_stores`` holding 582 MB. Dropping a ``.metadata_never_index`` marker at
    the root of each churny tree stops the indexing.

    HONESTY: this suppresses Spotlight *indexing* only (the ``mds_stores``
    growth). It does NOT stop ``fseventsd`` from *recording* the underlying FS
    events — that churn is cut by the separate incremental-perms follow-up.

    macOS-ONLY: Linux has no Spotlight, so this is a hard no-op there (guarded on
    the platform profile). Idempotent: returns ``False`` (no write) when the
    marker already exists or the profile isn't macOS; ``True`` when it plants one.

    ``via_sudo``: the per-bot marker lands in the bot-owned ``.openclaw/`` root,
    which the #3198 read clamp grants evolve only ``r-x`` (no write) — so it is
    planted via ``sudo /usr/bin/touch``. The pod-wide ``{shared_dir}`` marker is
    evolve-owned, so it is written directly (``via_sudo=False``). The marker is an
    empty, non-secret file: it is NOT chmod 0600'd and does not touch the read ACL
    or the #3198/#3200 group/other clamp — purely additive. Best-effort: a sudo
    failure (e.g. sudoers not yet refreshed) logs and returns False, never raises.
    """
    if not enabled or _PROFILE.name != "macos":
        return False
    marker = parent / NEVER_INDEX_MARKER
    # EACCES probing a clamped parent (Py3.12 .exists() raises) → fall through;
    # sudo /usr/bin/touch is itself idempotent (a no-op if the marker exists).
    with contextlib.suppress(OSError):
        if marker.exists():
            return False
    try:
        if via_sudo:
            # D-2: the marker's parent is the bot-owned ``.openclaw`` root, and root ``touch``
            # FOLLOWS a planted link on every component — a symlinked ``.openclaw`` would
            # root-create this file at an arbitrary path. Dir-shaped gate (the parent is a
            # directory), so ``redirect_refusal``, not ``sudo_dest_refusal``.
            if why := redirect_refusal(marker):
                _log.info("plant_never_index_marker: refusing %s — %s", marker, why)
                return False
            proc = subprocess.run(
                ["sudo", "/usr/bin/touch", str(marker)],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                _log.info(
                    "plant_never_index_marker: sudo touch %s failed (%s) — "
                    "sudoers may need refresh-sudoers; non-fatal",
                    marker, proc.stderr.strip()[:120],
                )
                return False
            return True
        marker.touch(exist_ok=True)
        return True
    except OSError as e:
        _log.info("plant_never_index_marker: could not plant %s: %s (non-fatal)", marker, e)
        return False


# ── Part C1: pod-wide deploy lock ─────────────────────────────────────────────
# One advisory flock so a manual web upgrade and the scheduled repo-puller
# redeploy sweep can't run their recursive perm-passes concurrently and double-
# hammer a starved box (the 2026-06-24 incident: a manual upgrade racing the
# puller sweep, no lock between them — every deploy subprocess stalled past its
# hardcoded timeout). Same idiom as the per-bot scan lock in web/server.py and
# store_lock.py — LOCK_EX | LOCK_NB so a second acquirer takes the "already
# running" branch instead of blocking. flock auto-releases on fd close / process
# death, so a crashed deploy can never leave a stale lock.
DEPLOY_LOCK_FILE_NAME = "deploy.lock"

# Returned by try_acquire_deploy_lock when the lock FILE itself can't be opened
# (e.g. a misconfigured shared_dir). DISTINCT from None (held by another deploy):
# the caller treats this sentinel as "acquired, unlocked" and PROCEEDS — a
# transient FS glitch must not wedge every deploy forever (fail-open).
_DEPLOY_LOCK_UNLOCKED = object()


def try_acquire_deploy_lock(shared_dir: "Path | None" = None):
    """Try to take the pod-wide deploy lock NON-BLOCKING.

    Returns the open file handle holding the flock on success — the caller MUST
    keep it open for the duration of the deploy critical section and release it
    via :func:`release_deploy_lock` (or let the process die) to release. Returns
    ``None`` when another deploy already holds the lock. Returns the
    ``_DEPLOY_LOCK_UNLOCKED`` sentinel (truthy) when the lock file can't be opened
    at all — fail-open, so a transient FS glitch can't block every deploy.

    Scope the held handle to the deploy/redeploy critical section only — NOT
    around git pull / status reads (see the repo-puller caller).
    """
    sd = Path(shared_dir or _CANONICAL_SHARED_DIR)
    lock_path = sd / DEPLOY_LOCK_FILE_NAME
    try:
        fh = open(lock_path, "a+")
    except OSError as e:
        _log.warning(
            "try_acquire_deploy_lock: cannot open %s (%s) — proceeding WITHOUT the deploy lock",
            lock_path, e,
        )
        return _DEPLOY_LOCK_UNLOCKED
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        with contextlib.suppress(OSError):
            fh.close()
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            return None  # held by another deploy
        _log.warning(
            "try_acquire_deploy_lock: flock on %s failed (%s) — proceeding WITHOUT the lock",
            lock_path, e,
        )
        return _DEPLOY_LOCK_UNLOCKED
    # Stamp pid for `lsof`-free "who holds it?" debugging — not used for liveness
    # (flock is). Best-effort.
    with contextlib.suppress(OSError):
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    return fh


def release_deploy_lock(handle) -> None:
    """Release a handle returned by :func:`try_acquire_deploy_lock`.

    Safe to call with ``None`` (held — nothing to release) or the fail-open
    sentinel (nothing locked). flock also releases on fd close / process death.
    """
    if handle is None or handle is _DEPLOY_LOCK_UNLOCKED:
        return
    with contextlib.suppress(OSError):
        handle.close()  # closing the fd releases the flock (no explicit LOCK_UN)


@contextlib.contextmanager
def deploy_lock(shared_dir: "Path | None" = None):
    """Context manager around :func:`try_acquire_deploy_lock`. Yields the handle
    (``None`` ⇒ another deploy holds the lock; otherwise acquired — a real fh or
    the fail-open sentinel) and releases it on exit::

        with deploy_lock(shared_dir) as lk:
            if lk is None:
                ...   # skip — another deploy is in progress
            else:
                ...   # run the deploy critical section
    """
    handle = try_acquire_deploy_lock(shared_dir)
    try:
        yield handle
    finally:
        release_deploy_lock(handle)
