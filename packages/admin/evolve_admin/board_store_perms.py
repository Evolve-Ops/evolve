"""board_store_perms.py — the Board store belongs to the daemon that reads it.

WHY THIS MODULE EXISTS (incident, first live phone test, 2026-09-04).
``sudo evolve-admin board token <bot>`` runs as **root**.  ``mint_token``
wrote ``{sharedDir}/boards/<bot>/token.sha256`` with a plain
``tempfile.mkstemp`` + ``os.replace``, so the hash landed root-owned at mode
0600 — and the admin daemon, which runs as ``evolve``
(``deploy.EVOLVE_SERVICE_USER``), could not open it.  ``verify_token``'s
``except OSError: return False`` swallowed the ``PermissionError``
indistinguishably from "no token minted", so every ``GET /board/<bot>?t=…``
answered the ordinary 401 — and each of those failures also charged
``routes_board._auth_fail_limiter``, which then refused the *correct* token
for the rest of the window.  Two silences stacked, on a freshly minted link,
with nothing in any log.  The operator repaired it by hand with
``chown -R evolve:wheel``.

The fix has three legs, and the first is the one that matters:

  1. **A root writer adopts what it writes.**  :func:`adopt` chowns the
     directories and files a root-run mint/revoke/save touches to
     ``evolve:<admin_group>``, so the mint CLI structurally cannot produce a
     store the daemon cannot read.  Running as ``evolve`` it is a no-op —
     the files are already the daemon's.
  2. **An unreadable store is a logged coverage gap, not a 401.**
     ``board_store.verify_token`` separates *no hash file* (unminted or
     revoked — a real, fail-closed 401) from *hash file present but
     unreadable*, warns once per bot per process, and
     ``routes_board._auth`` does not charge the failed-auth limiter for the
     second case: the client did nothing wrong.
  3. **The pod re-verifies it.**  :func:`check_board_store` is one
     ``_PermCheck`` per bot with a board dir, run by
     ``deploy.ensure_pod_perms`` on every deploy and hourly (check-only) by
     ``pod_perms_drift_monitor`` — the same shape as
     ``bot_shared_subdirs.check_bot_shared_subdirs`` (#3992).

NO NEW SUDOERS GRANT, DELIBERATELY.  ``evolve-admin ensure-pod-perms``
already refuses to apply anything unless it is running as root (cli.py), and
that is the command the warning names, so the repair here is a direct
``os.chown`` — no shell, no ``sudo``, no widening of what the service user
may do.  A pass running as ``evolve`` reports the drift and says which
command repairs it, rather than shelling out to a sudo it has no grant for.
"""
from __future__ import annotations

import grp
import logging
import os
import pwd
from pathlib import Path
from typing import TYPE_CHECKING

from evolve_util import assert_no_symlink_in_path as _assert_no_symlink
from platform_profile import get_profile as _get_profile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .deploy import _PermCheck

log = logging.getLogger(__name__)

#: ``_PermCheck.category`` for everything this module reports.
PERM_CHECK_CATEGORY = "board-store"

#: The one command an operator runs to repair a drifted board store. Named in
#: the drift check, in the fix description, and in the ``verify_token``
#: warning, so all three say the same thing.
REPAIR_COMMAND = "sudo evolve-admin ensure-pod-perms"


def _geteuid() -> int:
    """Indirection over ``os.geteuid()`` so tests can act like root.

    Same shape as ``repo_puller._geteuid`` — the root branch below is the
    entire point of this module and must be exercisable without one.
    """
    return os.geteuid()


def daemon_user() -> str:
    """The account the admin daemon runs as (and so the board store's owner)."""
    # Lazy: deploy imports the admin package widely and this module is
    # imported from board_store, which the web layer loads on every request
    # path. Same reason as bot_shared_subdirs._evolve_user.
    from .deploy import EVOLVE_SERVICE_USER
    return EVOLVE_SERVICE_USER


def _daemon_ids() -> "tuple[int, int] | None":
    """``(uid, gid)`` for ``evolve:<admin_group>``, or None if unresolvable.

    ``admin_group`` comes from the platform profile (``wheel`` on macOS,
    ``root`` on Linux — ``wheel`` is not gid 0 there and may not exist), the
    same source every ``chown`` in ``deploy`` routes through.
    """
    user = daemon_user()
    try:
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(_get_profile().admin_group).gr_gid
    except KeyError:
        # A dev box or a test fixture with no `evolve` account: nothing to
        # adopt to. Never raise out of a write path over this.
        return None
    return uid, gid


def _redirect_safe(path: Path) -> bool:
    """Refuse to chown a path that resolves through a planted symlink.

    The chowns below run as root, so a link anywhere on the way to the board
    dir would hand an attacker-chosen inode to the daemon user. Same gate,
    same reason, as ``bot_shared_subdirs._redirect_safe``.
    """
    try:
        _assert_no_symlink(path)
        return True
    except Exception as e:  # noqa: BLE001 - the gate reports, never raises out
        log.error(
            "board store %s: refusing the ownership repair — %s. Inspect it by "
            "hand (`ls -ld`); nothing is chowned through a redirected path.",
            path, e,
        )
        return False


