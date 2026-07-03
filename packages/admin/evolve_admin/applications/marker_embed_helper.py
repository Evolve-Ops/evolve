#!/usr/bin/env python3
"""marker_embed_helper.py — the narrow, single-purpose privileged write behind
the Sync Applications "Fix" (stamp / rewrite marker) action for **bot-owned**
workspace files.

Why this exists
───────────────
The admin server runs as the ``evolve`` user, which has macOS/Linux ACL **read**
(but not write) on a bot's ``.openclaw/workspace`` tree outside
``workspace/evolve/``. So ``provenance.embed_marker``'s in-place atomic write
(temp-file + rename inside the file's directory) throws ``PermissionError`` for
bot-owned scripts (``scripts/*.py``, ``*-cron.sh``, …). The handler then had to
punt a copy-paste ``manual_cli`` for the operator to run over SSH — a "just
works" violation.

This helper is the privileged half of the fix, run under a single fixed-path
sudoers grant (``setup_wizard._render_evolve_sudoers`` §11h). The unprivileged
half (in ``web/server.py``) computes the fully-marked file content as ``evolve``
(``provenance.render_marked_text`` — ACL read is enough) and stages it to
``/tmp``; this helper, as root, **applies that staged file to one validated
workspace path**. Its only job is "copy this staged file to this destination,
owned by the bot" — it is deliberately incapable of writing arbitrary content
to arbitrary paths.

Security boundary (the helper validates everything itself — it does NOT lean on
the caller's pre-checks):

  • **expected-bot binding** — the caller passes the bot id; the resolved
    destination MUST land in exactly that bot's workspace, so a redirect into
    another bot's tree is refused by the helper, not just the server gate.
  • **containment + ownership** — the destination's canonical path must live
    under ``{user_home_root}/<bot>/.openclaw/workspace/`` AND its
    workspace-relative form must satisfy ``app_ownership_policy.can_app_own``
    (no secret / telemetry / OpenClaw-standard / scanner-state path).
  • **race-free, symlink-proof write** — the destination's parent directory is
    opened component-by-component with ``O_NOFOLLOW`` (refusing ANY symlinked
    path component) and the create / chmod / chown / rename all operate relative
    to that held directory fd. This closes the validate-then-write TOCTOU: a
    post-validation rename of a path *name* cannot redirect the write (the fd is
    bound to the real inode), and a swapped-in symlink fails the ``O_NOFOLLOW``
    open. The leaf itself must already exist as a **regular file** (never
    created) and must NOT be a symlink.
  • **staged source** — must be a regular, non-symlink file under the temp dir
    carrying a provenance marker (so the helper can't be repurposed to overwrite
    a file with arbitrary unmarked content).
  • **owner / mode** — the write preserves the destination's prior mode (these
    are code files, not 0600 secrets — never clamped) and chowns to
    ``<bot>:staff`` so the bot is never locked out of its own file (the #2781
    root-owned-file class).

Exit codes: ``0`` success; ``2`` validation/usage failure; ``1`` I/O failure.
Any non-zero exit lets the caller fall back to the legacy ``manual_cli`` hint.

This module is invoked ONLY as a script (``sudo <venv_python>
<deploy_checkout>/packages/admin/evolve_admin/applications/marker_embed_helper.py
<staged_tmp> <dest> <bot_id>``). Run as a script — not imported — so the heavy
``app_ownership_policy`` import (it pulls the scanner) happens once per Fix click,
never at admin-server import time.
"""

from __future__ import annotations

import contextlib
import grp
import os
import pwd
import stat
import sys
import tempfile
from pathlib import Path


def _fail(msg: str, code: int = 2) -> "int":
    """Print an error to stderr and return the exit code (caller exits with it)."""
    print(f"marker_embed_helper: {msg}", file=sys.stderr)
    return code


# ── Relative location of this script within a deploy checkout ─────────────────
# The sudoers grant (setup_wizard._render_evolve_sudoers §11h) and the
# unprivileged invoker below MUST name the byte-identical absolute path or sudo
# falls to a password prompt and the privileged write silently fails. Both
# derive it from this one relative constant + ``platform_profile``'s deploy
# checkout, so they cannot drift.
HELPER_RELPATH = "packages/admin/evolve_admin/applications/marker_embed_helper.py"


def helper_script_path() -> str:
    """Absolute path to this script in the deploy checkout — the exact string the
    §11h sudoers grant is rendered with (so ``sudo`` matches it)."""
    from platform_profile import get_profile

    return f"{get_profile().deploy_checkout_default}/{HELPER_RELPATH}"


