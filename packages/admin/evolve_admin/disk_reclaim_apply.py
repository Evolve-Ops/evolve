"""disk_reclaim_apply — the privileged reaper half of pod-host disk hygiene.

PR2 of docs/spec-delta-disk-reclaim-2026-06-21.md. :mod:`disk_reclaim`
(PR1) *sizes* what is reclaimable and stays pure (no sudo, no subprocess);
this module *acts* on it. The one-click reaper reclaims **only the
regenerable npm caches** (``<home>/.npm/_cacache`` + ``_npx``) under the
narrow ``/bin/rm`` grants rendered by ``setup_wizard._render_evolve_sudoers``
(§3.2).

Logs are deliberately NOT reclaimable here
------------------------------------------
The PR1 scanner still *reports* the oversized-log categories
(``evolve_logs_oversized``, ``oc_rotated_logs``) — read-only sizing is
fine and operators still see the numbers — but the reaper refuses to act
on them. ``truncate -s 0 <path>`` follows symlinks in BOTH the
intermediate and final path components, which makes a sudo ``truncate``
grant a root *arbitrary-file-zero* primitive (a compromised bot can
``rename()`` its ``~/.evolve/logs`` into a symlink mid-window and steer
root at another account's ``auth-profiles.json``). Logs are also the
smaller win (~0.4 GB) and are already bounded at source (#3073 mcp-bridge
rotation + the daily ``log_cap`` job), so the audit (Option B) dropped the
truncate primitive entirely. npm caches (~13.7 GB) are the real reclaim.

Single source of truth
-----------------------
Every glob / cap / category / method comes from :mod:`disk_reclaim`. The
reaper re-scans with :func:`disk_reclaim.scan_reclaimable` *at reclaim
time* and acts ONLY on the paths that fresh scan returns — closing the
TOCTOU window between the operator seeing a breakdown and clicking
"Reclaim space", and never trusting a path handed in by the client.

Defence against the root-level symlink TOCTOU (the auditor-grade part)
----------------------------------------------------------------------
A sudo ``rm`` grant re-resolves its operand *string* at root's exec time,
and ``rm -rf`` follows a symlink in any INTERMEDIATE component. So handing
``sudo rm`` an absolute path that a compromised bot can race (swap ``.npm``
for a symlink between our check and rm's resolution) lets root delete
across accounts. We close that window structurally:

  * a cheap in-process gate (:func:`_classify_path`) rejects anything that
    isn't the exact ``<home>/.npm/{_cacache,_npx}`` shape, contains
    ``..``/``.``, or has a symlink in any component — an early reject, NOT
    the only gate;
  * the authoritative gate (:func:`_open_verified_parent`) walks
    ``home → .npm`` with ``openat``/``O_NOFOLLOW`` and HOLDS the verified
    ``.npm`` dirfd. Any symlinked component fails the open (``ELOOP``);
  * the delete runs as ``rm -rf -- <leaf>`` with a BARE leaf name and the
    child's cwd pinned to that held dirfd via ``fchdir`` (preexec) — so the
    operand root resolves carries no directory at all, and the leaf is
    resolved inside the exact verified inode. A rename of ``.npm`` to a
    symlink after the walk cannot redirect the held fd, and ``rm`` does not
    follow a symlink in its *final* operand, so a swapped leaf is unlinked,
    not traversed.

So even a buggy or compromised scanner — or a bot racing the reaper —
cannot make root escape the bot's verified ``.npm`` directory.

Tri-state / best-effort, like the scanner: a per-path failure is recorded
in ``errors`` and flips the category's ``partial`` flag — never a silent
success and never a raise into the caller.
"""
from __future__ import annotations

import os
import stat
import subprocess
from typing import Any

from . import disk_reclaim
from .disk_reclaim import (
    CAT_NPM_CACHE,
    CATEGORY_LABELS,
    CATEGORY_METHOD,
    DEFAULT_ROOTS,
    human_bytes,
)
from .runtime import is_sudo_escalation_error

# Privileged binary — literal matching the §3.2 ``/bin/rm`` grants. The path
# is identical across the macOS/Linux platform tables, so a literal can't
# drift from the grant the way a per-call profile lookup might; the grant
# render still pins it from platform_profile.commands (single source on the
# GRANT side).
_RM = "/bin/rm"

# npm cache leaf-dir names (the only things `rm` may ever touch). These are the
# exact operands granted by §3.2 (`rm -rf -- _cacache` / `_npx`).
_NPM_LEAVES = ("_cacache", "_npx")

# Only the npm cache is one-click reclaimable. The scanner still reports the
# log categories (read-only sizing), but the reaper refuses to truncate them
# — see the module docstring (audit Option B: no truncate primitive).
RECLAIMABLE_CATEGORIES = frozenset({CAT_NPM_CACHE})

