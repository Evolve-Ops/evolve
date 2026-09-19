"""Install + drift-check the PM probe's privileged half.

Brief: internal/dispatch/done/pm-mini-probe-readonly-mcp.md.

Two artifacts live here because both are only meaningful together with
``/etc/sudoers.d/pm-probe`` (rendered by :mod:`evolve_admin.pm_probe_sudoers`):

* **the root reader** — :mod:`evolve_admin.pm_probe_cat`, rendered for this
  platform and installed at :data:`WRAPPER_PATH`, root-owned, 0755. It is the
  ONLY thing the sudoers file grants root for reading files, and it enforces
  the operand contract that sudo's wildcards cannot (see that module's
  docstring for why the old ``cat`` depth ladder was not a bound at all).
* **the staging dir** — :data:`STAGING_DIR`, owned by the ADMIN account and
  0755, so a script the probe may run there (``python3 /tmp/pm/<name>.py``,
  an unprivileged but full interpreter) can only have been placed by the
  operator. ``/tmp`` is sticky and world-writable: if nobody owns ``/tmp/pm``,
  any bot user may create it and drop a script in it.

Both are enforced by ``evolve-admin ensure-pod-perms`` — but only once
``/etc/sudoers.d/pm-probe`` exists. A pod that never installed the probe's
privileged half gets an informational pass and no new files: the check follows
the grant rather than leading it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import pm_probe_cat

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .deploy import _PermCheck

SUDOERS_PATH = "/etc/sudoers.d/pm-probe"

WRAPPER_PATH = pm_probe_cat.INSTALL_PATH
WRAPPER_DIR = pm_probe_cat.INSTALL_DIR
WRAPPER_MODE = 0o755
WRAPPER_OWNER = "root"

# The PM's staging dir for operator-placed scripts. Owned by the admin
# account (not root): the probe runs those scripts UNPRIVILEGED, as the admin
# account, so admin ownership is what makes "operator-placed" true.
STAGING_DIR = "/tmp/pm"
STAGING_MODE = 0o755

# The one line the wrapper's source carries as a placeholder; the installed
# artifact is this file with that line rewritten from the platform profile.
_ROOT_LINE_RE = re.compile(r"^ROOT_PATTERNS: tuple\[str, \.\.\.\] = .*$", re.M)

# Profile-supplied path values must be plain enough to drop into a regex
# verbatim — no metacharacter, so the rendered pattern stays readable and no
# escaping subtlety can widen a root.
_PLAIN_PATH_RE = re.compile(r"^(?:/[A-Za-z0-9_-]+)+$")


def wrapper_root_patterns(profile) -> tuple[str, ...]:
    """The regexes the installed reader accepts, AFTER ``realpath``.

    Derived from ``platform_profile`` so the reader, the probe's own path set
    and the rest of the pod cannot disagree about where the trees are. The
    macOS twins matter: ``/tmp`` and ``/var`` are symlinks into ``/private``
    there, so every resolved path under them arrives with that prefix.
    """
    def plain(value: str) -> str:
        if not _PLAIN_PATH_RE.match(value):
            raise ValueError(
                f"{value!r} is not a plain path — pm-probe-cat's root patterns are "
                f"written verbatim into a regex and may not carry metacharacters"
            )
        return value

    shared = plain(profile.shared_dir_default)
    home = plain(profile.user_home_root)
    daemons = plain(profile.daemon_dir)
    pats = [
        # The pod's own trees: {shared_dir} and its `-repo` / `-venv` /
        # `-staging` / `-backup` siblings, one pattern rather than a per-tree list.
        rf"{shared}(?:-[A-Za-z0-9._]+)?(?:/.*)?",
        rf"{home}/[^/]+/\.openclaw(?:/.*)?",
        rf"{daemons}/ai\.evolve\.[A-Za-z0-9._-]+",
    ]
    for root in (plain(STAGING_DIR), "/var/log"):
        pats.append(rf"{root}(?:/.*)?")
        if profile.name == "macos":
            pats.append(rf"/private{root}(?:/.*)?")
    return tuple(pats)


def render_wrapper(profile=None) -> str:
    """The exact bytes installed at :data:`WRAPPER_PATH` for this platform."""
    if profile is None:
        from platform_profile import get_profile

        profile = get_profile()
    src = Path(pm_probe_cat.__file__).read_text(encoding="utf-8")
    pats = wrapper_root_patterns(profile)
    # repr() already escapes for a plain string literal; an `r` prefix here
    # would turn the pattern's `\\.` into two literal backslashes.
    body = "".join(f"    {p!r},\n" for p in pats)
    replacement = f"ROOT_PATTERNS: tuple[str, ...] = (\n{body})"
    rendered, n = _ROOT_LINE_RE.subn(lambda _m: replacement, src, count=1)
    if n != 1:
        raise ValueError(
            "pm_probe_cat.py no longer carries exactly one ROOT_PATTERNS line — "
            "the installed reader would keep an empty root set and refuse everything"
        )
    return rendered


def install_wrapper(profile=None) -> bool:
    """Write the rendered reader to :data:`WRAPPER_PATH`, root-owned 0755.

    Staged through a temp file and copied with sudo, the same shape as every
    other privileged write in this package. Returns True on success.
    """
    from platform_profile import get_profile

    profile = profile or get_profile()
    c = profile.commands
    content = render_wrapper(profile)
    fd, tmp = tempfile.mkstemp(prefix="pm-probe-cat-", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        steps = [
            # sudo-grant: ungranted-by-design: installing a root-owned reader is
            # an operator/root action; no service user may reach it.
            [c["mkdir"], "-p", WRAPPER_DIR],
            [c["cp"], tmp, WRAPPER_PATH],
            [c["chown"], f"{WRAPPER_OWNER}:{_root_group(profile)}", WRAPPER_PATH],
            [c["chmod"], oct(WRAPPER_MODE)[2:], WRAPPER_PATH],
            [c["chown"], f"{WRAPPER_OWNER}:{_root_group(profile)}", WRAPPER_DIR],
            [c["chmod"], "755", WRAPPER_DIR],
        ]
        for step in steps:
            # sudo-grant: ungranted-by-design: root-only install, operator-invoked.
            r = subprocess.run(["sudo"] + step, capture_output=True, text=True)
            if r.returncode != 0:
                return False
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError as exc:
                # The copy has already landed; a stale temp is untidy, not fatal.
                print(f"pm-probe: could not remove {tmp}: {exc}", file=sys.stderr)
    return True


def _root_group(profile) -> str:
    return "wheel" if profile.name == "macos" else "root"


def probe_sudoers_installed() -> bool:
    """True when the operator has installed the probe's sudoers file.

    Everything else here is gated on this: the reader and the staging dir are
    the grant's companions, not a new surface for pods that never opted in.

    ``/etc/sudoers.d`` is 0700 on Linux, where ``exists()`` does not answer
    False — it RAISES ``PermissionError``, which would take the whole
    ensure-pod-perms pass down for every non-root caller (the hourly
    check-only drift monitor, and every test that calls the pass). An
    unreadable grant dir reads as "not installed", and that is the right
    direction rather than a convenient one: a caller who cannot see
    ``/etc/sudoers.d`` is not root, so it could neither install the reader nor
    chown ``/tmp/pm`` — there is nothing for these two checks to do or report.
    The apply pass runs as root, where this answers truthfully.
    """
    try:
        return Path(SUDOERS_PATH).exists()
    except OSError:
        return False


def check_pm_probe_wrapper() -> "_PermCheck":
    """ensure-pod-perms: the root reader is present, current, root-owned, 0755.

    A stale copy is the dangerous shape — the grant names the path, so whatever
    lives there is what root runs — hence the content comparison rather than a
    mere existence check.
    """
    from .deploy import _PermCheck  # lazy: avoid an import cycle (deploy imports this)

    if not probe_sudoers_installed():
        return _PermCheck(
            category="pm-probe", target=WRAPPER_PATH, ok=True,
            detail=f"(no {SUDOERS_PATH} — PM probe not installed on this pod)",
        )
    path = Path(WRAPPER_PATH)
    try:
        expected = render_wrapper()
    except (OSError, ValueError) as exc:
        return _PermCheck(
            category="pm-probe", target=WRAPPER_PATH, ok=False,
            detail=f"could not render the reader: {exc}",
        )
    fix = f"install the rendered pm-probe-cat at {WRAPPER_PATH} (root, 0755)"
    if not path.exists():
        return _PermCheck(
            category="pm-probe", target=WRAPPER_PATH, ok=False,
            detail="missing — the sudoers grant names a file that is not there",
            fix_description=fix, apply=install_wrapper,
        )
    try:
        st = path.stat()
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _PermCheck(
            category="pm-probe", target=WRAPPER_PATH, ok=False,
            detail=f"unreadable: {exc}", fix_description=fix, apply=install_wrapper,
        )
    mode = st.st_mode & 0o777
    problems: list[str] = []
    if current != expected:
        problems.append("content differs from the rendered reader")
    if st.st_uid != 0:
        problems.append(f"owner uid={st.st_uid}, expected 0")
    if mode != WRAPPER_MODE:
        problems.append(f"mode={oct(mode)}, expected {oct(WRAPPER_MODE)}")
    if problems:
        return _PermCheck(
            category="pm-probe", target=WRAPPER_PATH, ok=False,
            detail="; ".join(problems), fix_description=fix, apply=install_wrapper,
        )
    return _PermCheck(
        category="pm-probe", target=WRAPPER_PATH, ok=True,
        detail=f"current, root-owned, {oct(mode)}",
    )


def admin_user() -> str:
    """The admin account ensure-pod-perms is running on behalf of, or ""."""
    return (os.environ.get("SUDO_USER") or "").strip()


def ensure_staging_dir(user: str) -> bool:
    """Create/repair :data:`STAGING_DIR` as ``user``-owned, 0755."""
    from platform_profile import get_profile

    c = get_profile().commands
    steps = [
        [c["mkdir"], "-p", STAGING_DIR],
        [c["chown"], user, STAGING_DIR],
        [c["chmod"], oct(STAGING_MODE)[2:], STAGING_DIR],
    ]
    for step in steps:
        # sudo-grant: ungranted-by-design: root-only repair of an operator-owned
        # staging dir; the admin-ui service user never places scripts there.
        r = subprocess.run(["sudo"] + step, capture_output=True, text=True)
        if r.returncode != 0:
            return False
    return True


def check_pm_staging_dir() -> "_PermCheck":
    """ensure-pod-perms: ``/tmp/pm`` exists, is owned by the admin account, 0755.

    The trust boundary the probe leans on: a staged script runs as a full
    (unprivileged) interpreter, so "the operator put it there" has to be a
    property of the directory, not an assumption. ``/tmp`` is world-writable
    and sticky — an unowned ``/tmp/pm`` is a bot's to create.
    """
    from .deploy import _PermCheck  # lazy: avoid an import cycle

    if not probe_sudoers_installed():
        return _PermCheck(
            category="pm-probe", target=STAGING_DIR, ok=True,
            detail=f"(no {SUDOERS_PATH} — PM probe not installed on this pod)",
        )
    user = admin_user()
    if not user:
        return _PermCheck(
            category="pm-probe", target=STAGING_DIR, ok=True,
            detail="(no SUDO_USER — cannot name the admin account to own it)",
        )
    path = Path(STAGING_DIR)
    fix = f"mkdir {STAGING_DIR} && chown {user} && chmod {oct(STAGING_MODE)[2:]}"
    if not path.exists():
        return _PermCheck(
            category="pm-probe", target=STAGING_DIR, ok=False,
            detail="missing — any user could create it and stage a script",
            fix_description=fix, apply=lambda: ensure_staging_dir(user),
        )
    try:
        import pwd

        st = path.stat()
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (OSError, KeyError) as exc:
        return _PermCheck(
            category="pm-probe", target=STAGING_DIR, ok=False,
            detail=f"stat failed: {exc}", fix_description=fix,
            apply=lambda: ensure_staging_dir(user),
        )
    mode = st.st_mode & 0o7777
    if owner != user or mode != STAGING_MODE:
        return _PermCheck(
            category="pm-probe", target=STAGING_DIR, ok=False,
            detail=f"owner={owner!r} mode={oct(mode)}; expected {user!r} {oct(STAGING_MODE)}",
            fix_description=fix, apply=lambda: ensure_staging_dir(user),
        )
    return _PermCheck(
        category="pm-probe", target=STAGING_DIR, ok=True,
        detail=f"owner={owner}, mode={oct(mode)}",
    )
