"""
evolve_admin/service.py — macOS launchd service management for the evolve-admin server.

Manages the com.evolve.admin LaunchAgent, which keeps the Flask admin server
running persistently: starts at login, restarts on crash, no terminal required.

CLI surface:  evolve-admin service {install,uninstall,start,stop,restart,status,logs}
API surface:  GET /api/admin/service/status
              POST /api/admin/service/restart
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from platform_profile import get_profile

from .runtime import JobSpec, LaunchdScheduler, get_scheduler, render_launchd_plist

_log = logging.getLogger(__name__)


def _is_linux() -> bool:
    """True iff the active platform profile is Linux (systemd backend).

    Routes the system-daemon detection/status/restart through the platform
    seam rather than ``sys.platform`` branches: the admin-ui runs as a
    systemd unit on a Linux VPS, where the macOS-shaped LaunchAgent /
    LaunchDaemon plist paths below never exist (the false-negative the
    8.3 port fixes). ``get_scheduler()`` resolves to ``SystemdScheduler``
    on Linux off this same profile, so this is the sanctioned dispatch key
    (NOT a banned ``sys.platform`` branch, NOT a banned scheduler
    construction)."""
    return get_profile().name == "linux"


LABEL = "com.evolve.admin"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

# System LaunchDaemon label used when the server runs as a dedicated user
# (e.g. the 'evolve' user on a production Mac Mini).
SYSTEM_LABEL = "ai.evolve.evolve.admin-ui"
SYSTEM_PLIST_PATH = Path("/Library/LaunchDaemons") / f"{SYSTEM_LABEL}.plist"
SYSTEM_LOG_PATH = Path("/Users/evolve/.openclaw/logs/evolve-admin-ui.log")
LOG_DIR = Path.home() / ".evolve" / "logs"
LOG_PATH = LOG_DIR / "admin-server.log"

def generate_plist(host: str = "127.0.0.1", port: int = 5050) -> str:
    """Generate launchd plist XML for the current Python environment."""
    # Preserve the current PATH so evolve-admin's dependencies are findable
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin")
    spec = JobSpec(
        label=LABEL,
        program_args=[
            sys.executable, "-m", "evolve_admin.web.run",
            "--host", str(host), "--port", str(port),
        ],
        run_at_load=True,
        keep_alive=True,
        stdout_path=str(LOG_PATH),
        stderr_path=str(LOG_PATH),
        env={"PATH": path_env},
    )
    return render_launchd_plist(spec)


def _uid() -> str:
    return str(os.getuid())


def _user_scheduler() -> LaunchdScheduler | None:
    """gui/<uid>-domain launchd adapter over the active Scheduler seam.

    service.py manages the admin server's own user LaunchAgent (plus the
    system-daemon self-restart) — the daemon-managing-itself bootstrap-order
    special case of the 4.3C design (open question 3). The derived adapter
    shares the active seam's runner, so a test-injected
    ``set_scheduler(LaunchdScheduler(runner=fake))`` intercepts every call
    here and tests never spawn a real launchctl. Returns None when a
    non-launchd Scheduler is active (pure-protocol fake / future port) —
    call sites no-op or fall back to Protocol verbs.
    """
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        return None
    return LaunchdScheduler(
        domain=f"gui/{_uid()}",
        plist_dir=PLIST_PATH.parent,
        use_sudo=False,
        runner=sched._runner,  # share the seam's (possibly injected) runner
    )


def _launchctl(*args: str) -> tuple[int, str]:
    """Run one user-domain launchctl verb through the Scheduler seam;
    returns (returncode, combined output).

    raw() escape hatch (4.3C S2 debt): the self-management flows below
    predate the verb surface — path-form ``bootout``, legacy
    ``load``/``unload`` fallbacks for older macOS, and ``start``/``stop``
    — and the CLI surface must stay behavior-identical. No-op ``(0, "")``
    when a non-launchd Scheduler is injected (same convention as
    ``deploy._scheduler_launchctl``).
    """
    sched = _user_scheduler()
    if sched is None:
        return 0, ""
    rc, out, err = sched.raw(*args)
    return rc, (out + err).strip()


def _kill_port(port: int) -> None:
    """Kill any process currently listening on the given TCP port."""
    try:
        # lsof BINARY routes through platform_profile (W7): /usr/sbin/lsof on
        # macOS, /usr/bin/lsof on Linux. A hardcoded macOS path FileNotFound-
        # errors on a Linux pod and the bare `except` below swallows it — the
        # stale listener never gets killed and the admin server fails to bind.
        r = subprocess.run(
            [get_profile().lsof, "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in r.stdout.split():
            try:
                os.kill(int(pid_str), 9)
            except (ProcessLookupError, ValueError):
                pass
        if r.stdout.strip():
            time.sleep(0.5)  # give kernel time to release the port
    except Exception:
        pass


def _start_direct(host: str, port: int) -> tuple[bool, str]:
    """Start the server as a detached background process (no launchd).

    Used as a fallback when launchd registration isn't possible (e.g. SSH
    session without an active GUI domain).  The plist is still written so
    the service auto-starts on next GUI login.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fd = open(LOG_PATH, "a")  # noqa: WPS515 — intentional long-lived fd
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "evolve_admin.web.run",
             "--host", host, "--port", str(port)],
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,  # detach from terminal / SSH session
            close_fds=True,
        )
    except Exception as e:
        log_fd.close()
        return False, f"Could not start server directly: {e}"

    # Give it a moment to bind
    for _ in range(8):
        time.sleep(0.5)
        if proc.poll() is not None:
            log_fd.close()
            return False, f"Server exited immediately (rc={proc.returncode}). Check {LOG_PATH}"
        # Quick port-open check
        import socket as _sock
        try:
            with _sock.create_connection((host, port), timeout=0.5):
                log_fd.close()
                return True, (
                    f"Server started (PID {proc.pid}) — not managed by launchd.\n"
                    f"It will auto-start via launchd on your next GUI login.\n"
                    f"Log: {LOG_PATH}"
                )
        except OSError:
            pass

    log_fd.close()
    return True, (
        f"Server starting (PID {proc.pid}) — port not yet open, "
        f"but process is running. Check {LOG_PATH}"
    )


