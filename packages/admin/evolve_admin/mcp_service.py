"""
evolve_admin/mcp_service.py — macOS launchd service management for the MCP bridge.

Manages the ai.evolve.evolve.mcp-bridge LaunchDaemon, which runs the MCP bridge
server persistently as the `evolve` user: starts at boot, restarts on crash, no
terminal required, no Aqua (graphical) session required.

History: the bridge originally shipped as a per-user LaunchAgent under
~/Library/LaunchAgents/com.evolve.mcp-bridge.plist with bootstrap target
gui/<uid>. That scope structurally cannot work on a headless pod — gui/<uid>
domains exist only when that user has an active Aqua login session, and Evolve
pods are designed to be administered via SSH + web UI, not the console. The
plist would be installed but never loaded, the infra audit would file
`daemon_not_loaded` every run, and the suggested `launchctl bootstrap gui/$UID`
fix would fail with error 125 (Domain does not support specified action). The
bridge has no per-user state — it's just an HTTP server listening on
0.0.0.0:5051 — so a system-scope LaunchDaemon is the correct shape.

The label was renamed `com.evolve.mcp-bridge` → `ai.evolve.evolve.mcp-bridge` to
match the other infra daemons (`ai.evolve.evolve.*`) so the existing pod-wide
sudoers grants and `_install_launchd_*` plumbing apply uniformly.

CLI surface:  evolve-admin mcp-bridge {install,uninstall,start,stop,restart,status,logs}
API surface:  GET  /api/mcp/status
              POST /api/mcp/start
              POST /api/mcp/stop
              POST /api/mcp/restart
              POST /api/mcp/install
              POST /api/mcp/uninstall

Install/uninstall require sudo (writing to /Library/LaunchDaemons/). The CLI
and API entrypoints log a clear error if invoked without root; start/stop/
restart use the existing pod-wide sudoers grants for `launchctl kickstart -k
system/ai.evolve.*` so they don't require interactive sudo.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from evolve_config import user_home
from platform_profile import get_profile

from evolve_admin.config import DEFAULT_NETWORK_CONFIG
from evolve_admin.deploy import VENV_PYTHON
from evolve_admin.runtime import (
    JobSpec,
    LaunchdScheduler,
    Scheduler,
    get_launchd_scheduler,
    get_scheduler,
    render_launchd_plist,
)

LABEL = "ai.evolve.evolve.mcp-bridge"

# Legacy label retained ONLY for migration: existing pods may still have a
# LaunchAgent at this label loaded in someone's gui/<uid> domain (or, more
# likely, an unloaded plist file in ~/Library/LaunchAgents/). The uninstall
# path tries to clean both legacy locations so an in-place upgrade does the
# right thing. New code MUST NOT reference this constant; use LABEL.
_LEGACY_AGENT_LABEL = "com.evolve.mcp-bridge"

# ── Platform-resolved paths (lazy — NOT import-time literals) ─────────────────
#
# These were macOS literals (``/Library/LaunchDaemons/<label>.plist`` and
# ``/Users/evolve/.evolve/logs``) that broke under the Linux gate. They are now
# resolved per-platform at USE time, NOT frozen at import:
#
#   * The Scheduler-seam adapters already absorb launchd-vs-systemd, but the
#     macOS install/uninstall/start/status branches still touch the on-disk
#     service-definition file directly (staged-cp + bootstrap), so they need
#     its path; ``daemon_dir`` carries it (``/Library/LaunchDaemons`` vs
#     ``/etc/systemd/system``).
#   * The bridge's log dir is the daemon's ``StandardOutput=`` target on Linux
#     AND the macOS plist's ``StandardOutPath`` — it MUST land in evolve's real
#     home (``/Users/evolve`` on macOS, ``/home/evolve`` on Linux), so it
#     resolves via ``user_home("evolve")`` (pwd-first, profile-keyed fallback),
#     the same POSIX-portable resolution the gateway daemon's log path uses
#     (deploy.py).
#
# Resolution is lazy because the admin suite IMPORTS this module on a Linux CI
# runner BEFORE conftest pins the macOS profile — an import-time literal would
# bake in the wrong platform. ``__getattr__`` below re-exports them as the
# historical module constants (``PLIST_PATH`` / ``LOG_DIR`` / ``LOG_PATH``) so
# ``from evolve_admin.mcp_service import PLIST_PATH, LOG_PATH`` (cli.py) keeps
# working, resolving lazily on each access.


def _service_def_path() -> Path:
    """On-disk service-definition path for the bridge daemon.

    macOS: the launchd plist under ``/Library/LaunchDaemons``. Linux: the
    systemd unit under ``/etc/systemd/system``. Only the macOS install/
    uninstall/start/status branches consult this for file ops; on Linux the
    Scheduler seam owns the unit path, so this value is display-only (cli
    ``status``/``install``)."""
    prof = get_profile()
    suffix = ".plist" if prof.name == "macos" else ".service"
    return Path(prof.daemon_dir) / f"{LABEL}{suffix}"


def _log_dir() -> Path:
    """evolve's ``~/.evolve/logs`` — resolved via pwd so the bridge log lands
    in ``/Users/evolve`` on macOS and ``/home/evolve`` on Linux (the systemd
    unit's ``StandardOutput=`` / the plist's ``StandardOutPath`` target)."""
    return user_home("evolve") / ".evolve" / "logs"


def _log_path() -> Path:
    return _log_dir() / "mcp-bridge.log"


def __getattr__(name: str) -> Path:
    """Lazily expose the platform-resolved path constants (PEP 562).

    Keeps the historical module-attribute surface (``PLIST_PATH`` / ``LOG_DIR``
    / ``LOG_PATH``) for external importers (cli.py) without freezing a
    wrong-platform value at import time. A ``monkeypatch.setattr`` on any of
    these sets a real attribute that shadows this hook; tests that need to
    redirect the *internal* resolution patch the ``_service_def_path`` /
    ``_log_dir`` / ``_log_path`` helpers instead."""
    if name == "PLIST_PATH":
        return _service_def_path()
    if name == "LOG_DIR":
        return _log_dir()
    if name == "LOG_PATH":
        return _log_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _admin_home() -> Path:
    """Home directory of the admin user (where ~/CLAUDE.md gets written).

    The MCP bridge daemon itself no longer cares about per-user homes — it runs
    as `evolve` and listens on a network port. But the Claude Desktop
    integration still wants a CLAUDE.md in the admin's home so the desktop app
    can pick it up. Resolves via SUDO_USER when invoked under sudo, else $HOME.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.getuid() == 0:
        return Path(f"/Users/{sudo_user}")
    return Path.home()


