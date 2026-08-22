"""
Evolve installer — shared directory setup and ownership enforcement.

Called by `evolve-admin setup` (wizard) and `evolve-admin deploy --setup-shared`.
Requires sudo for chown operations.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Repo root: .../packages/admin/evolve_admin/installer.py → up 4 → repo root
# (matches deploy._REPO_ROOT, computed independently so installer doesn't
# import deploy — deploy imports installer, not the reverse).
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _find_uv() -> "str | None":
    """Resolve the ``uv`` binary used to build the deploy venv, or None.

    ``uv venv`` creates a virtualenv WITHOUT the stdlib ``venv``/``ensurepip``
    machinery — which stock Ubuntu ships in the separate ``python3-venv`` apt
    package that is NOT installed by default. That gap was the W7c real-VPS
    blocker: ``python3 -m venv`` died with "ensurepip is not available". uv is
    already the project's venv tool (the runbook installs it; ``uv sync``
    builds the dev venv), so prefer it and stay ensurepip-free.

    Resolution order, built to survive ``sudo`` (the deploy flow is root-gated
    and ``sudo`` resets PATH to ``secure_path``):

    1. The inherited PATH — ``/usr/local/bin/uv`` on a runbook'd pod is on
       sudo's ``secure_path`` and Homebrew's ``uv`` is on an interactive PATH.
    2. The ``UV`` env var (``uv run`` exports the absolute uv path).
    3. Well-known install dirs — ``~/.local/bin`` for ``curl|sh`` /
       ``pip install --user`` (stripped from PATH under sudo), including the
       pre-sudo invoking user's home via ``SUDO_USER``.

    Returns None when uv isn't installed — ``ensure_evolve_venv`` then falls
    back to stdlib ``venv`` (works on a host that has ``python3-venv`` but no
    uv), so neither prerequisite alone blocks the build.
    """
    found = shutil.which("uv")
    if found:
        return found
    env_uv = os.environ.get("UV")
    if env_uv and os.access(env_uv, os.X_OK) and Path(env_uv).is_file():
        return env_uv
    homes: list[str] = []
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        # expanduser("~<user>") returns the user's home, or the literal string
        # unchanged when the user is unknown (no exception to swallow) — a stale
        # SUDO_USER then just yields a candidate path that won't exist and is
        # skipped below.
        expanded = os.path.expanduser(f"~{sudo_user}")
        if expanded != f"~{sudo_user}":
            homes.append(expanded)
    homes.append(os.path.expanduser("~"))
    candidates = [Path(h) / ".local" / "bin" / "uv" for h in homes]
    candidates += [
        Path(p) / "uv"
        for p in ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin", "/root/.local/bin")
    ]
    # os.access(X_OK) before is_file: a non-executable file named `uv` must be
    # skipped (else subprocess raises an uncaught PermissionError), and we want
    # the executable, not just a regular file. shutil.which already did X_OK.
    for c in candidates:
        if os.access(c, os.X_OK) and c.is_file():
            return str(c)
    return None


# System interpreters tried (in order) when the running interpreter is not
# exec-able by the service accounts. Module-level so tests can pin it.
_SYSTEM_PYTHON_CANDIDATES: tuple[str, ...] = ("/usr/bin/python3",)


def _world_exec_ok(path: "str | Path") -> bool:
    """True iff every non-root account can exec ``path``.

    Resolves the symlink chain, then requires o+x on the real binary and o+x
    (traverse) on each of its ancestor directories. The stat-based check reads
    the "other" bits only — a chain reachable solely via group membership or a
    POSIX ACL is treated as NOT ok, which is the safe direction here (the
    service accounts hold no such grants on interpreter trees).
    """
    try:
        real = Path(os.path.realpath(path))
        if not (real.stat().st_mode & 0o001):
            return False
        for parent in real.parents:
            if not (parent.stat().st_mode & 0o001):
                return False
    except OSError:
        return False
    return True


def _pick_venv_base_python() -> str:
    """Choose the interpreter the canonical deploy venv is built against.

    The venv's ``bin/python3`` ends up a symlink to this interpreter, and
    every daemon — running as ``evolve`` or a bot account, never root — execs
    through that chain. Prefer the running interpreter (the historical
    behavior), but only when service accounts can actually traverse to it:
    the Linux runbook's bootstrap ``sudo uv sync`` makes uv download its
    managed CPython into ``/root/.local/share/uv`` (root's ``$HOME``, 0700),
    so ``sys.executable`` resolves somewhere only root can reach and every
    timer unit dies at exec with ``status=203/EXEC`` (live Ubuntu 24.04
    install, 2026-08). Fall back to a world-readable system python; fail the
    build loudly rather than ship a pod whose daemons can never start.
    """
    candidates = [sys.executable, *_SYSTEM_PYTHON_CANDIDATES]
    which = shutil.which("python3")
    if which:
        candidates.append(which)
    for cand in candidates:
        if cand and Path(cand).exists() and _world_exec_ok(cand):
            return cand
    raise RuntimeError(
        "no world-executable python3 found to build the deploy venv against: "
        f"tried {candidates}. The running interpreter likely resolves into a "
        "0700 home (e.g. uv's managed CPython under /root/.local/share/uv "
        "when the bootstrap `uv sync` ran via sudo) — daemons running as "
        "service accounts cannot exec through it (status=203/EXEC). Install "
        "a system python3 (e.g. `apt-get install python3`) and re-run."
    )


def resolve_evolve_admin_bin() -> str:
    """Absolute path to the ``evolve-admin`` console script, for sudo calls.

    A bare ``"evolve-admin"`` only works if it is on the caller's PATH. Under
    ``sudo`` the PATH is reset to the sudoers ``secure_path``, which on a
    ``uv sync`` durable pod does NOT include the actual venv — it lives at
    ``<deploy_checkout>/.venv/bin``, not the canonical ``{venv_dir}/bin`` that
    ``secure_path`` lists — so ``sudo evolve-admin …`` dies "command not found"
    (live ``evolve-vsp-pod``, FIND-A). Resolve to an absolute path the caller
    can hand to ``sudo`` verbatim:

      1. The console script next to the running interpreter
         (``Path(sys.executable).parent / "evolve-admin"``) — provably the venv
         currently executing this code; ``console_scripts`` install beside
         ``python`` in the same ``bin/``.
      2. ``sys.argv[0]`` when it names an ``evolve-admin`` script.
      3. ``shutil.which`` on the live (in-process) PATH, which has the venv.
      4. Bare ``"evolve-admin"`` — last-resort PATH lookup.
    """
    cand = Path(sys.executable).parent / "evolve-admin"
    if cand.exists():
        return str(cand)
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        p = Path(argv0)
        if p.name == "evolve-admin" and p.exists():
            return str(p.resolve())
    found = shutil.which("evolve-admin")
    if found:
        return found
    return "evolve-admin"


def ensure_evolve_admin_on_path(*, link_dir=None, bin_resolver=None):
    """Symlink the resolved ``evolve-admin`` onto a standard ``sudo`` PATH dir.

    ``sudo`` resets PATH to the sudoers ``secure_path``. On a ``uv sync``
    durable pod the real venv is ``.venv`` inside the deploy checkout — NOT the
    canonical ``{venv_dir}/bin`` that ``secure_path`` lists — so bare
    ``sudo evolve-admin …`` dies "command not found" (live ``evolve-vsp-pod``,
    FIND-A): operator-facing ``sudo evolve-admin deploy <bot>`` hints and any
    in-code bare call break. ``/usr/local/bin`` is on ``secure_path`` on every
    platform; point a stable symlink there at whatever ``evolve-admin`` is
    actually running.

    macOS is left untouched (pip/brew already put ``evolve-admin`` on PATH
    there; the macOS install flow must stay byte-identical). Idempotent and
    best-effort: never raises; returns the link path on success else ``None``.
    """
    from platform_profile import get_profile

    if get_profile().name == "macos":
        return None
    target = (bin_resolver or resolve_evolve_admin_bin)()
    if target == "evolve-admin" or not Path(target).exists():
        return None  # couldn't resolve a real binary — nothing safe to link
    link = (Path(link_dir) if link_dir else Path("/usr/local/bin")) / "evolve-admin"
    try:
        # Never clobber a real (non-symlink) binary already installed there.
        if link.exists() and not link.is_symlink():
            return str(link)
        if link.is_symlink():
            try:
                current = os.readlink(link)
            except OSError:
                current = None  # unreadable link — fall through and relink
            if current == target:
                return str(link)  # already correct
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
        return str(link)
    except OSError:
        return None


def ensure_evolve_venv() -> None:
    """Create + populate the canonical deploy venv if it doesn't exist yet.

    macOS pods provision this venv during bootstrap, so this is a fast no-op
    there (and on any already-set-up pod). On a FRESH Linux pod the deploy
    flow must BUILD it at the canonical path before reinstall_evolve_admin and
    the daemons (which all exec ``VENV_PYTHON``) can run — the real-VPS bug a
    live install hit: ``No such file or directory:
    /var/lib/evolve-venv/bin/python3`` (W7; §8.2 venv contract).

    Builds the venv against the interpreter currently running the installer (a
    fresh box has no deploy venv yet to bootstrap from) — unless that
    interpreter is not exec-able by the service accounts, in which case a
    system python is used instead (see :func:`_pick_venv_base_python`; the
    203/EXEC failure a live Ubuntu 24.04 install hit) — via ``uv venv`` when
    uv is available (the default), so the build never touches stdlib
    ``ensurepip`` which stock Ubuntu omits by default (W7c; see :func:`_find_uv`)
    — then installs evolve-analyzer + evolve-admin COMPAT-EDITABLE — analyzer
    first (admin depends on it and it is not on PyPI), compat-editable so a
    module added later by ``git pull`` imports without a reinstall (the
    interpreter contract, [[analyzer-packaged-compat-editable]]).

    Idempotent (returns at once if the venv python already exists). Requires
    root on Linux (the venv lives under ``/var/lib``, root-owned) — the deploy
    pipeline is already root-gated. Raises RuntimeError on any failing step.
    """
    from platform_profile import get_profile

    prof = get_profile()
    venv_python = Path(prof.venv_python)
    if venv_python.exists():
        # macOS bootstrap / already-set-up pod — nothing to build, but still
        # keep the PATH shim fresh so re-deploys on a uv-sync pod re-assert it
        # (FIND-A). Linux-only + best-effort; no-op on macOS.
        ensure_evolve_admin_on_path()
        return

    venv_dir = Path(prof.venv_dir)
    # Prefer ``uv venv`` — it creates the virtualenv without stdlib
    # ``venv``/``ensurepip`` (stock Ubuntu ships those in the un-installed-by-
    # default ``python3-venv`` apt pkg; ``python3 -m venv`` died there with
    # "ensurepip is not available" — the W7c real-VPS blocker). ``--seed``
    # populates pip so the compat-editable installs below run through the
    # venv's own pip unchanged; ``--python`` pins the base interpreter the
    # picker chose (the running one when service accounts can exec it, else a
    # system python). ``--clear`` makes the build idempotent on a
    # deploy retry: we only get here when ``venv_python`` is ABSENT (the guard
    # above), so any directory still at ``venv_dir`` is partial/stray state
    # from a crashed earlier build — clear it and rebuild rather than wedge on
    # uv's "directory already exists" refusal (stdlib ``venv`` upgraded in
    # place, so this preserves that self-healing). Fall back to stdlib ``venv``
    # when uv is absent (a host with ``python3-venv`` but no uv).
    base_python = _pick_venv_base_python()
    uv = _find_uv()
    if uv is not None:
        create_cmd = [
            uv, "venv", "--clear", "--seed", "--python", base_python, str(venv_dir)
        ]
    else:
        create_cmd = [base_python, "-m", "venv", str(venv_dir)]
    r = subprocess.run(create_cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(
            f"venv create failed at {venv_dir}: {(r.stderr or r.stdout or '').strip()[:300]}"
        )

    pip = str(venv_dir / "bin" / "pip")
    # analyzer FIRST — admin lists evolve-analyzer as a dependency and it is
    # not published to PyPI, so it must already be satisfied (editable) when
    # admin resolves. Both compat-editable; deps come from PyPI on the fresh
    # box (omit --no-deps — unlike ensure_analyzer_installed, this venv has none).
    for rel in ("packages/analyzer", "packages/admin"):
        pkg = _REPO_ROOT / rel
        r = subprocess.run(
            [pip, "install", "--quiet", "--config-settings",
             "editable_mode=compat", "-e", str(pkg)],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"venv 'pip install -e {rel}' failed: "
                f"{(r.stderr or r.stdout or '').strip()[:400]}"
            )

    # FIND-A: a uv-sync durable pod's venv lives off sudo's secure_path, so bare
    # `sudo evolve-admin …` (operator hints + internal calls) dies "command not
    # found". Shim a stable /usr/local/bin symlink onto secure_path now that the
    # console script exists — preferring the canonical venv's script over the
    # one beside the running interpreter: during the wizard that interpreter is
    # the bootstrap venv, whose chain may be root-only (the 203/EXEC family
    # above), which would leave bare `evolve-admin` broken for non-root shells.
    # Linux-only + best-effort; no-op on macOS.
    canonical_script = venv_dir / "bin" / "evolve-admin"
    if canonical_script.exists():
        ensure_evolve_admin_on_path(bin_resolver=lambda: str(canonical_script))
    else:
        ensure_evolve_admin_on_path()


# Subdirectories that must exist under the shared data dir (755)
_SHARED_SUBDIRS = [
    "metrics",
    "annotations",
    "turns",
    "proposals/pending",
    "proposals/approved",
    "proposals/rejected",
    "scoreboard",
    "feedback",
    "alerts",
    "applications",
    "plists",
    "logs",
    "kaizen",
]

# Dirs that need sticky world-writable (1777) so multiple users can write
_STICKY_DIRS = ["metrics", "annotations"]


def setup_shared(shared_dir: Path, bot_ids: list[str] | None = None) -> None:
    """
    Create the shared /Users/Shared/evolve/ directory tree and enforce
    ownership per the architecture doc (docs/local-deployment-architecture.md):

      - evolve:wheel owns the shared data dir and most subdirs (if evolve user exists)
      - Falls back to root:wheel if evolve user has not been created yet
      - 755 permissions on most dirs (readable by all bot users, writable only by owner)
      - Per-bot turns dirs ({bot}/turns/) get 1777 (sticky world-writable) so each bot
        user can create and append their own files without needing evolve user access.
        The sticky bit prevents bots from deleting each other's files.

    Requires sudo (must be called from a process with root/sudo access).
    bot_ids — if provided, create per-bot turns subdirectories now.
              Safe to call again later when a new bot is added.
    """
    shared_dir = Path(shared_dir)

    # Create parent dir if needed
    subprocess.run(
        ["sudo", "mkdir", "-p", str(shared_dir)],
        check=True, capture_output=True,
    )

    # Create all required subdirectories
    for subdir in _SHARED_SUBDIRS:
        p = shared_dir / subdir
        subprocess.run(
            ["sudo", "mkdir", "-p", str(p)],
            check=True, capture_output=True,
        )

    # Create per-bot turns subdirs if bot list is known
    for bot_id in (bot_ids or []):
        p = shared_dir / bot_id / "turns"
        subprocess.run(
            ["sudo", "mkdir", "-p", str(p)],
            check=True, capture_output=True,
        )
        # 1777: sticky world-writable so each bot writes its own files
        subprocess.run(
            ["sudo", "chmod", "1777", str(p)],
            check=True, capture_output=True,
        )

    # Set ownership: evolve:<admin_group> if evolve user exists, else
    # root:<admin_group>. The admin/root group is platform-keyed (macOS
    # 'wheel', Linux 'root') — 'wheel' is not gid 0 on Linux, so a hardcoded
    # 'root:wheel' chown fails on a fresh Ubuntu box (Linux deploy-port W7).
    from platform_profile import get_profile
    from .runtime.isolation import get_isolation
    from .secret_config_perms import shared_dir_targets_excluding_checkout
    group = get_profile().admin_group
    owner = f"evolve:{group}" if get_isolation().user_exists("evolve") else f"root:{group}"
    # chown evolve/root + chmod 755 so all bot users can read/traverse, only owner
    # can write. Both passes PRUNE a nested deploy checkout (Linux {shared_dir}/repo):
    # a recursive `chmod -R 755` over the git tree sets the owner-exec bit on every
    # tracked file (100644→100755), which `core.fileMode=true` reports as modified and
    # `git pull --ff-only` then refuses — the 2026-06-23 Linux pod freeze. On macOS the
    # checkout is a SIBLING, so every child is included → identical to a blanket `-R`.
    for _path, _recursive in shared_dir_targets_excluding_checkout(shared_dir):
        _flags = ["-R"] if _recursive else []
        subprocess.run(["sudo", "chown", *_flags, owner, str(_path)],
                       check=True, capture_output=True)
    for _path, _recursive in shared_dir_targets_excluding_checkout(shared_dir):
        _flags = ["-R"] if _recursive else []
        subprocess.run(["sudo", "chmod", *_flags, "755", str(_path)],
                       check=True, capture_output=True)

    # Re-assert special modes stripped by the -R 755 pass above.
    # The shared dir root must be 1777 so bot users can create their own
    # subdirs (e.g. {bot}/turns/) without needing root each time.
    subprocess.run(["sudo", "chmod", "1777", str(shared_dir)], capture_output=True)

    # The `-R 755` pass is the SECOND re-exposer of the evolve-owned protected
    # trees (deploy_shared_dir's `chmod -R a+rX` is the first, and the one that
    # left the file-vault machine key world-readable for two months — see the
    # keystore section in secret_config_perms). It is strictly worse here: 755
    # adds o+x on top of o+r. Re-tighten secrets/ + directory/ + keystore/ right
    # after, the same way deploy_shared_dir does. Best-effort; the per-file
    # self-heals in ensure_pod_perms are the backstop.
    from .secret_config_perms import tighten_shared_protected_trees
    tighten_shared_protected_trees(shared_dir)

    # metrics and annotations: 1777 so each bot can write its own files
    # without being able to delete files owned by other bots (sticky bit).
    for sticky in _STICKY_DIRS:
        p = shared_dir / sticky
        if p.exists():
            subprocess.run(["sudo", "chmod", "1777", str(p)], capture_output=True)

    # Per-bot turns dirs: 1777 so each bot writes its own turn files.
    for bot_id in (bot_ids or []):
        p = shared_dir / bot_id / "turns"
        if p.exists():
            subprocess.run(
                ["sudo", "chmod", "1777", str(p)],
                check=True, capture_output=True,
            )
    # Also fix any existing turns dirs not in the bot_ids list (idempotent).
    for turns in shared_dir.glob("*/turns"):
        if turns.is_dir() and turns not in [shared_dir / b / "turns" for b in (bot_ids or [])]:
            subprocess.run(["sudo", "chmod", "1777", str(turns)], capture_output=True)