def install(host: str = "127.0.0.1", port: int = 5050) -> tuple[bool, str]:
    """Write plist and load the LaunchAgent. Returns (ok, message).

    When called over SSH (no active GUI session), launchd registration will
    fail with exit 125 ("Domain does not support specified action").  In that
    case the plist is still written (for auto-start at next GUI login) and the
    server is started directly as a detached background process.

    If the system LaunchDaemon is already running the server (production
    setup), reports success without trying to start a second instance.

    On Linux the admin-ui is a systemd unit installed at provisioning
    (deploy.py ``_install_launchd_admin_ui`` → ``_install_spec_via_seam``);
    Step 1 is status + restart ONLY there. install() is a no-op-with-message
    so it never writes a launchd plist on Linux — the bug that leaked the
    stale ``com.evolve.admin.plist`` user-LaunchAgent the 8.3 port removes.
    """
    if _is_linux():
        return True, (
            f"Admin server is managed by the systemd unit {SYSTEM_LABEL} "
            "(installed at provisioning). Use Restart."
        )

    # If the system daemon already has the server running, nothing to do.
    import socket as _sock
    if SYSTEM_PLIST_PATH.exists():
        try:
            with _sock.create_connection((host, port), timeout=1):
                return True, (
                    f"Admin server already running via system daemon "
                    f"({SYSTEM_LABEL}). No action needed."
                )
        except OSError:
            pass

    # Ensure log directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist_content = generate_plist(host, port)
    PLIST_PATH.write_text(plist_content)

    # Remove any existing registration before loading.
    # raw (not Scheduler.remove()): path-form bootout that must NOT delete
    # the plist we just wrote, plus the legacy `unload` fallback.
    _launchctl("bootout", f"gui/{_uid()}", str(PLIST_PATH))
    _launchctl("unload", str(PLIST_PATH))  # legacy fallback cleanup

    # Try bootstrap (macOS 10.15+), fall back to load (older/some configs).
    # raw (not Scheduler.install()): install() owns the plist write and
    # skips byte-identical content — this flow must always re-register, and
    # needs the bootstrap rc to drive the legacy-load / direct-start
    # fallbacks for SSH/headless sessions.
    rc, out = _launchctl("bootstrap", f"gui/{_uid()}", str(PLIST_PATH))
    if rc != 0:
        rc2, out2 = _launchctl("load", str(PLIST_PATH))
        if rc2 != 0:
            # Both launchd methods failed — likely an SSH/headless session where
            # the gui/<uid> domain doesn't exist.  Kill any stale server on the
            # port first, then start the process directly.
            _kill_port(port)
            return _start_direct(host, port)

    # Wait briefly for the process to start
    for _ in range(12):
        time.sleep(0.5)
        s = status()
        if s.get("running"):
            return True, f"Service installed and running (PID {s.get('pid')})"

    s = status()
    if s.get("installed"):
        return True, "Service installed. Process starting — check status in a moment."
    return False, f"Service installed but process did not start. Check logs: {LOG_PATH}"