CLAUDE_MD_PATH = _admin_home() / "CLAUDE.md"

_CLAUDE_MD_TEMPLATE = """\
# Evolve Pod — Session Context

At the start of every session, call the `get_context` MCP tool to load the
current pod state. The active bot defaults to **{primary_context_bot}**.

Use `list_bots` to see all bots in the pod and `set_active_bot` to switch
context. Use `create_handoff` to delegate tasks for the bot to pick up on
its next heartbeat.

Pod machine: {tailscale_hostname}
MCP Bridge: connected (port {port})
"""

def write_claude_md(
    primary_context_bot: str,
    port: int = 5051,
    tailscale_hostname: str = "",
) -> Path:
    """Generate ~/CLAUDE.md so Claude Desktop loads pod context at every session start.

    Claude Desktop reads ~/CLAUDE.md as session context when running on this machine.
    The file instructs Claude to call get_context at session start, giving it
    full situational awareness of the pod without the user having to repeat context.
    """
    content = _CLAUDE_MD_TEMPLATE.format(
        primary_context_bot=primary_context_bot,
        port=port,
        tailscale_hostname=tailscale_hostname or f"localhost:{port}",
    )
    claude_md = _admin_home() / "CLAUDE.md"
    claude_md.write_text(content)
    return claude_md