# Reason surfaced when a caller explicitly asks to reclaim a log category.
_LOG_SKIP_REASON = "logs are bounded at source; not one-click reclaimable"

# openat flags for the verified walk: O_NOFOLLOW makes the open of a symlink
# component fail with ELOOP, O_DIRECTORY rejects a non-directory, O_CLOEXEC so
# the held fd never leaks into an unrelated exec.
_WALK_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)


# ── Privileged-op seam (injectable for tests) ─────────────────────────────────
#
# The default runner shells out to sudo. Tests pass a fake runner with the
# same one-method surface so a real `rm` never runs under pytest. Injecting an
# object (rather than monkeypatching module-level `subprocess`) keeps the seam
# stable across module moves — see memory:subprocess-patch-fakes-break-on-
# module-move.


class SudoReclaimRunner:
    """Default runner: ``sudo -n /bin/rm -rf -- <leaf>`` with cwd pinned to a
    verified parent dirfd.

    ``-n`` (non-interactive) so a missing/denied grant fails immediately with
    a classifiable "a password is required" stderr instead of hanging on a
    TTY prompt — same idiom as :mod:`oc_log_rotate`.

    The operand handed to ``rm`` is a BARE leaf name (``_cacache``/``_npx``),
    never a path. The child's working directory is pinned to ``parent_fd`` —
    the ``.npm`` dirfd the caller verified with ``O_NOFOLLOW`` — via
    ``os.fchdir`` in ``preexec_fn``, so ``rm`` resolves the leaf inside that
    exact inode. ``fchdir`` on a held fd cannot be redirected by a rename of
    the directory's name, and ``rm`` does not follow a symlink in its final
    operand, so root can only ever unlink the verified leaf.
    """

    def rm_leaf(self, parent_fd: int, leaf: str) -> tuple[bool, str]:
        # preexec_fn runs in the forked child just before exec. It does ONLY a
        # single fchdir (an async-signal-safe syscall) — deliberately nothing
        # that could deadlock after fork in a threaded server. pass_fds keeps
        # parent_fd open through subprocess's fd-closing so fchdir can't hit
        # EBADF; sudo's own closefrom then drops it before exec'ing rm, which
        # is harmless because the cwd is already set.
        def _pin_cwd() -> None:
            os.fchdir(parent_fd)

        try:
            r = subprocess.run(
                ["sudo", "-n", _RM, "-rf", "--", leaf],
                capture_output=True, text=True, timeout=120,
                preexec_fn=_pin_cwd, pass_fds=(parent_fd,),
            )
        except subprocess.TimeoutExpired:
            return False, "timed out"
        except OSError as e:
            return False, str(e)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip() or "command failed"
        return True, "ok"


# ── Path safety gate ──────────────────────────────────────────────────────────


def _home_and_subparts(path: str, roots) -> tuple[str, list[str]] | None:
    """Split a target into ``(home_abs, subparts)`` under the matching root.

    ``/Users/alice/.npm/_cacache`` under root ``/Users`` → ``("/Users/alice",
    [".npm", "_cacache"])``. Returns None for an exact-root path, a path not
    under any root, or a bare home with no sub-component.
    """
    for root in roots:
        r = str(root).rstrip("/")
        if path == r:
            return None
        if path.startswith(r + "/"):
            rel = path[len(r) + 1:].split("/")
            if len(rel) < 2:
                return None
            return r + "/" + rel[0], rel[1:]
    return None


def _relparts_under_roots(path: str, roots) -> list[str] | None:
    """Path components relative to the matching root, or None if not under one.

    ``/Users/alice/.npm/_cacache`` under root ``/Users`` → ``["alice", ".npm",
    "_cacache"]``. An exact-root or not-under-any-root path returns None.
    """
    for root in roots:
        r = str(root).rstrip("/")
        if path == r:
            return None
        if path.startswith(r + "/"):
            return path[len(r) + 1:].split("/")
    return None