def uninstall(quiet: bool = False) -> tuple[bool, str]:
    """Unload the LaunchAgent and remove the plist.

    On Linux this is a no-op-with-message: the systemd unit is owned by
    the provisioning layer, not the wizard, so uninstall() must never
    ``systemctl disable`` / delete it (and there is no launchd plist to
    remove)."""
    if _is_linux():
        msg = (
            f"Admin server is managed by the systemd unit {SYSTEM_LABEL}; "
            "nothing to uninstall from the wizard."
        )
        return (True, "not installed") if quiet else (True, msg)

    if not PLIST_PATH.exists():
        if quiet:
            return True, "not installed"
        return False, f"Service not installed (plist not found at {PLIST_PATH})"

    # raw (not Scheduler.remove()): path-form bootout; the unlink below runs
    # unprivileged with its own message contract ("Could not remove plist").
    _launchctl("bootout", f"gui/{_uid()}", str(PLIST_PATH))

    try:
        PLIST_PATH.unlink()
    except OSError as e:
        return False, f"Could not remove plist: {e}"

    return True, "Service uninstalled successfully."


def start() -> tuple[bool, str]:
    """Start the service (must already be installed/bootstrapped)."""
    if not PLIST_PATH.exists():
        return False, "Service not installed — run 'evolve-admin service install' first"
    # raw: legacy `launchctl start` has no Scheduler verb (restart() would
    # kill a running instance; this must only start a stopped one).
    rc, out = _launchctl("start", LABEL)
    return rc == 0, out or "Started"


def stop() -> tuple[bool, str]:
    """Stop the service without unregistering it from launchd."""
    # raw: legacy `launchctl stop` has no Scheduler verb (remove() would
    # unregister; kill() would signal but KeepAlive respawns differently).
    rc, out = _launchctl("stop", LABEL)
    return rc == 0, out or "Stopped"


def restart() -> tuple[bool, str]:
    """Restart the service.

    If managed by launchd, uses kickstart -k for a clean restart.
    Otherwise falls back to os.execv to restart the current process in-place
    (useful when the server was started manually, e.g. during development or
    before 'evolve-admin service install' has been run).
    """
    # Linux: restart the systemd unit through the seam (SystemdScheduler →
    # `systemctl restart`, sudo'd by default). Only fall through to the
    # os.execv self-re-exec if the systemd restart genuinely fails — never
    # take the macOS launchd branches below (their plist paths don't exist).
    if _is_linux():
        try:
            ok, out = get_scheduler().restart(SYSTEM_LABEL)
            if ok:
                return True, out or "Restarted via systemctl (systemd unit)"
        except Exception as e:
            # Logged, not swallowed (except-pass-ratchet): the execv fallback
            # below is the recovery, but record WHY the systemd path failed.
            _log.debug("systemd restart of %s failed; falling back to execv: %s",
                       SYSTEM_LABEL, e)
        # systemd restart failed — fall through to the execv re-exec below
        # so a restart still works (e.g. sudo not yet granted at first run).

    # macOS launchd branches — gated off Linux so the stale
    # com.evolve.admin.plist (PLIST_PATH) that may linger on a Linux box
    # can't route a restart through the wrong (systemd) label. On Linux the
    # only way here is a failed systemd restart, which goes straight to execv.
    if not _is_linux():
        # Try system LaunchDaemon first (production: server runs as 'evolve' user).
        # Self-restart special case: the process kickstarts its OWN daemon label.
        if SYSTEM_PLIST_PATH.exists():
            # raw: unsudo'd first attempt — succeeds when this process already
            # runs as root; the sudo'd Protocol restart() below is the retry.
            rc, out = _launchctl("kickstart", "-k", f"system/{SYSTEM_LABEL}")
            if rc == 0:
                return True, out or "Restarted via launchctl (system daemon)"
            # Retry with sudo via the seam (system domain, sudo'd by default).
            try:
                ok, out = get_scheduler().restart(SYSTEM_LABEL)
                if ok:
                    return True, out or "Restarted via sudo launchctl (system daemon)"
            except Exception:
                pass

        if PLIST_PATH.exists():
            g = _user_scheduler()
            ok, out = (g.restart(LABEL) if g is not None
                       else get_scheduler().restart(LABEL))
            if ok:
                return True, out or "Restarted via launchctl"
            # kickstart failed — likely SSH/headless (no gui domain). Fall through
            # to the os.execv path so restarts still work over SSH.

    # Not managed by launchd (or launchd unavailable) — restart by re-exec'ing.
    # Close all FDs above stderr first so the new process can bind the port.
    import os as _os
    import resource as _res
    import sys as _sys
    import threading as _th
    import time as _t

    def _exec_restart() -> None:
        _t.sleep(1.5)
        try:
            maxfd = _res.getrlimit(_res.RLIMIT_NOFILE)[1]
            if maxfd == _res.RLIM_INFINITY:
                maxfd = 4096
            _os.closerange(3, min(int(maxfd), 4096))
        except Exception:
            pass
        _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

    _th.Thread(target=_exec_restart, daemon=False).start()
    return True, "Admin server restarting in ~2 seconds…"