def _bridge_job_spec(
    host: str = "0.0.0.0",
    port: int = 5051,
    network: Path = DEFAULT_NETWORK_CONFIG,
) -> JobSpec:
    """The bridge's platform-neutral :class:`JobSpec`.

    Single source for both the macOS plist renderer (:func:`generate_plist`)
    and the Linux Protocol-verb install path (``get_scheduler().install``).
    The same spec renders to a system-domain ``.service`` (with
    ``Restart=always`` from ``keep_alive`` and ``[Install]`` from
    ``run_at_load``) on a Linux pod, where the bridge unit lives under the
    systemd daemon dir.
    """
    log_path = _log_path()
    return JobSpec(
        label=LABEL,
        program_args=[
            str(VENV_PYTHON), "-m", "evolve_admin.mcp_bridge",
            "--network", str(network),
            "--port", str(port), "--host", host,
        ],
        user="evolve",
        run_at_load=True,
        keep_alive=True,
        stdout_path=str(log_path),
        stderr_path=str(log_path),
        env={"PATH": os.environ.get(
            "PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin")},
    )


def generate_plist(
    host: str = "0.0.0.0",
    port: int = 5051,
    network: Path = DEFAULT_NETWORK_CONFIG,
) -> str:
    return render_launchd_plist(_bridge_job_spec(host, port, network))


# ── Scheduler-seam handles (bite 6 portability migration) ─────────────────────
#
# The MCP bridge is KEPT on a Linux pod (it binds 0.0.0.0:5051 for remote
# Claude Desktop), so this is a real seam migration, NOT a platform-gate-out.
# Two handles because the bridge's launchctl wrappers came in sudo'd and
# unsudo'd flavors; both now ROUTE THROUGH the process-wide ``get_scheduler()``
# singleton so a Linux pod resolves the injected SystemdScheduler (the bridge
# unit) and macOS keeps byte-identical launchctl argv.
#
#   * ``_scheduler_sudo`` — the Protocol-verb sudo handle. ``sudo_non_interactive``
#     (``sudo -n``) is LOAD-BEARING: the bridge's restart/kill/status callers
#     run in TTY-less daemon contexts (the admin-ui daemon, the web API
#     endpoints), where a plain ``sudo`` would BLOCK on a password prompt
#     rather than fail fast. ``get_scheduler()``'s default LaunchdScheduler is
#     sudo-by-default WITHOUT ``-n`` (plain ``sudo``), so a clean swap would
#     silently flip that posture and drop the byte-identical argv. We therefore
#     guarded-derive a ``sudo_non_interactive=True`` launchd adapter ONLY when
#     the active adapter is launchd (reusing the injected fake's runner under
#     test), and on systemd use the injected adapter directly — ``sudo -n`` is
#     a launchd argv concept SystemdScheduler models with its own
#     ``sudo_non_interactive`` posture, which the platform gate's
#     ``set_scheduler()`` injection sets for Linux. Same guarded-derive shape
#     as retire._probe_scheduler / web/routes_trust._get_probe_scheduler.
#     The pod-wide sudoers grants cover ``launchctl bootstrap system
#     /Library/LaunchDaemons/ai.evolve.*.plist``, ``launchctl bootout
#     system/ai.evolve.*``, and ``launchctl kickstart -k system/ai.evolve.*``.
#   * ``_scheduler_nosudo`` — the unsudo'd ``status`` probe handle (system-scope
#     ``launchctl list`` works without sudo). Same guarded-derive: an unsudo'd
#     launchd adapter on macOS (byte-identical no-sudo argv), the injected
#     adapter directly on systemd.
#
# The ``raw()`` bootout/bootstrap/kickstart sites (install/start/uninstall +
# the legacy gui-domain agent sweep) are launchd-verbatim and route through the
# FAIL-FAST ``get_launchd_scheduler`` accessor; each is platform-gated so a
# Linux pod takes the ``get_scheduler()`` Protocol-verb path instead (see
# install/uninstall/start). Tests inject ``LaunchdScheduler(runner=<fake>)``
# via ``set_scheduler()`` with a mandatory ``set_scheduler(None)`` teardown.