def embed_marker_privileged(
    target: "Path",
    *,
    new_text: str,
    bot_id: str,
) -> "tuple[bool, str]":
    """Apply already-rendered marked content to a **bot-owned** workspace file,
    server-side. The unprivileged (``evolve``) half of the Fix action.

    The caller (``web/server.py``) has already computed ``new_text`` via
    ``provenance.render_marked_text`` (ACL read is enough). This stages it to
    ``/tmp`` and invokes :func:`main` as root through the §11h sudoers grant,
    binding the write to ``bot_id``'s workspace.

    Returns ``(ok, detail)``. ``ok=False`` means the privileged path was
    unavailable or refused — the caller should fall back to the legacy
    ``manual_cli`` hint. NEVER raises for an expected failure (missing grant,
    EACCES, validation refusal); those come back as ``(False, detail)``.
    """
    import subprocess

    from ..config import scanner_python

    helper = helper_script_path()
    if not os.path.exists(helper):
        return False, f"marker_embed_helper.py not found at {helper}"

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-marker-", suffix=target.suffix or ".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        try:
            proc = subprocess.run(
                ["sudo", "-n", scanner_python(), helper, tmp, str(target), bot_id],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.SubprocessError as e:
            return False, f"privileged helper invocation failed: {e}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            return False, f"privileged helper exit {proc.returncode}: {detail}"
        return True, ""
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


# ── Root-side validation + write ──────────────────────────────────────────────


def _staged_source_ok(staged: str) -> "tuple[bool, str]":
    """Validate the /tmp-staged source: absolute, under the temp dir, a regular
    non-symlink file carrying a provenance marker. Returns (ok, reason)."""
    if not os.path.isabs(staged):
        return False, f"staged source {staged!r} is not absolute"
    # The staged file must resolve under a temp dir — bounds where the
    # to-be-written content can originate. The invoker stages to ``/tmp``
    # (mkstemp(dir="/tmp")); allow both that and the platform temp dir, each
    # canonicalized (on macOS ``/tmp`` is a symlink to ``/private/tmp`` and
    # ``gettempdir()`` is a per-user ``/var/folders/...`` path). realpath
    # collapses any symlink in the path, so a /tmp symlink pointing elsewhere
    # fails this containment check.
    tmp_roots = {os.path.realpath("/tmp"), os.path.realpath(tempfile.gettempdir())}
    real_staged = os.path.realpath(staged)
    if not any(real_staged == r or real_staged.startswith(r + os.sep) for r in tmp_roots):
        return False, f"staged source {staged!r} is not under a temp dir ({sorted(tmp_roots)})"
    try:
        st = os.lstat(staged)  # lstat: refuse a symlink AT the staged path
    except OSError as e:
        return False, f"staged source unreadable: {e}"
    if stat.S_ISLNK(st.st_mode):
        return False, "staged source is a symlink (refused)"
    if not stat.S_ISREG(st.st_mode):
        return False, "staged source is not a regular file"
    # The staged content must itself be a marked file — the helper applies
    # provenance markers, it is not a general file-overwrite primitive.
    try:
        from evolve_admin.applications.provenance import _MARKER_RE
        text = Path(staged).read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 — any read/import failure is a refusal
        return False, f"cannot read staged source: {e}"
    if not (_MARKER_RE.search(text) or '"_evolve"' in text):
        return False, "staged source carries no provenance marker (refused)"
    return True, ""


def _secure_walk_to_parent(
    dest: str, expected_bot: str,
) -> "tuple[int | None, str, str, str]":
    """Validate ``dest`` and open its parent directory WITHOUT following any
    symlink in the path. Returns ``(parent_fd, leaf, bot_user, reason)``;
    ``parent_fd`` is ``None`` on any failure (caller must ``os.close`` it on
    success).

    Validation: ``dest`` absolute, no ``.``/``..`` components; its canonical
    path lives under ``{user_home_root}/<bot>/.openclaw/workspace/``; the bot
    matches ``expected_bot``; the workspace-relative path is ``can_app_own``;
    the leaf exists as a regular, non-symlink file.

    Race-free write target: the parent is opened component-by-component from the
    (canonicalized) home root with ``O_NOFOLLOW`` on every component, so the
    returned fd is bound to the real inode of the validated directory. A
    post-validation name swap can't redirect the later write, and a swapped-in
    symlink fails the open. This is what makes the helper — not the server gate —
    the actual containment boundary.
    """
    from platform_profile import get_profile
    from evolve_admin.applications.app_ownership_policy import can_app_own

    if not os.path.isabs(dest):
        return None, "", "", f"destination {dest!r} is not absolute"

    # Canonicalize ONLY the parent chain (handles /var→/private/var etc. and any
    # legitimate symlinked ancestor) — NOT the leaf, which we lstat in place so a
    # symlink there is refused rather than followed.
    leaf = os.path.basename(dest.rstrip(os.sep))
    if not leaf or leaf in (".", ".."):
        return None, "", "", f"destination {dest!r} has no usable leaf name"
    real_parent = os.path.realpath(os.path.dirname(os.path.abspath(dest)))
    real_dest = os.path.join(real_parent, leaf)

    home_root = os.path.realpath(get_profile().user_home_root)
    try:
        rel_to_home = Path(real_dest).relative_to(home_root)
    except ValueError:
        return None, "", "", f"destination {real_dest!r} is not under {home_root}"
    parts = rel_to_home.parts
    if ".." in parts or "." in parts:
        return None, "", "", f"destination {real_dest!r} contains '.'/'..'"
    # Expect: <bot>/.openclaw/workspace/<rel...>
    if len(parts) < 4 or parts[1] != ".openclaw" or parts[2] != "workspace":
        return None, "", "", (
            f"destination {real_dest!r} is not under a bot "
            ".openclaw/workspace/ tree"
        )
    bot_user = parts[0]
    if not bot_user or bot_user in (".", ".."):
        return None, "", "", f"could not derive bot from {real_dest!r}"
    # Bind to the caller's intended bot — refuse a redirect into another bot's
    # workspace at the helper, not just the server.
    if expected_bot and bot_user != expected_bot:
        return None, "", "", (
            f"destination bot {bot_user!r} != expected {expected_bot!r}"
        )
    rel = os.sep.join(parts[3:])  # workspace-relative path
    if not can_app_own(rel, name=leaf):
        return None, "", "", (
            f"destination {rel!r} is not an application-ownable path "
            "(platform telemetry, scanner/manifest state, secret/runtime "
            "file, or OpenClaw-standard file)"
        )

    # Open the parent dir chain with O_NOFOLLOW at every step. real_parent is
    # already canonical (no symlinks), so these opens succeed in the happy case;
    # a component swapped to a symlink after the realpath above fails the open.
    dir_parts = parts[:-1]  # <bot>/.openclaw/workspace/.../<leaf-parent>
    try:
        fd = os.open(home_root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        return None, "", "", f"cannot open home root {home_root!r}: {e}"
    try:
        for comp in dir_parts:
            try:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                os.close(fd)
                return None, "", "", (
                    f"cannot open path component {comp!r} (symlink or missing?): {e}"
                )
            os.close(fd)
            fd = nfd
        try:
            st = os.lstat(leaf, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None, "", "", f"destination leaf {leaf!r} not found"
        except OSError as e:
            os.close(fd)
            return None, "", "", f"cannot stat leaf {leaf!r}: {e}"
        if stat.S_ISLNK(st.st_mode):
            os.close(fd)
            return None, "", "", "destination is a symlink (refused)"
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None, "", "", "destination is not a regular file"
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return fd, leaf, bot_user, ""


def _apply(parent_fd: int, leaf: str, bot_user: str, staged: str) -> "tuple[bool, str]":
    """Write the staged content to ``leaf`` inside the held ``parent_fd``
    directory, preserving the prior mode and chowning to ``<bot>:staff``. Atomic
    via a same-dir temp + ``os.replace`` — all operations are dir-fd-relative so
    they cannot be redirected by a path-name swap (TOCTOU-safe)."""
    try:
        uid = pwd.getpwnam(bot_user).pw_uid
    except KeyError:
        return False, f"unknown bot user {bot_user!r}"
    try:
        gid = grp.getgrnam("staff").gr_gid
    except KeyError:
        # `staff` should exist on macOS and Ubuntu; fall back to the bot's
        # primary group so the file is never left root-owned.
        gid = pwd.getpwnam(bot_user).pw_gid

    # Preserve the destination's prior mode — these are code files (a .py at
    # 0644, a cron .sh at 0755), NOT 0600 secrets, so they must not be clamped.
    try:
        prior_mode = os.lstat(leaf, dir_fd=parent_fd).st_mode & 0o777
    except OSError as e:
        return False, f"cannot stat destination mode: {e}"

    # Read the staged source (O_NOFOLLOW: never follow a symlink there).
    try:
        sfd = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(sfd, "rb") as f:
            data = f.read()
    except OSError as e:
        return False, f"cannot read staged source: {e}"

    tmpname = f".marker-embed-{os.getpid()}-{os.urandom(6).hex()}"
    try:
        wfd = os.open(
            tmpname,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as e:
        return False, f"cannot create temp in destination dir: {e}"
    try:
        try:
            os.write(wfd, data)
            os.fchmod(wfd, prior_mode)
            os.fchown(wfd, uid, gid)
        finally:
            os.close(wfd)
        os.replace(tmpname, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        return True, ""
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmpname, dir_fd=parent_fd)
        return False, f"write failed: {e}"


def main(argv: "list[str]") -> int:
    if len(argv) != 4:
        return _fail(f"usage: {argv[0]} <staged_tmp> <dest_path> <bot_id>")
    staged, dest, expected_bot = argv[1], argv[2], argv[3]

    ok, reason = _staged_source_ok(staged)
    if not ok:
        return _fail(reason)

    parent_fd, leaf, bot_user, reason = _secure_walk_to_parent(dest, expected_bot)
    if parent_fd is None:
        return _fail(reason)
    try:
        ok, reason = _apply(parent_fd, leaf, bot_user, staged)
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_fd)
    if not ok:
        return _fail(reason, code=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