def _classify_path(path: str, roots) -> str | None:
    """Return the category a path is a *valid reclaim target* for, else None.

    Cheap STRING-SHAPE gate — pure (no filesystem access), independent of the
    scanner. Recognises the npm cache shape only (the sole reclaimable
    category); rejects anything that isn't ``<home>/.npm/{_cacache,_npx}`` or
    contains ``..``/``.``. This is the early reject; the AUTHORITATIVE symlink
    + type rejection is the ``O_NOFOLLOW`` walk in
    :func:`_open_verified_parent`, which holds the verified fd across the
    delete. Two gates — never just one.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    rel = _relparts_under_roots(path, roots)
    if not rel:
        return None
    # No empty / dot / parent-dir segments anywhere (defends against "//",
    # trailing slash, and any `..` the scanner could never produce but the
    # gate must still refuse).
    if any(seg in ("", ".", "..") for seg in rel):
        return None
    home = rel[0]
    if not home or "/" in home:
        return None
    # npm cache: <home>/.npm/_cacache | _npx  — and nothing deeper.
    if len(rel) == 3 and rel[1] == ".npm" and rel[2] in _NPM_LEAVES:
        return CAT_NPM_CACHE
    return None


def _open_verified_parent(home_abs: str, subparts: list[str]) -> tuple[int | None, str]:
    """Open the leaf's parent dir via an ``O_NOFOLLOW`` walk; verify the leaf.

    ``subparts`` is the path below the home, e.g. ``[".npm", "_cacache"]``: all
    but the last component are *opened* (each ``openat`` with ``O_NOFOLLOW`` so
    a symlink fails ``ELOOP``), and the last component (the leaf ``rm`` will
    remove) is ``fstatat``-checked ``NOFOLLOW`` to be a real directory. The
    home itself is opened ``O_NOFOLLOW`` too.

    Returns ``(parent_fd, leaf)`` on success — the caller MUST ``os.close`` the
    fd — or ``(None, reason)`` on any rejection. The held fd pins the verified
    parent inode so the subsequent ``fchdir``-pinned delete cannot be
    redirected by a rename swapping ``.npm`` for a symlink mid-window.
    """
    if not subparts:
        return None, "no leaf component"
    *intermediate, leaf = subparts
    if not leaf or leaf in ("", ".", ".."):
        return None, f"bad leaf {leaf!r}"

    try:
        fd = os.open(home_abs, _WALK_FLAGS)
    except OSError as e:
        return None, f"open home {home_abs}: {e.__class__.__name__}"

    try:
        for comp in intermediate:
            if comp in ("", ".", ".."):
                return None, f"bad component {comp!r}"
            try:
                nfd = os.open(comp, _WALK_FLAGS, dir_fd=fd)
            except OSError as e:
                # ELOOP = symlink component (O_NOFOLLOW); ENOTDIR = not a dir.
                return None, f"reject {comp}: {e.__class__.__name__}"
            os.close(fd)
            fd = nfd
        # Verify the leaf without following it. Its type/symlink status is
        # belt-and-suspenders: even a leaf swapped to a symlink after this
        # check is only UNLINKED by `rm` (no final-operand follow), never
        # traversed — the security comes from the pinned parent fd.
        try:
            st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        except OSError as e:
            return None, f"stat leaf {leaf}: {e.__class__.__name__}"
        if not stat.S_ISDIR(st.st_mode):
            return None, f"leaf {leaf} not a directory"
    except BaseException:
        os.close(fd)
        raise
    return fd, leaf


# ── Public API ────────────────────────────────────────────────────────────────


def reclaim(categories=None, *, roots=DEFAULT_ROOTS, runner: Any = None) -> dict[str, Any]:
    """Reclaim space by acting on a FRESH scan of what's reclaimable.

    Re-scans with :func:`disk_reclaim.scan_reclaimable` and, for each npm-cache
    entry the scan reports, validates the path against :func:`_classify_path`,
    opens the verified parent dirfd (:func:`_open_verified_parent`), and
    ``rm -rf``s the leaf relative to that fd. Acts ONLY on freshly-scanned,
    gate-approved npm caches.

    Log categories are NOT reclaimable here (audit Option B). When ``None``,
    only npm caches are reclaimed and logs are silently left alone (the
    scanner still reports their size elsewhere). When a log category is
    *explicitly* requested it is recorded as ``skipped`` with a reason — never
    an error.

    ``categories`` — optional iterable of category ids to limit the run to a
    subset (unknown ids are ignored). ``None`` reclaims every *reclaimable*
    category.

    Returns::

        {
          "ok": bool,                 # True iff no per-path errors
          "freed_bytes": int,         # sum of acted-on entry sizes (best-effort)
          "per_category": [
            {"category", "label", "method", "freed_bytes", "acted", "partial",
             # log categories asked for explicitly also carry:
             "skipped": True, "reason": str},
            ...
          ],
          "errors": [{"path", "category", "error"}, ...],
        }

    Best-effort: a per-path failure is recorded in ``errors`` and flips that
    category's ``partial`` — it never raises and never reports a silent success.
    A skipped log category does NOT count as an error (``ok`` stays True).
    """
    runner = runner or SudoReclaimRunner()
    want = set(categories) if categories is not None else None

    scan = disk_reclaim.scan_reclaimable(roots)
    per_category: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total_freed = 0

    for cat in scan.get("categories", []):
        cat_id = cat.get("category")
        if cat_id not in RECLAIMABLE_CATEGORIES:
            # The reaper never touches logs. Only surface a skip record when it
            # was explicitly requested, so a default "reclaim all" run stays
            # quiet about the read-only-reported log categories.
            if want is not None and cat_id in want:
                per_category.append({
                    "category": cat_id,
                    "label": CATEGORY_LABELS.get(cat_id, cat_id),
                    "method": CATEGORY_METHOD.get(cat_id),
                    "freed_bytes": 0,
                    "acted": 0,
                    "partial": False,
                    "skipped": True,
                    "reason": _LOG_SKIP_REASON,
                })
            continue
        if want is not None and cat_id not in want:
            continue
        rec = {
            "category": cat_id,
            "label": CATEGORY_LABELS.get(cat_id, cat_id),
            "method": CATEGORY_METHOD.get(cat_id),
            "freed_bytes": 0,
            "acted": 0,
            # A partial scan means some subtree was unreadable: act on what we
            # found, but tell the operator the picture was incomplete.
            "partial": bool(cat.get("partial")),
        }
        for entry in cat.get("entries", []):
            path = entry.get("path")
            size = int(entry.get("bytes") or 0)
            # Cheap pre-check — never trust the scanner alone.
            if _classify_path(path, roots) != cat_id:
                rec["partial"] = True
                errors.append({"path": path, "category": cat_id, "error": "rejected by safety gate"})
                continue
            split = _home_and_subparts(path, roots)
            if split is None:
                rec["partial"] = True
                errors.append({"path": path, "category": cat_id, "error": "rejected by safety gate"})
                continue
            home_abs, subparts = split
            # Authoritative gate: O_NOFOLLOW walk that HOLDS the verified parent
            # dirfd across the delete. This is what closes the intermediate-
            # symlink race — _classify_path's lstat pass above can be raced.
            parent_fd, leaf = _open_verified_parent(home_abs, subparts)
            if parent_fd is None:
                rec["partial"] = True
                errors.append({"path": path, "category": cat_id, "error": f"privileged resolver rejected path ({leaf})"})
                continue
            try:
                ok, msg = runner.rm_leaf(parent_fd, leaf)
            finally:
                os.close(parent_fd)
            if ok:
                rec["freed_bytes"] += size
                rec["acted"] += 1
                total_freed += size
            else:
                rec["partial"] = True
                hint = ""
                if is_sudo_escalation_error(msg):
                    hint = " (sudo denied — run `sudo evolve-admin refresh-sudoers`)"
                errors.append({"path": path, "category": cat_id, "error": msg + hint})
        per_category.append(rec)

    return {
        "ok": not errors,
        "freed_bytes": total_freed,
        "per_category": per_category,
        "errors": errors,
    }


def _fresh_disk() -> dict[str, Any] | None:
    """Best-effort fresh disk section from host_health, for the endpoint reply.

    Lazy import so this module stays importable without the web/psutil stack
    (the reaper itself needs neither).
    """
    try:
        from .host_health import collect_host_health

        snap = collect_host_health()
        return snap.get("disk") if isinstance(snap, dict) else None
    except Exception:
        return None


def handle_reclaim_request(payload: dict[str, Any] | None, *, runner: Any = None) -> tuple[dict[str, Any], int]:
    """Web-handler logic for ``POST /api/host-health/reclaim`` (keeps server.py thin).

    Reads an optional ``categories`` list from the request body (a subset to
    reclaim; absent → all reclaimable). Runs :func:`reclaim`, then attaches a
    fresh disk reading and a human summary, and invalidates the per-poll scan
    cache so the next ``/api/host-health`` poll re-scans and the UI updates.
    Idempotent: re-running reclaims whatever is currently reclaimable.

    Returns ``(body, http_status)`` — 200 when every acted-on path succeeded,
    207 (multi-status) when some paths failed but the run otherwise completed.
    A skipped log category is not a failure, so a request that names only logs
    returns 200 with a ``skipped`` record.
    """
    payload = payload or {}
    requested = payload.get("categories")
    categories = None
    if isinstance(requested, list):
        # Only known category ids; silently drop anything else (never let an
        # operator-supplied string widen what gets touched). Log categories
        # are kept so reclaim() can surface the explicit-skip reason.
        known = set(CATEGORY_METHOD.keys())
        categories = [c for c in requested if c in known]

    result = reclaim(categories=categories, runner=runner)

    # The hot-path scan cache (read by /api/host-health) is now stale — reset
    # it so the operator's next poll reflects the freed space immediately.
    # (reset_scan_cache is a trivial global reset; it cannot raise.)
    disk_reclaim.reset_scan_cache()

    body = dict(result)
    body["freed_human"] = human_bytes(result["freed_bytes"])
    body["disk"] = _fresh_disk()
    status = 200 if result["ok"] else 207
    return body, status