def _scheduler_sudo() -> "Scheduler":
    """The sudo'd Protocol-verb handle (restart / kill / status fallback).

    On macOS, a ``sudo_non_interactive=True`` launchd adapter — the
    load-bearing daemon-context fail-fast posture, byte-identical to the
    pre-seam ``LaunchdScheduler(sudo_non_interactive=True, timeout=15.0)``
    argv (``sudo -n /bin/launchctl …``). Reuses the injected fake's runner
    under test so a seam-injected fake still intercepts. On a non-launchd
    pod (systemd), returns the injected adapter directly — ``sudo -n`` is a
    launchd argv concept SystemdScheduler carries via its own posture set by
    the platform gate's ``set_scheduler()`` injection.
    """
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        return sched
    if sched._custom_runner:
        return LaunchdScheduler(
            sudo_non_interactive=True, runner=sched._runner, timeout=15.0,
        )
    return LaunchdScheduler(sudo_non_interactive=True, timeout=15.0)


def _scheduler_nosudo() -> "Scheduler":
    """The unsudo'd ``status`` probe handle.

    On macOS, an unsudo'd launchd adapter (byte-identical no-sudo
    ``/bin/launchctl list <label>`` argv). On systemd, the injected adapter
    directly (``status()`` is portable; sudo posture is launchd-only).
    Same guarded-derive shape as ``_scheduler_sudo``.
    """
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        return sched
    if sched._custom_runner:
        return LaunchdScheduler(
            use_sudo=False, runner=sched._runner, timeout=15.0,
        )
    return LaunchdScheduler(use_sudo=False, timeout=15.0)


def _sudo_raw_adapter() -> "LaunchdScheduler":
    """A ``sudo -n`` launchd adapter for the macOS install/start/uninstall
    ``raw()`` bootout/bootstrap/kickstart sites.

    ``raw()`` is launchd-verbatim (NOT on the ``Scheduler`` Protocol), so these
    sites route through the FAIL-FAST ``get_launchd_scheduler`` accessor (raises
    on a non-launchd adapter; each call site is macOS-gated, so that is
    impossible). Derives a ``sudo_non_interactive=True`` variant that shares the
    verified launchd singleton's runner (so a seam-injected fake still
    intercepts), preserving the EXACT pre-seam argv: the pre-seam
    ``_scheduler_sudo()`` was ``sudo_non_interactive=True``, so its verbatim
    launchctl escape hatch emitted ``sudo -n /bin/launchctl …`` — the
    load-bearing daemon-context fail-fast posture (a plain ``sudo`` would block
    on a password prompt in the TTY-less admin-ui daemon / web API context).
    """
    base = get_launchd_scheduler()
    if base._custom_runner:
        return LaunchdScheduler(
            sudo_non_interactive=True, runner=base._runner, timeout=15.0,
        )
    return LaunchdScheduler(sudo_non_interactive=True, timeout=15.0)


def _nosudo_raw_adapter() -> "LaunchdScheduler":
    """An unsudo'd launchd adapter for the legacy gui-domain ``raw()`` bootout.

    ``raw()`` is launchd-verbatim (NOT on the ``Scheduler`` Protocol), so its
    one remaining call site — the macOS-gated legacy LaunchAgent sweep in
    ``_bootout_legacy_agents`` — routes through the FAIL-FAST
    ``get_launchd_scheduler`` accessor (raises on a non-launchd adapter; the
    macOS platform gate at the call site makes that impossible). Derives an
    unsudo'd variant that shares the verified launchd singleton's runner (so a
    seam-injected fake still intercepts), preserving the no-sudo argv the
    pre-seam unsudo'd verbatim-launchctl escape hatch emitted (the caller is
    already root in the gui domain; no sudo prefix wanted).
    """
    base = get_launchd_scheduler()
    if base._custom_runner:
        return LaunchdScheduler(use_sudo=False, runner=base._runner, timeout=15.0)
    return LaunchdScheduler(use_sudo=False, timeout=15.0)


def _legacy_agent_plist_paths() -> list[Path]:
    """Locations where the pre-system-scope LaunchAgent plist might exist.

    Covers (a) the admin's home (where sudo-invoked installs landed) and (b)
    the evolve user's home (where daemon-context installs landed). Both shapes
    have been observed in the wild on the same pod.
    """
    paths: list[Path] = []
    seen: set[str] = set()
    candidates = [_admin_home(), Path("/Users/evolve")]
    for home in candidates:
        p = home / "Library" / "LaunchAgents" / f"{_LEGACY_AGENT_LABEL}.plist"
        if str(p) not in seen:
            paths.append(p)
            seen.add(str(p))
    return paths


