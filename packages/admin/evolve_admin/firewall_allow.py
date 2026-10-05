"""firewall_allow.py — the macOS Application Firewall must know the daemon's Python.

WHY THIS MODULE EXISTS (incident, first live phone test, 2026-09-04).
The board's tailnet listener bound correctly on the pod's ``100.x`` address
and logged its one happy line.  Every inbound connection from the phone then
died on the first read with ``OSError: [Errno 57] Socket is not connected``,
in a stderr file nobody reads.  A bare ``python3 -m http.server`` on the same
host failed identically while ``nc -l`` served fine: the macOS **Application
Firewall** was on, Apple's ``/usr/bin/python3`` was in its allow list, and the
Homebrew interpreter the daemon actually runs
(``/opt/homebrew/Cellar/python@3.14/…/Python.app/Contents/MacOS/Python``) was
set to *Block incoming connections*.  Loopback is exempt from the filter,
which is why the admin UI on ``127.0.0.1`` never noticed and why nothing in
any test or probe could see it.

The operator repaired it by hand (``socketfilterfw --unblockapp``, then a
daemon restart — the filter decision is cached per process).  Nothing in
deploy set the rule and nothing checked it, so the next Homebrew Python
upgrade would move the binary's path and silently re-block the pod: the same
drift class as the missing per-bot sudoers grants (#3992), with the same
signature — a feature that is *configured* correctly and simply never works.

So the rule is now Evolve's, in three places that say the same thing:

  1. **Deploy sets it.**  :func:`ensure_daemon_firewall_allow` runs beside the
     admin-ui plist install (``install-infra-jobs`` / a pod deploy, where root
     is already held): resolve the real binary the plist's program ends up
     executing, and — only when the firewall is enabled and that binary is
     absent from its app list or blocked — ``--add`` + ``--unblockapp`` it.
  2. **The pod re-verifies it.**  :func:`check_daemon_firewall_allow` is one
     ``_PermCheck`` in ``deploy.ensure_pod_perms``, so every deploy applies it
     and ``pod_perms_drift_monitor`` turns a re-block between deploys into a
     Signal within the hour.
  3. **The listener says so.**  ``web.board_listener`` turns the ENOTCONN
     shape into one warning naming this repair, instead of a stderr
     traceback (see that module's ``handle_error``).

SCOPE, DELIBERATELY NARROW.  This module never calls ``--setglobalstate``:
an operator who turned the firewall on keeps it on, and a pod whose firewall
is off needs nothing from us.  It never touches an app other than the one
binary the admin daemon executes — and that binary is not a configuration
choice, it is whatever the kernel resolves when launchd starts the plist, so
allowing anything else would be allowing the wrong thing.

NO NEW SUDOERS GRANT, DELIBERATELY.  ``socketfilterfw`` needs root, and both
call sites (``install-infra-jobs``, ``ensure-pod-perms`` in apply mode)
already refuse to run without it.  A pass running as ``evolve`` therefore
*reports* the drift and names the operator command rather than shelling out
to a ``sudo`` the service user has no grant for — the same posture, for the
same reason, as :mod:`board_store_perms`.

LINUX IS A NO-OP.  There is no Application Firewall; ``iptables``/``ufw`` on a
VPS pod is the operator's own perimeter and not something Evolve edits behind
their back.  Both entry points return an explicit informational pass naming
the platform, so a Linux pod's ``ensure-pod-perms`` output says why the check
did nothing instead of silently omitting a line.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from platform_profile import get_profile as _get_profile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .deploy import _PermCheck

log = logging.getLogger(__name__)

#: ``_PermCheck.category`` for everything this module reports.
PERM_CHECK_CATEGORY = "firewall-allow"

#: Apple's Application Firewall CLI. macOS-only by construction — every call
#: site below sits behind the platform gate in :func:`_is_macos`.
SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"

#: The one command an operator runs to repair a re-blocked interpreter, named
#: identically in the drift check, in the fix description, and in the board
#: listener's warning, so all three say the same thing.
REPAIR_COMMAND = "sudo evolve-admin ensure-pod-perms"

#: What the operator must ALSO do after the rule is added. The filter's
#: verdict is cached per process, so an already-running daemon keeps being
#: blocked until it is restarted — the step the 2026-09-04 hand-repair needed
#: and the reason the fix text says it out loud.
RESTART_HINT = (
    "then restart the admin daemon "
    "(`sudo launchctl kickstart -k system/ai.evolve.evolve.admin-ui`) — the "
    "firewall's verdict is cached per process, so a running daemon stays "
    "blocked until it is restarted"
)

#: Firewall global states, from ``--getglobalstate``. 1 = on, 2 = on with
#: "block all incoming"; both mean the per-app list is consulted. 0 = off.
_ENABLED_STATES = ("1", "2")


def _is_macos() -> bool:
    return _get_profile().name == "macos"


def _cli_available() -> bool:
    """True when this host actually HAS an Application Firewall to talk to.

    A second gate behind :func:`_is_macos`, and the two answer different
    questions. The profile says which path shapes and binaries Evolve should
    assume; whether ``socketfilterfw`` is on disk says whether the firewall
    exists at all. They come apart in two places that both matter:

    * a **pinned** macOS profile on a Linux host — the admin test suite does
      exactly this (``tests/conftest.py`` pins ``MACOS`` so macOS path shapes
      are deterministic on CI runners), and a stripped or containerised image
      can too;
    * a macOS install with the ApplicationFirewall bundle absent.

    In both, there is no filter to be blocked by, so the honest report is an
    informational pass naming the missing binary — not the "cannot read the
    firewall state" drift, which means something quite different: the CLI is
    right there and would not answer.
    """
    return os.path.exists(SOCKETFILTERFW)


def _geteuid() -> int:
    """Indirection over ``os.geteuid()`` so tests can act like root.

    Same shape, same reason, as ``board_store_perms._geteuid``: the root
    branch is the entire point of the apply path and must be exercisable
    without one.
    """
    return os.geteuid()


def _run(*args: str) -> "tuple[int, str]":
    """Run ``socketfilterfw`` with ``args``; return ``(rc, stdout+stderr)``.

    The single seam every firewall call goes through, so a test substitutes
    one function instead of patching ``subprocess`` globally. Never raises: a
    missing binary or a timeout is reported as a non-zero rc with the reason
    in the text, and every caller treats "cannot tell" as "do not claim".
    """
    try:
        proc = subprocess.run(
            [SOCKETFILTERFW, *args],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ── what the daemon actually executes ───────────────────────────────────────


def _shebang_interpreter(script: Path) -> "Path | None":
    """The interpreter named by ``script``'s shebang, or None if it has none.

    The admin-ui plist runs ``evolve-admin serve`` — a venv **console
    script**, not an interpreter — so the binary the firewall judges is the
    one on its ``#!`` line, not the script itself. A venv's generated console
    scripts carry an absolute shebang, which is the case that matters here;
    ``#!/usr/bin/env python3`` is handled only when the named interpreter sits
    beside the script (the venv layout), because resolving it through ``PATH``
    would be guessing at a ``PATH`` we are not the ones executing under.
    """
    try:
        with open(script, "rb") as fh:
            first = fh.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    try:
        parts = first[2:].decode("utf-8", "replace").strip().split()
    except Exception:  # noqa: BLE001 - a binary first line is simply not a shebang
        return None
    if not parts:
        return None
    interp = Path(parts[0])
    if interp.name == "env":
        if len(parts) < 2:
            return None
        beside = script.parent / parts[1]
        return beside if beside.exists() else None
    return interp


def resolve_daemon_interpreter(program: "str | os.PathLike[str]") -> Path:
    """The real binary the kernel executes for ``program``.

    Two steps, both of which the 2026-09-04 incident needed:

    1. **Through the console script.** ``program`` is the plist's first argv
       element — ``/…/evolve-venv/bin/evolve-admin``, a Python script. The
       firewall judges the *interpreter*, so follow the shebang.
    2. **Through the whole symlink chain.** A venv's ``bin/python3`` is a
       symlink into the Homebrew Cellar, whose own ``bin/python3.X`` is in
       turn a symlink to the framework's
       ``Resources/Python.app/Contents/MacOS/Python``. That last file is what
       the kernel executes, what the process reports as its executable, and
       what the operator found blocked. ``Path.resolve()`` walks the whole
       chain, so it lands there without this code having to know the layout —
       and on a python.org build, where ``bin/python3.X`` is itself the real
       binary, it correctly lands on that instead.

    Returns the best path it can name even when nothing exists on disk (a
    dry-run or a test fixture): resolution is a path computation, and the
    callers decide separately whether the file is real.
    """
    p = Path(program)
    shebang = _shebang_interpreter(p)
    if shebang is not None:
        p = shebang
    try:
        return p.resolve()
    except OSError:  # pragma: no cover - resolve() is strict=False by default
        return p


def daemon_interpreter() -> Path:
    """:func:`resolve_daemon_interpreter` for the admin-ui plist's program.

    The one definition of "the binary the firewall has to allow", read off
    the same JobSpec deploy installs, so the rule can never be scoped to a
    program the daemon does not actually run.
    """
    from .deploy import _admin_ui_jobspec  # lazy: deploy imports this module
    return resolve_daemon_interpreter(_admin_ui_jobspec("probe").program_args[0])


# ── reading the firewall ────────────────────────────────────────────────────


def firewall_enabled() -> "bool | None":
    """True/False for the global on-off state; None when it cannot be read.

    None is a real answer, not an error: a pass that cannot ask the firewall
    must not assert that the interpreter is fine, nor invent drift.
    """
    rc, out = _run("--getglobalstate")
    if rc != 0:
        return None
    text = out.strip()
    if "State = " not in text:
        return None
    state = text.rsplit("State = ", 1)[-1].strip().rstrip(").").strip()
    return state in _ENABLED_STATES


def app_state(binary: Path) -> "str | None":
    """``"allow"`` / ``"block"`` for ``binary``, or None if it is not listed.

    Read from ``--listapps``, deliberately NOT from ``--getappblocked``: that
    subcommand answers "permitted" for a path the firewall has never heard of
    (measured 2026-09-04), so it cannot distinguish *allowed* from *absent* —
    and absent is exactly the state a Homebrew Python upgrade produces.

    ``--listapps`` prints ``N : <path>`` lines each followed by a
    parenthesised verdict; paths are compared after ``realpath`` so a listed
    symlink and a resolved binary are recognised as the same app.
    """
    rc, out = _run("--listapps")
    if rc != 0:
        return None
    target = os.path.realpath(binary)
    pending = False
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if pending and line.startswith("("):
            verdict = line.strip("()").strip().lower()
            return "block" if verdict.startswith("block") else "allow"
        pending = False
        if " : " not in line:
            continue
        listed = line.split(" : ", 1)[1].strip()
        if listed and os.path.realpath(listed) == target:
            pending = True
    return None


# ── writing the firewall ────────────────────────────────────────────────────


def allow_daemon_interpreter(binary: "Path | None" = None) -> bool:
    """``--add`` + ``--unblockapp`` the daemon's interpreter. Root only.

    Idempotent by construction: ``--add`` on an already-listed app and
    ``--unblockapp`` on an already-allowed one are both no-ops in the
    firewall's own semantics, and the callers only reach this when the state
    actually needs changing.

    Returns False — with a log line naming the operator command — when the
    pass is not root, which is the ``evolve``-daemon case. Never disables the
    firewall and never touches another app.
    """
    if not _is_macos() or not _cli_available():
        return False
    binary = binary or daemon_interpreter()
    if _geteuid() != 0:
        log.warning(
            "firewall allow-rule for %s needs root — run `%s` on the pod host",
            binary, REPAIR_COMMAND,
        )
        return False
    add_rc, add_out = _run("--add", str(binary))
    unblock_rc, unblock_out = _run("--unblockapp", str(binary))
    if add_rc != 0 or unblock_rc != 0:
        log.warning(
            "firewall: could not allow %s (--add rc=%s %s; --unblockapp rc=%s %s)",
            binary, add_rc, add_out.strip(), unblock_rc, unblock_out.strip(),
        )
        return False
    log.info("firewall: allowed incoming connections for %s", binary)
    return True


def ensure_daemon_firewall_allow(
    log_line: "Callable[[str], None] | None" = None,
) -> bool:
    """The deploy step: make sure the firewall knows this pod's interpreter.

    Emits exactly one line naming the binary and what was (or was not) done,
    through ``log_line`` when a deploy result is collecting steps and through
    the module logger otherwise. Returns True when the pod ends the call in
    the desired state — including every case where there was nothing to do.

    Never raises: a firewall the deploy cannot read is a logged unknown, not
    a failed deploy.
    """
    def _say(msg: str) -> None:
        (log_line or log.info)(msg)  # type: ignore[operator]

    if not _is_macos():
        _say(f"firewall-allow: skipped — no Application Firewall on "
             f"{_get_profile().name}")
        return True
    if not _cli_available():
        _say(f"firewall-allow: skipped — {SOCKETFILTERFW} is not present on "
             f"this host")
        return True
    try:
        binary = daemon_interpreter()
    except Exception as exc:  # noqa: BLE001 - never fatal to a deploy
        _say(f"[warn] firewall-allow: cannot resolve the admin daemon's "
             f"interpreter ({exc}); skipping")
        return False

    enabled = firewall_enabled()
    if enabled is None:
        _say(f"[warn] firewall-allow: cannot read the Application Firewall "
             f"state; left {binary} alone")
        return False
    if not enabled:
        _say(f"firewall-allow: Application Firewall is off — no rule needed "
             f"for {binary}")
        return True

    state = app_state(binary)
    if state == "allow":
        _say(f"firewall-allow: {binary} already allowed")
        return True
    ok = allow_daemon_interpreter(binary)
    _say(
        f"firewall-allow: allowed incoming connections for {binary} "
        f"(was {state or 'not listed'})"
        if ok else
        f"[warn] firewall-allow: {binary} is {state or 'not listed'} and the "
        f"rule could not be added — run `{REPAIR_COMMAND}`"
    )
    return ok


# ── drift check (ensure_pod_perms / pod_perms_drift_monitor) ────────────────


def _fix_description(binary: Path) -> str:
    return (
        f"{SOCKETFILTERFW} --add {binary} && "
        f"{SOCKETFILTERFW} --unblockapp {binary}, {RESTART_HINT} "
        f"(runs in-process as root; if this pass is not root, run "
        f"`{REPAIR_COMMAND}`)"
    )


def check_daemon_firewall_allow() -> "list[_PermCheck]":
    """One ``_PermCheck``: the firewall lets the admin daemon accept traffic.

    Four outcomes, and three of them are passes:

    * not macOS — informational pass naming the platform;
    * firewall disabled — informational pass (nothing to enforce; the pod is
      reachable and an operator who enables the firewall later gets the drift
      on the next hourly tick);
    * interpreter listed as allowed — pass;
    * firewall enabled and the interpreter absent or blocked — **drift**, with
      the two commands and the restart in the fix text, because the operator
      reading this in a Signal has no other way to know the restart is part of
      the repair.

    A firewall whose state cannot be read is reported as drift with no
    ``apply``: "we could not check" must never render as "checked and fine",
    and there is nothing to apply when we do not know what is wrong.
    """
    from .deploy import _PermCheck  # lazy: deploy imports this module at load

    if not _is_macos():
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=SOCKETFILTERFW, ok=True,
            detail=(f"(non-macOS pod [{_get_profile().name}] — no Application "
                    f"Firewall, nothing to enforce)"),
        )]
    if not _cli_available():
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=SOCKETFILTERFW, ok=True,
            detail=("(no Application Firewall on this host — "
                    f"{SOCKETFILTERFW} is absent, nothing to enforce)"),
        )]

    binary = daemon_interpreter()
    enabled = firewall_enabled()
    if enabled is None:
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=str(binary), ok=False,
            detail=(f"cannot read the Application Firewall state — inbound "
                    f"connections to {binary} may be silently dropped"),
            fix_description=(
                f"inspect it by hand (`{SOCKETFILTERFW} --getglobalstate`); "
                f"there is no automatic repair for a firewall we cannot read"
            ),
        )]
    if not enabled:
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=str(binary), ok=True,
            detail="(Application Firewall is off — nothing to enforce)",
        )]

    state = app_state(binary)
    if state == "allow":
        return [_PermCheck(
            category=PERM_CHECK_CATEGORY, target=str(binary), ok=True,
            detail="allowed to accept incoming connections",
        )]
    return [_PermCheck(
        category=PERM_CHECK_CATEGORY, target=str(binary), ok=False,
        detail=(
            f"the Application Firewall is on and this interpreter is "
            f"{'blocked' if state == 'block' else 'not in its app list'} — "
            f"every off-box connection (the board on the tailnet address) is "
            f"dropped before the first byte; loopback is exempt, so the admin "
            f"UI on 127.0.0.1 keeps working and hides it"
        ),
        fix_description=_fix_description(binary),
        apply=lambda: allow_daemon_interpreter(binary),
    )]