def _status_linux() -> dict:
    """Linux status — the admin-ui systemd unit via the seam.

    Ignores the macOS-shaped LaunchAgent/LaunchDaemon plist paths entirely:
    on Linux they never legitimately exist (a stale ``com.evolve.admin.plist``
    install() once wrote is meaningless), so this queries the SYSTEM unit
    through ``get_scheduler()`` (SystemdScheduler → ``systemctl is-active`` /
    ``show``). ``installed`` ← unit file on disk, ``running`` ← active with a
    MainPID, ``managed`` ← always true (systemd owns this unit, installed at
    provisioning), ``last_exit`` ← ExecMainStatus, ``backend`` ← "systemd"."""
    base: dict = {
        "installed": False,
        "running": False,
        "managed": True,
        "pid": None,
        "last_exit": None,
        "backend": "systemd",
    }
    try:
        s = get_scheduler().status(SYSTEM_LABEL)
        base["installed"] = bool(s.get("installed"))
        base["running"] = bool(s.get("running"))
        base["pid"] = s.get("pid")
        base["last_exit"] = s.get("last_exit")
    except Exception as e:
        base["error"] = str(e)
    return base


def status() -> dict:
    """
    Return a status dict:
      {
        "installed": bool,   # plist/unit file exists
        "running": bool,     # process is alive
        "managed": bool,     # registered with launchd/systemd
        "pid": int | None,
        "last_exit": str | None,
        "backend": str,      # "launchd" (macOS) | "systemd" (Linux)
      }
    """
    if _is_linux():
        return _status_linux()

    # Check user LaunchAgent first; fall back to system LaunchDaemon.
    system_daemon = SYSTEM_PLIST_PATH.exists()
    installed = PLIST_PATH.exists() or system_daemon
    active_label = SYSTEM_LABEL if (system_daemon and not PLIST_PATH.exists()) else LABEL
    base: dict = {
        "installed": installed,
        "running": False,
        "managed": False,
        "pid": None,
        "last_exit": None,
        "backend": "launchd",
    }

    if not installed:
        return base

    # Scheduler.status() runs `launchctl list <label>` (unsudo'd in the
    # user-domain adapter — same argv as before) and parses the PID /
    # LastExitStatus shape this function originally established. We keep
    # our own "installed" (user OR system plist) and discard the
    # adapter's, which only knows the LaunchAgents dir.
    try:
        g = _user_scheduler()
        s = (g.status(active_label) if g is not None
             else get_scheduler().status(active_label))
        for k in ("managed", "running", "pid", "last_exit"):
            base[k] = s.get(k)
    except Exception as e:
        base["error"] = str(e)

    return base


def _system_log_path() -> Path:
    """The admin-ui daemon's stdout log path for this platform.

    macOS: the ``SYSTEM_LOG_PATH`` literal (``/Users/evolve/.openclaw/...``).
    Linux: derived from the profile's ``user_home_root`` (``/home``) — the
    systemd unit routes stdout to ``{home}/evolve/.openclaw/logs/
    evolve-admin-ui.log`` (deploy.py ``_admin_ui_jobspec``). Derived rather
    than hardcoded so no new ``/Users/`` literal is introduced (lint)."""
    if _is_linux():
        root = get_profile().user_home_root  # "/home" on Linux
        return Path(root) / "evolve" / ".openclaw" / "logs" / "evolve-admin-ui.log"
    return SYSTEM_LOG_PATH


def tail_logs(n: int = 100) -> list[str]:
    """Return last n lines of the admin server log."""
    # Prefer the user LaunchAgent log; fall back to the system daemon log.
    system_log = _system_log_path()
    log = LOG_PATH if LOG_PATH.exists() else (system_log if system_log.exists() else None)
    if log is None:
        return [f"(log file not found: {LOG_PATH})"]
    try:
        r = subprocess.run(
            ["tail", f"-{n}", str(log)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.splitlines()
    except Exception as e:
        return [f"(error reading log: {e})"]