def _bootout_legacy_agents() -> list[str]:
    """Best-effort bootout of any legacy LaunchAgent registrations.

    Returns a list of human-readable status lines for logging. Never raises.

    macOS-only: the legacy bridge shipped as a per-user ``gui/<uid>``
    LaunchAgent under ``~/Library/LaunchAgents/`` (see the module docstring) —
    a construct a Linux pod does not have (the Linux platform uses
    system-domain systemd units only; per-user units were rejected in the
    design, and a fresh Linux pod was never the user-agent era). On a non-macOS
    pod this is a clean no-op rather than a launchctl call that would fail —
    the same platform-gate-out shape as ocadmin._remove_conflicting_user_agents.
    """
    if get_profile().name != "macos":
        return []

    notes: list[str] = []
    # Try the admin user's gui/<uid> domain — the only place a legacy agent
    # could plausibly have been loaded. We probe SUDO_USER's uid because
    # _admin_home() uses the same resolution.
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.getuid() == 0:
        try:
            rid = subprocess.run(
                ["/usr/bin/id", "-u", sudo_user],
                capture_output=True, text=True, timeout=5,
            )
            if rid.returncode == 0:
                uid = rid.stdout.strip()
                # raw(): gui/<uid> domain — the system-domain adapter
                # doesn't model gui domains, and this caller is already
                # root (no sudo prefix wanted). Routed through the FAIL-FAST
                # launchd-typed accessor: it raises loudly on a non-launchd
                # adapter, but the macOS platform gate above makes that
                # impossible here. The no-sudo posture is preserved by
                # building an unsudo'd adapter that shares the verified
                # singleton's runner (the same guarded-derive shape as
                # _scheduler_nosudo) — the fail-fast launchd-typed accessor
                # guards the platform, _nosudo_raw_adapter preserves the argv.
                rc, _o, _e = _nosudo_raw_adapter().raw(
                    "bootout", f"gui/{uid}/{_LEGACY_AGENT_LABEL}"
                )
                if rc == 0:
                    notes.append(f"bootout gui/{uid}/{_LEGACY_AGENT_LABEL}: ok")
                # rc != 0 is the common case (no such service) — silent.
        except (subprocess.SubprocessError, OSError):
            pass

    for p in _legacy_agent_plist_paths():
        if p.exists():
            try:
                p.unlink()
                notes.append(f"removed legacy plist {p}")
            except PermissionError:
                # File is root-owned (one of the two real-world cases); use sudo.
                r = subprocess.run(
                    ["sudo", "-n", "/bin/rm", str(p)],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    notes.append(f"removed legacy plist {p} (sudo)")
                else:
                    notes.append(f"could not remove legacy plist {p}: {(r.stderr or '').strip()}")
            except OSError as e:
                notes.append(f"could not remove legacy plist {p}: {e}")
    return notes


def _write_system_plist(content: str) -> tuple[bool, str]:
    """Stage the plist in /tmp and `sudo /bin/cp` it into /Library/LaunchDaemons/.

    Follows the standard infra-plist install pattern (chown root:wheel,
    chmod 644) the Scheduler seam's LaunchdScheduler.install also implements.
    Short-circuits if the existing file already has byte-identical content so
    reinstalls don't churn launchd.

    macOS-only: callers gate on the macOS profile before staging a plist (Linux
    installs render a systemd unit through the Scheduler seam, no staged cp).
    """
    plist_path = _service_def_path()
    try:
        if plist_path.exists() and plist_path.read_text() == content:
            return True, "up-to-date"
    except (OSError, PermissionError):
        pass

    fd, tmp_path = tempfile.mkstemp(dir="/tmp", suffix=".plist", prefix="evolve-mcp-bridge-")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    try:
        r = subprocess.run(
            ["sudo", "-n", "/bin/cp", tmp_path, str(plist_path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"sudo cp failed: {(r.stderr or '').strip() or 'unknown error'}"
        for cmd in (
            ["sudo", "-n", "/usr/sbin/chown", "root:wheel", str(plist_path)],
            ["sudo", "-n", "/bin/chmod", "644", str(plist_path)],
        ):
            rc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if rc.returncode != 0:
                return False, f"{cmd[2]} failed: {(rc.stderr or '').strip() or 'unknown error'}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return True, "written"


def install(
    host: str = "0.0.0.0",
    port: int = 5051,
    network: Path = DEFAULT_NETWORK_CONFIG,
) -> tuple[bool, str]:
    """Write plist and load the LaunchDaemon. Returns (ok, message).

    Also cleans up any pre-existing LaunchAgent (legacy `com.evolve.mcp-bridge`)
    so an upgrade-in-place from the user-scope era doesn't leave a stale plist
    that the infra audit would then flag.
    """
    _log_dir().mkdir(parents=True, exist_ok=True)

    legacy_notes = _bootout_legacy_agents()

    if get_profile().name != "macos":
        # Linux pod: systemd has no plist + no staged-cp ritual. The Protocol
        # ``install`` verb renders the bridge unit under the systemd daemon dir
        # (``/etc/systemd/system``), daemon-reloads, enables and restarts it
        # atomically — the bootout+bootstrap analogue. The legacy
        # ``~/Library/LaunchAgents`` sweep above is already a no-op off macOS,
        # and ``_write_claude_md_from_network`` stays best-effort.
        # install() returns InstallResult (a 3-field NamedTuple); read .ok /
        # .message explicitly rather than 2-tuple-unpacking it.
        res = get_scheduler().install(
            _bridge_job_spec(host, port, network), timeout=15.0,
        )
        if not res.ok:
            return False, f"Could not install MCP bridge unit: {res.message}"
        for _ in range(12):
            time.sleep(0.5)
            s = status()
            if s.get("running"):
                _write_claude_md_from_network(network, port)
                return True, f"MCP bridge installed and running (PID {s.get('pid')})"
        s = status()
        if s.get("installed"):
            _write_claude_md_from_network(network, port)
            return True, "MCP bridge installed. Process starting — check status in a moment."
        return False, f"MCP bridge installed but process did not start. Check logs: {_log_path()}"

    plist_path = _service_def_path()
    content = generate_plist(host, port, network)
    ok, write_msg = _write_system_plist(content)
    if not ok:
        return False, f"Could not install plist at {plist_path}: {write_msg}"

    # Bootout any existing registration so the new plist's KeepAlive/Run
    # config takes effect cleanly. Ignore rc — "service not loaded" is fine.
    # raw() rather than Scheduler.install(): the plist was staged by
    # _write_system_plist (which uses ``sudo -n`` for cp/chown — the
    # adapter's install() writes with plain sudo, which could block a
    # TTY-less daemon context on a missing grant), and install() skips
    # re-registering a booted-out service when content is byte-identical,
    # breaking this function's install-always-(re)starts contract.
    # Routed through the FAIL-FAST launchd-typed accessor (raises on a
    # non-launchd adapter — impossible here, the macOS branch is gated above).
    # The raw argv carries the sudo prefix, and the pre-seam handle was
    # ``sudo_non_interactive=True`` (``sudo -n``), so _sudo_raw_adapter
    # preserves that exact ``sudo -n /bin/launchctl …`` posture.
    sched = _sudo_raw_adapter()
    sched.raw("bootout", f"system/{LABEL}")
    rc, b_out, b_err = sched.raw("bootstrap", "system", str(plist_path))
    if rc != 0:
        return False, f"launchctl bootstrap failed: {(b_out + b_err).strip()}"

    for _ in range(12):
        time.sleep(0.5)
        s = status()
        if s.get("running"):
            _write_claude_md_from_network(network, port)
            msg = f"MCP bridge installed and running (PID {s.get('pid')})"
            if legacy_notes:
                msg += f"\n  legacy cleanup: {'; '.join(legacy_notes)}"
            return True, msg

    s = status()
    if s.get("installed"):
        _write_claude_md_from_network(network, port)
        return True, "MCP bridge installed. Process starting — check status in a moment."
    return False, f"MCP bridge installed but process did not start. Check logs: {_log_path()}"


def _write_claude_md_from_network(network: Path, port: int) -> None:
    """Read mcp_bridge config from network.json and write ~/CLAUDE.md."""
    try:
        from evolve_admin.config import load_network
        net = load_network(network)
        mcp_cfg = net.get("mcp_bridge", {})
        primary_bot = (
            mcp_cfg.get("primary_context_bot")
            or net.get("primary")
            or next(iter(net.get("bots", {})), None)
        )
        if not primary_bot:
            return
        tailscale_hostname = mcp_cfg.get("tailscale_hostname", "")
        write_claude_md(primary_bot, port, tailscale_hostname)
    except Exception:
        pass  # CLAUDE.md is best-effort; don't block install on failure


def uninstall(quiet: bool = False) -> tuple[bool, str]:
    """Bootout the service and remove its on-disk definition. Also sweeps
    legacy agents (a macOS no-op off macOS)."""
    legacy_notes = _bootout_legacy_agents()

    if get_profile().name != "macos":
        # Linux pod: the Protocol ``remove`` verb is ``disable --now`` + unit
        # deletion + ``daemon-reload`` atomically — the bootout+rm analogue.
        # Idempotent: a not-installed unit returns ok. ``installed`` keys on
        # the service unit file's existence (status()'s portable field).
        was_installed = status().get("installed")
        ok, msg = get_scheduler().remove(LABEL, timeout=15.0)
        if not ok:
            return False, f"Could not remove MCP bridge unit: {msg}"
        if not was_installed and not legacy_notes:
            if quiet:
                return True, "not installed"
            return False, "MCP bridge not installed"
        return True, "MCP bridge uninstalled."

    plist_path = _service_def_path()
    plist_existed = plist_path.exists()
    if plist_existed:
        # raw(): bare bootout — the plist removal below keeps its own
        # ``sudo -n /bin/rm`` with separately-surfaced diagnostics
        # (Scheduler.remove() folds the rm in and uses plain sudo). Routed
        # through the FAIL-FAST launchd-typed accessor with the ``sudo -n``
        # posture preserved (see _sudo_raw_adapter); macOS-gated above.
        _sudo_raw_adapter().raw("bootout", f"system/{LABEL}")
        r = subprocess.run(
            ["sudo", "-n", "/bin/rm", str(plist_path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"Could not remove plist: {(r.stderr or '').strip() or 'unknown error'}"

    if not plist_existed and not legacy_notes:
        if quiet:
            return True, "not installed"
        return False, f"MCP bridge not installed (no plist at {plist_path})"

    msg_parts = []
    if plist_existed:
        msg_parts.append("MCP bridge uninstalled.")
    if legacy_notes:
        msg_parts.append(f"legacy cleanup: {'; '.join(legacy_notes)}")
    return True, " ".join(msg_parts) or "uninstalled"


def start() -> tuple[bool, str]:
    if get_profile().name != "macos":
        # Linux pod: the "installed" axis is the service unit file on disk
        # (status()'s portable field). If it's not already running, restart()
        # (``systemctl restart``) starts the stopped unit; an already-running
        # bridge is left untouched (start() is non-destructive — it must not
        # bounce a healthy bridge, unlike restart()).
        s = status()
        if not s.get("installed"):
            return False, "MCP bridge not installed — run 'sudo evolve-admin mcp-bridge install' first"
        _log_dir().mkdir(parents=True, exist_ok=True)
        if s.get("running"):
            return True, "Started"
        ok, out = get_scheduler().restart(LABEL, timeout=15.0)
        return ok, out.strip() or "Started"

    plist_path = _service_def_path()
    if not plist_path.exists():
        return False, "MCP bridge not installed — run 'sudo evolve-admin mcp-bridge install' first"
    _log_dir().mkdir(parents=True, exist_ok=True)
    s = status()
    sched = _sudo_raw_adapter()
    if not s.get("managed"):
        # Plist exists on disk but is not bootstrapped into the system domain
        # (kickstart would fail with "Domain does not support specified action").
        # raw() rather than Scheduler.install(): the on-disk plist is
        # byte-identical to what install() would render, so install()
        # would skip exactly the re-registration this branch exists for.
        # Routed through the FAIL-FAST launchd-typed accessor with the
        # ``sudo -n`` posture preserved (see _sudo_raw_adapter); macOS-gated.
        rc, b_out, b_err = sched.raw("bootstrap", "system", str(plist_path))
        return rc == 0, (b_out + b_err).strip() or "Started"
    # raw(): bare ``kickstart`` (no -k) — start-if-not-running. The
    # Scheduler.restart() verb is kickstart -k, which would kill a
    # healthy running bridge; start() must not.
    rc, k_out, k_err = sched.raw("kickstart", f"system/{LABEL}")
    return rc == 0, (k_out + k_err).strip() or "Started"


def stop() -> tuple[bool, str]:
    # SIGTERM via Scheduler.kill keeps the service registered (KeepAlive will
    # be throttled by launchd's restart backoff); avoids the legacy
    # `launchctl stop` which doesn't work from system context. The 15s bound
    # is passed per-call so it holds on both the macOS guarded-derive adapter
    # and the systemd adapter (whose singleton default is 30s).
    ok, out = _scheduler_sudo().kill(LABEL, timeout=15.0)
    return ok, out or "Stopped"


def restart() -> tuple[bool, str]:
    # SIGHUP first so the registry refreshes from network.json immediately.
    # ``installed`` is the portable on-disk axis (plist on macOS, service unit
    # on Linux) — status() computes it per platform, so this no longer hard-codes
    # the macOS PLIST_PATH literal.
    s = status()
    if not s.get("installed"):
        return False, "MCP bridge not installed"
    if s.get("pid"):
        try:
            import signal
            os.kill(int(s["pid"]), signal.SIGHUP)
            time.sleep(0.5)
        except (OSError, ValueError, PermissionError):
            # PermissionError when the caller is the evolve user and the bridge
            # also runs as evolve — same uid, allowed. If somehow not, fall
            # through to kickstart -k below which restarts via launchd.
            pass
    # 15s bound passed per-call (see stop()).
    ok, out = _scheduler_sudo().restart(LABEL, timeout=15.0)
    return ok, out or "Restarted"


def reload_registry() -> tuple[bool, str]:
    """
    Send SIGHUP to the running bridge to trigger an immediate registry reload
    from network.json. Called by Evolve after adding or removing a bot.
    """
    s = status()
    pid = s.get("pid")
    if not pid:
        return False, "MCP bridge is not running"
    try:
        import signal
        os.kill(int(pid), signal.SIGHUP)
        return True, f"SIGHUP sent to PID {pid} — registry will reload from network.json"
    except (OSError, ValueError) as e:
        return False, f"Failed to send SIGHUP: {e}"


def status() -> dict:
    """
    Return a status dict:
      {
        "installed": bool,
        "running": bool,
        "managed": bool,
        "pid": int | None,
        "last_exit": str | None,
      }
    """
    base: dict = {
        "installed": False,
        "running": False,
        "managed": False,
        "pid": None,
        "last_exit": None,
    }

    # On macOS, short-circuit when the plist is absent so an uninstalled
    # bridge costs zero launchctl calls (the historical fast path). On Linux
    # the "installed" axis is the service unit file on disk, which the
    # Protocol status() verb reports — so we skip this macOS-only literal and
    # let the verb answer (the systemd adapter's status()["installed"] checks
    # the .service file under the daemon dir).
    if get_profile().name == "macos" and not _service_def_path().exists():
        return base

    # System-scope: `launchctl list` works without sudo for any caller
    # because it only enumerates services in the caller's launchd domain
    # *plus* the system domain. Falls back to sudo -n if needed.
    # Scheduler.status() does the `launchctl list <label>` parse
    # (PID / LastExitStatus) and never raises. On systemd both handles
    # resolve to the same injected adapter, so the fallback is a harmless
    # second `systemctl show`. 15s bound passed per-call (see stop()).
    st = _scheduler_nosudo().status(LABEL, timeout=15.0)
    if not st["managed"]:
        st = _scheduler_sudo().status(LABEL, timeout=15.0)
    base.update(
        installed=st["installed"], managed=st["managed"], running=st["running"],
        pid=st["pid"], last_exit=st["last_exit"],
    )
    return base


def tail_logs(n: int = 100) -> list[str]:
    log = _log_path()
    if not log.exists():
        return [f"(log file not found: {log})"]
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