def adopt(*paths: "Path | None") -> None:
    """Hand each existing path to the daemon user — only when we are root.

    The mint/revoke/save entry points are reachable two ways: in-process as
    the ``evolve`` daemon (nothing to do — the files are already ours) and
    from ``sudo evolve-admin board …`` as root (everything just written is
    root-owned, and a 0600 token hash is then unreadable by the daemon that
    has to verify it). This is the second case, and it is best-effort by
    construction: a failed chown must never cost the operator the token that
    was just printed, so every failure is a WARNING on the record rather than
    an exception out of a write path.
    """
    if _geteuid() != 0:
        return
    ids = _daemon_ids()
    if ids is None:
        log.warning(
            "board store: cannot resolve the %s account — files written as "
            "root stay root-owned and the admin daemon will not be able to "
            "read them", daemon_user(),
        )
        return
    uid, gid = ids
    for path in paths:
        if path is None or not os.path.lexists(path):
            continue
        p = Path(path)
        if not _redirect_safe(p):
            continue
        try:
            # follow_symlinks=False: chown the object named, never whatever a
            # link at the leaf points at.
            os.chown(p, uid, gid, follow_symlinks=False)
        except OSError as e:  # noqa: BLE001 - never fatal to a mint
            log.warning(
                "board store: could not give %s to %s (%s) — the admin daemon "
                "may not be able to read it; run `%s`",
                p, daemon_user(), e, REPAIR_COMMAND,
            )


# ── drift check (ensure_pod_perms / pod_perms_drift_monitor) ────────────────


def _owner_of(path: Path) -> "str | None":
    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (KeyError, OSError):
        return None


def _wrong_owner(root: Path, expected: str) -> "list[Path]":
    """Every entry at or under ``root`` that is not owned by ``expected``.

    Walks without following links (``os.walk`` defaults to
    ``followlinks=False``): a board store is small — one JSON, one token
    hash, one JSONL per day — so a full walk is cheap and catches the file
    that actually broke the phone test, which a dir-only check would miss.
    """
    bad: list[Path] = []
    if _owner_of(root) not in (expected, None):
        bad.append(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            p = Path(dirpath) / name
            if _owner_of(p) not in (expected, None):
                bad.append(p)
    return bad


def check_board_store(shared_dir: Path, bot_id: str) -> "list[_PermCheck]":
    """One ``_PermCheck`` for this bot's board store: the daemon owns it.

    A bot with no board dir is an informational pass — most bots never get a
    board. A bot WITH one must own it as ``evolve``, dir and contents both,
    or the daemon that serves ``/board/<bot>`` cannot read the token hash it
    is asked to verify against.
    """
    from .board_store import board_dir  # lazy: board_store imports adopt()
    from .deploy import _PermCheck  # lazy: deploy imports this module at load

    try:
        target = board_dir(shared_dir, bot_id)
    except ValueError:  # a bot id that could never be a board dir
        return []
    if not target.is_dir():
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=str(target), ok=True,
            detail="(no board minted for this bot — nothing to enforce)",
        )]
    if not _redirect_safe(target):
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=str(target), ok=False,
            detail="path resolves through a non-root symlink or hard link",
            fix_description=(
                f"inspect {target} by hand (`ls -ld`) — the self-heal must "
                f"never be the thing that lands a chown on an attacker-chosen "
                f"path. Remove the link, restore the real directory, then "
                f"re-run `{REPAIR_COMMAND}`."
            ),
            apply=None,
        )]

    user = daemon_user()
    bad = _wrong_owner(target, user)
    if not bad:
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=str(target), ok=True,
        )]
    shown = ", ".join(str(p.relative_to(target.parent)) for p in bad[:5])
    more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
    return [_PermCheck(
        category=PERM_CHECK_CATEGORY, target=str(target), ok=False,
        detail=(
            f"{len(bad)} path(s) not owned by {user}: {shown}{more} — the "
            f"daemon cannot read a 0600 token hash it does not own, and every "
            f"board request 401s"
        ),
        fix_description=(
            f"chown -R {user}:{_get_profile().admin_group} {target} "
            f"(runs in-process as root; if this pass is not root, run "
            f"`{REPAIR_COMMAND}`)"
        ),
        apply=lambda: repair_board_store(target),
    )]


def repair_board_store(target: Path) -> bool:
    """Give the whole board store back to the daemon user. Root only.

    Returns False (with a log line naming the operator command) when the pass
    is not running as root — ``ensure-pod-perms`` in apply mode already
    requires root, so that is the supported path; a daemon-triggered pass
    reports the drift instead of shelling out to an ungranted ``sudo``.
    """
    if _geteuid() != 0:
        log.warning(
            "board store %s: ownership repair needs root — run `%s` on the pod "
            "host", target, REPAIR_COMMAND,
        )
        return False
    ids = _daemon_ids()
    if ids is None or not _redirect_safe(target):
        return False
    paths: list[Path] = [target]
    for dirpath, dirnames, filenames in os.walk(target):
        paths.extend(Path(dirpath) / n for n in list(dirnames) + list(filenames))
    adopt(*paths)
    return not _wrong_owner(target, daemon_user())
