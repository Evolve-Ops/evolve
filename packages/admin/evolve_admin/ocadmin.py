"""
ocadmin.py — interactive admin menu + flat subcommands.

Handles bot-keyed operations that span OpenClaw gateways and per-bot config:
npm upgrades, usage analytics, plugin allow/deny lists, exec approvals,
calendars, GWS reauth, logs, and processes.

Entry point: `evolve-admin menu` (interactive single-letter menu by default,
or run a flat subcommand like `evolve-admin menu plugins team_bot_a`).

A hidden `evolve-admin oc` alias is preserved for backwards compatibility.
"""

from __future__ import annotations

import csv
import json
import os
import re
import stat
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from platform_profile import get_profile as _get_profile

from .config import (
    load_network,
    resolve_admin_base_url,
    user_home,
    DEFAULT_NETWORK_CONFIG,
    DEFAULT_SHARED_DIR,
)
from .runtime import get_launchd_scheduler, get_scheduler

console = Console()

# ── OpenClaw package location ─────────────────────────────────────────────────
#
# Platform-keyed via platform_profile (#3194 follow-up): on Linux OpenClaw is at
# `/usr/lib/node_modules/openclaw` (npm `root -g`=/usr/lib/node_modules,
# `prefix -g`=/usr), NOT the macOS Homebrew prefix the Linux gateway never loads
# from. These resolve byte-identically to the prior `/opt/homebrew/...` values on
# the macOS profile (pinned by platform_profile tests). No `sys.platform` branch
# here — everything flows through `get_profile()` (imported at top).


def _resolve_openclaw_package_json() -> Path:
    """`openclaw/package.json` to read the installed version from — the first
    platform candidate that exists, else the first candidate (so the error
    string still names a sensible path). Mirrors
    `update_watcher.read_installed_openclaw_version`."""
    candidates = _get_profile().openclaw_pkg_json_candidates
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


OPENCLAW_PACKAGE_JSON = _resolve_openclaw_package_json()
OPENCLAW_NPM_REGISTRY = "https://registry.npmjs.org/openclaw/latest"
OPENCLAW_NPX          = _get_profile().npx_bin

# npm prefix `npm install -g` MUST target so the upgrade lands in the global
# node_modules the gateway/systemd unit actually loads from. We force `--prefix`
# because npm's default prefix lives inside the versioned install dir
# (e.g. Homebrew's /opt/homebrew/Cellar/node/<ver>), which an unprefixed
# `npm install -g` would target — silently orphaning the upgrade while the
# gateways keep running the stale copy at the canonical path. macOS →
# /opt/homebrew (byte-identical to the prior hardcode); Linux → /usr.
OPENCLAW_NPM_PREFIX   = _get_profile().npm_global_prefix


def _npm_node_modules_dir() -> Path:
    """Directory where `npm install -g --prefix=<OPENCLAW_NPM_PREFIX>` lands
    packages. Also where `.openclaw-XXX` rename-staging dirs live during
    install, and where they get stranded if a prior install bailed."""
    return Path(OPENCLAW_NPM_PREFIX) / "lib" / "node_modules"


def _openclaw_cli_path() -> str:
    """Absolute path to a runnable ``openclaw`` CLI on this host, for ad-hoc
    operator invocations (e.g. the per-bot ``doctor`` menu action).

    Mirrors ``update_watcher._find_openclaw_cli``: search the platform's exec
    PATH dirs for ``<dir>/openclaw`` (Homebrew prefixes on macOS, /usr/bin on
    Linux where NodeSource lands it), then fall back to ``shutil.which``. No
    ``sys.platform`` branch — the candidate dirs come from ``get_profile()``,
    so this stays byte-identical to the prior ``/opt/homebrew/bin/openclaw``
    hardcode on the macOS profile. Returns the bare ``"openclaw"`` if nothing
    resolves, so the caller still forms a runnable command that fails with a
    useful error rather than an empty path."""
    for d in _get_profile().exec_path_dirs:
        cand = Path(d) / "openclaw"
        if cand.exists():
            return str(cand)
    import shutil
    return shutil.which("openclaw") or "openclaw"

# Generic user-level LaunchAgent that openclaw's npm post-install drops.
# When a system daemon already exists for a bot, this causes duplicate processes.
_GENERIC_USER_AGENT = "ai.openclaw.gateway.plist"

# ── Bot helpers ───────────────────────────────────────────────────────────────

_GATEWAY_PROC_MARKERS = (
    "openclaw-gateway",          # process title set by openclaw < 2026.4.29
    "openclaw/dist/entry.js",    # node entry point, openclaw >= 2026.4.29
    "openclaw/dist/index.js",    # older entry point
    "openclaw/openclaw.mjs",     # alternate entry point
)

def _is_gateway_proc(ps_line: str, user: str) -> bool:
    """Return True if a ps aux line belongs to an openclaw gateway for this user."""
    parts = ps_line.split()
    if not parts or parts[0] != user:
        return False
    return any(m in ps_line for m in _GATEWAY_PROC_MARKERS)


def _gateway_runtime_user(bot_id: str, network: dict) -> str:
    """Return the POSIX username the bot's gateway daemon actually runs as.

    Reads UserName from the bot's launchd plist (authoritative for
    launchctl operations — the daemon runs as whatever the plist says,
    even if network.json has drifted), then falls back to the network
    config, then to the bot_id itself. Deliberately NOT the canonical
    evolve_config.get_bot_user: that answers "who should it be", this
    answers "who is it right now".
    """
    # Authoritative: launchd plist
    plist = Path(f"/Library/LaunchDaemons/ai.openclaw.{bot_id}-gateway.plist")
    if plist.exists():
        try:
            import plistlib
            data = plistlib.loads(plist.read_bytes())
            user = data.get("UserName")
            if user:
                return user
        except Exception:
            pass
    # Network config override
    bot_cfg = network.get("bots", {}).get(bot_id, {})
    if "user" in bot_cfg:
        return bot_cfg["user"]
    return bot_id


def _bot_ids(network: dict) -> list[str]:
    """Return all bot IDs from network config."""
    return list(network.get("bots", {}).keys())


def _bot_service(bot_id: str) -> str:
    return f"ai.openclaw.{bot_id}-gateway"


# Gateway kickstarts route through the platform-portable Scheduler seam
# (``get_scheduler()`` → LaunchdScheduler on macOS, SystemdScheduler on a
# Linux pod via the platform gate's ``set_scheduler()`` injection). ocadmin
# is an interactive CLI that runs as root (``sudo evolve-admin menu`` enforces
# ``geteuid()==0``; ``upgrade`` / ``safe-upgrade`` are documented
# ``sudo evolve-admin menu …``), so the seam's sudo-by-default posture is a
# redundant — but behavior-identical — prefix on a system-domain
# ``kickstart -k`` that already requires root, and is the correct posture on
# Linux (``systemctl`` needs root). The pre-seam handle used
# ``use_sudo=False`` only to drop that redundant prefix; nothing depended on
# the no-sudo posture (see ``_restart_gateway``). The gui-domain raw launchctl
# operations in ``_remove_conflicting_user_agents`` have no systemd analogue
# (per-user LaunchAgents don't exist on a Linux pod), so they are gated to
# macOS and routed through the fail-fast launchd-typed accessor.


def _openclaw_json(user: str) -> Path:
    return Path(f"/Users/{user}/.openclaw/openclaw.json")


def _auth_json(user: str) -> Path:
    return Path(f"/Users/{user}/.openclaw/agents/main/agent/auth-profiles.json")


def _exec_approvals_json(user: str) -> Path:
    return Path(f"/Users/{user}/.openclaw/exec-approvals.json")


def _gateway_log(user: str) -> Path:
    return Path(f"/Users/{user}/.openclaw/logs/gateway.log")


def _gateway_err_log(user: str) -> Path:
    return Path(f"/Users/{user}/.openclaw/logs/gateway.err.log")


def _load_json_permissive(path: Path) -> dict:
    """Load JSON, stripping trailing commas (openclaw.json may have them)."""
    content = path.read_text()
    content = re.sub(r',(\s*[}\]])', r'\1', content)
    return json.loads(content)


def _preserve_write(data: dict, path: Path) -> None:
    """Write JSON preserving the file's existing ownership and permissions."""
    try:
        st = os.stat(path)
        uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        uid = gid = mode = None

    path.write_text(json.dumps(data, indent=2))

    if uid is not None:
        try:
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        except OSError as e:
            console.print(f"[yellow]  ⚠️  Could not restore permissions on {path}: {e}[/]")


# ── Version helpers ───────────────────────────────────────────────────────────

def _installed_version() -> str:
    try:
        return json.loads(OPENCLAW_PACKAGE_JSON.read_text()).get("version", "unknown")
    except Exception as e:
        return f"(error: {e})"


def _latest_version() -> str:
    try:
        req = urllib.request.Request(
            OPENCLAW_NPM_REGISTRY,
            headers={"Accept": "application/json", "User-Agent": "evolve-admin/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("version", "unknown")
    except Exception as e:
        return f"(error: {e})"


# ── Conflicting LaunchAgent cleanup ───────────────────────────────────────────

def _remove_conflicting_user_agents(network: dict) -> None:
    """Remove generic ai.openclaw.gateway user LaunchAgents installed by npm post-install.

    When openclaw upgrades via npm, its post-install script drops a generic
    ai.openclaw.gateway.plist into each bot user's ~/Library/LaunchAgents/.
    Because each bot already has a dedicated system daemon in /Library/LaunchDaemons/,
    this creates a duplicate gateway process that respawns indefinitely.

    Safe: only removes the generic agent when a system daemon exists for that bot.
    If no system daemon is present the user agent may be intentional — leave it.

    Handles both cases:
    - plist still active (ai.openclaw.gateway.plist)
    - plist already renamed to .DISABLED but service still loaded in launchd

    macOS-only: this manages per-user ``gui/<uid>`` LaunchAgents under
    ``~/Library/LaunchAgents/`` — a construct a Linux pod does not have (the
    Linux platform uses system-domain systemd units only; per-user units were
    rejected in the design). On a non-macOS pod this is a clean no-op rather
    than a launchctl call that would fail.
    """
    from platform_profile import get_profile

    if get_profile().name != "macos":
        return

    for bot_id in _bot_ids(network):
        user        = _gateway_runtime_user(bot_id, network)
        agent_dir   = Path(f"/Users/{user}/Library/LaunchAgents")
        agent_path  = agent_dir / _GENERIC_USER_AGENT
        disabled_path = agent_dir / (_GENERIC_USER_AGENT[:-len(".plist")] + ".plist.DISABLED")
        daemon_path = Path(f"/Library/LaunchDaemons/{_bot_service(bot_id)}.plist")

        if not daemon_path.exists():
            # No system daemon — user agent may be intentional, leave it
            continue

        # Resolve UID for bootout
        try:
            uid = int(subprocess.run(
                ["id", "-u", user], capture_output=True, text=True
            ).stdout.strip())
        except Exception:
            console.print(f"  [yellow]⚠️  {bot_id}: could not resolve uid for {user} — skipping[/]")
            continue

        # Check if the generic service is still loaded in the gui domain,
        # regardless of whether the plist file still exists or was renamed.
        # Renaming to .DISABLED does NOT unload it from launchd.
        # raw(): per-user ``asuser … list`` probe — the system-domain
        # adapter doesn't model per-user gui domains and there is no
        # Scheduler-verb equivalent for asuser indirection. Reached only on
        # macOS (the function early-returns otherwise), so the fail-fast
        # launchd-typed accessor never raises here.
        svc_label = _GENERIC_USER_AGENT[:-len(".plist")]  # "ai.openclaw.gateway"
        _rc, _out, _err = get_launchd_scheduler().raw(
            "asuser", str(uid), "/bin/launchctl", "list", svc_label,
        )
        # Belt-and-suspenders: check both exit code AND stdout content.
        # On macOS, `launchctl asuser` can return non-zero for non-interactive
        # sessions even when the service IS loaded, so we treat any output
        # containing the service label as confirmation it's loaded.
        loaded = _rc == 0 or svc_label in _out

        plist_exists = agent_path.exists()

        if not loaded and not plist_exists:
            continue  # Nothing to do

        actions = []

        if loaded:
            # raw(): gui/<uid> domain bootout — not modelled by the
            # system-domain adapter; the plist rename below (not an rm)
            # rules out Scheduler.remove() anyway. macOS-only path (the
            # function early-returns on a non-macOS pod).
            b_rc, _b_out, b_err = get_launchd_scheduler().raw(
                "bootout", f"gui/{uid}/{svc_label}",
            )
            actions.append("unloaded" if b_rc == 0
                           else f"bootout failed ({b_err.strip() or 'unknown'})")

        # Rename active plist to .DISABLED so it won't reload on next login
        if plist_exists:
            r = subprocess.run(
                ["sudo", "/bin/mv", str(agent_path), str(disabled_path)],
                capture_output=True, text=True,
            )
            actions.append(
                f"renamed → {disabled_path.name}" if r.returncode == 0
                else f"rename failed ({r.stderr.strip() or 'unknown'})"
            )

        console.print(f"  [green]✅ {bot_id}:[/] conflicting user agent: {', '.join(actions)}")


# ── Click group ───────────────────────────────────────────────────────────────

@click.group("menu", invoke_without_command=True)
@click.pass_context
def menu_group(ctx: click.Context) -> None:
    """Interactive admin menu — bot config, plugins, exec-approvals, calendars, logs, usage.

    Run with no subcommand for the full single-letter interactive menu. Each
    item below is also available as a flat subcommand for scripting
    (e.g. `evolve-admin menu plugins team_bot_a`).
    """
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand is None:
        ctx.invoke(oc_menu)


# Backwards-compatible alias for `evolve-admin oc ...` is defined at the
# bottom of this module, after all menu_group subcommands have been
# registered, so the alias shares the same commands dict.


# ── oc version ────────────────────────────────────────────────────────────────

@menu_group.command("version")
@click.pass_context
def oc_version(ctx: click.Context) -> None:
    """Show installed vs latest OpenClaw version and running gateway processes."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)

    installed = _installed_version()
    console.print(f"\n  Installed:  [bold]{installed}[/]")
    console.print("  Checking npm registry…", end="")
    latest = _latest_version()
    console.print(f"\r  Latest:     [bold]{latest}[/]      ")

    if latest.startswith("(error"):
        console.print(f"  [yellow]⚠️  Could not reach npm registry.[/]")
    elif installed == latest:
        console.print("  [green]✅ Up to date.[/]")
    else:
        console.print(f"  [yellow]🔼 Update available: {installed} → {latest}[/]")

    # Gateway process table
    t = Table(show_header=True, header_style="bold blue")
    t.add_column("Bot")
    t.add_column("User")
    t.add_column("PID(s)")
    t.add_column("Status")

    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    for bot_id in _bot_ids(network):
        user = _gateway_runtime_user(bot_id, network)
        procs = [l for l in result.stdout.splitlines()
                 if _is_gateway_proc(l, user)]
        if not procs:
            t.add_row(bot_id, user, "—", "[red]not running[/]")
        elif len(procs) == 1:
            pid = procs[0].split()[1]
            t.add_row(bot_id, user, pid, "[green]✅ ok[/]")
        else:
            pids = ", ".join(l.split()[1] for l in procs)
            t.add_row(bot_id, user, pids, f"[yellow]⚠️  {len(procs)} processes[/]")

    console.print()
    console.print(t)

    # Auto-updater state
    state_path = Path("/Users/Shared/openclaw-updater-state.json")
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            last_check = state.get("last_check", "never")
            last_ver   = state.get("last_applied_version", "unknown")
            console.print(f"\n  Auto-updater last check:   {str(last_check)[:19] if last_check else 'never'}")
            console.print(f"  Auto-updater last applied: {last_ver}")
        except Exception:
            pass


# ── oc upgrade — npm stale-temp-dir helpers ──────────────────────────────────
#
# npm's atomic-swap install renames the in-place package dir to a
# `.<name>-XXX` sibling before moving the freshly-extracted tarball into
# position. If a prior `sudo npm install -g` was killed partway (Ctrl-C,
# OOM, network hiccup), that `.openclaw-XXX` dir gets stranded — owned
# by root, dated whenever the failure happened — and the next install's
# rename step fails with `ENOTEMPTY: directory not empty`. We detect
# this both proactively (preflight glob) and reactively (post-failure
# stderr classification) so the operator doesn't have to diagnose it.

def _find_stale_npm_temp_dirs() -> list[Path]:
    """Return any leftover `.openclaw-*` dirs in the install node_modules.

    These are residue from a prior failed `npm install -g openclaw` —
    npm renames the live `openclaw` dir to a `.openclaw-XXX` sibling as
    part of its atomic swap, and if the install bails before completion
    the sibling never gets cleaned up. Subsequent installs then fail
    with `ENOTEMPTY` on the same rename step.
    """
    try:
        return sorted(_npm_node_modules_dir().glob(".openclaw-*"))
    except OSError:
        return []


def _dir_age_days(path: Path) -> int | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    delta = datetime.now(tz=timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)
    return delta.days


def _openclaw_install_has_manifest(install_dir: Path) -> bool:
    """True iff ``install_dir`` has a parseable ``package.json`` carrying a
    version. A dir lacking this is unambiguously a husk, not a real install.

    This is the predicate that gates the *destructive* direction of recovery:
    we only ever delete the live ``openclaw`` dir when it has NO valid manifest,
    so an upstream change that relocates an entrypoint can never make us destroy
    a real, manifest-bearing install (it would just read as "not healthy" and
    fall through to the safe delete-residue path)."""
    try:
        pkg = json.loads((install_dir / "package.json").read_text())
    except (OSError, ValueError):
        return False
    return bool(pkg.get("version"))


def _openclaw_install_is_healthy(install_dir: Path) -> bool:
    """True iff ``install_dir`` looks like a COMPLETE, resolvable openclaw
    install: a valid ``package.json`` manifest AND both runtime entrypoints —
    ``openclaw.mjs`` (the ``bin/openclaw`` symlink target) and ``dist/index.js``
    (what the gateway executes). Requiring both entrypoints rejects a truncated
    extraction that wrote only the manifest + ``openclaw.mjs``.

    Used to decide a ``.openclaw-XXX`` sibling is complete enough to PROMOTE
    into the live slot (and to re-verify after the promote). Distinct from
    ``_openclaw_install_has_manifest``, which gates the destructive direction:
    a genuine stale residue (live install healthy → the sibling is junk to
    delete) vs. an inverted / half-finished atomic swap (live is a husk with no
    manifest while a complete ``.openclaw-XXX`` sibling holds the only complete
    install → that sibling is the *recovery source*). See
    ``_check_and_clean_stale_npm_temp_dirs``.
    """
    if not _openclaw_install_has_manifest(install_dir):
        return False
    return (install_dir / "openclaw.mjs").exists() and (
        install_dir / "dist" / "index.js"
    ).exists()


def _staged_install_version(install_dir: Path) -> str:
    """Best-effort version string from a (possibly staged) install dir's
    ``package.json``; ``"?"`` if unreadable. For operator-facing messages."""
    try:
        return json.loads((install_dir / "package.json").read_text()).get("version", "?")
    except (OSError, ValueError):
        return "?"


def _format_npm_install_error_hint(stderr: str) -> str:
    """Classify an `npm install -g openclaw` failure and return the hint
    block to print after the raw stderr.

    The stale-temp-dir case (ENOTEMPTY on a `.openclaw-XXX` rename target)
    is the one we know how to remediate concretely; everything else falls
    back to the generic registry/disk/sudoers hint.
    """
    if "ENOTEMPTY" in stderr and ".openclaw-" in stderr:
        # Prefer the absolute path from npm's `dest` line so the
        # suggested `rm -rf` is copy-pasteable. Fall back to a wildcard
        # under the known node_modules dir if the path didn't parse.
        m = re.search(r"(/\S+/\.openclaw-[A-Za-z0-9_-]+)", stderr)
        path_str = m.group(1) if m else str(_npm_node_modules_dir() / ".openclaw-*")
        return (
            f"\n  [yellow]→[/] Found stale npm temp dir at {path_str}.\n"
            f"     This is residue from a prior failed `npm install -g openclaw` — npm renames\n"
            f"     the live dir to a `.openclaw-XXX` sibling during its atomic swap, and if the\n"
            f"     install bails before completion the sibling is left behind, blocking the next\n"
            f"     install's rename step with ENOTEMPTY. Remove it and retry:\n"
            f"       sudo rm -rf {path_str}\n"
            f"       sudo evolve-admin menu upgrade"
        )
    return (
        "\n  [yellow]→[/] Common causes: registry unreachable (check network), "
        "disk full (`df -h /opt/homebrew`), or missing sudoers grant for npm."
    )


def _recover_from_staged_install(live: Path, complete: list[Path]) -> bool:
    """Recover a half-finished npm atomic swap by promoting a complete
    ``.openclaw-XXX`` staging dir into the live ``openclaw`` slot.

    Reached only when the live ``openclaw`` install is already a broken husk
    (``_openclaw_install_is_healthy`` False) while a ``.openclaw-XXX`` sibling
    holds a complete install — npm extracted the new tree into the temp name
    and died before the final rename into place (the dangling-``bin``-symlink
    outage seen on the mini, 2026-06-30). Because the live install is already
    non-working, promoting can only improve a broken state. Privileged +
    confirm-gated, mirroring the delete path. Returns True if the install was
    recovered (upgrade may proceed), False to abort.
    """
    console.print(
        f"\n  [yellow]⚠️  The live openclaw install at {live} is missing or "
        "broken[/] — no valid package.json manifest."
    )
    if len(complete) > 1:
        console.print(
            "  [red]❌ Found multiple complete `.openclaw-XXX` staging dirs; "
            "refusing to guess which is the good one:[/]"
        )
        for p in complete:
            console.print(f"    - {p.name}  (openclaw {_staged_install_version(p)})")
        console.print(
            "     Inspect them, then promote the right one manually:\n"
            f"       sudo /bin/rm -rf {live}\n"
            f"       sudo /bin/mv {complete[0].parent}/<chosen> {live}\n"
            "     then re-run the upgrade."
        )
        return False
    staged = complete[0]
    ver = _staged_install_version(staged)
    console.print(
        f"  A complete openclaw {ver} install is staged at {staged.name} — "
        "npm's atomic swap died before renaming it into place. It is the only\n"
        "  complete copy on disk; deleting it (the usual stale-residue path) would\n"
        "  destroy the only resolvable install."
    )
    if not click.confirm(
        f"  Promote it into {live.name} to repair the install?", default=True
    ):
        console.print(
            "  [red]❌ Left as-is.[/] Promote manually with:\n"
            f"       sudo /bin/rm -rf {live}\n"
            f"       sudo /bin/mv {staged} {live}\n"
            "     then re-run the upgrade."
        )
        return False
    # Warm up the sudo timestamp with a VISIBLE prompt first: the rm/mv below
    # capture output, which would hide sudo's password prompt and hang silently
    # if the credential cache expired during the confirm pause above.
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        console.print("  [red]❌ Could not validate sudo credentials; left as-is.[/]")
        return False
    # Remove the broken husk, then rename the staged tree into place. Uses the
    # operator's interactive sudo (ocadmin is the pod-admin CLI, not the evolve
    # daemon) — no new sudoers grant beyond the `/bin/rm`/`/bin/mv` it already
    # relies on for the delete path.
    r = subprocess.run(
        ["sudo", "/bin/rm", "-rf", str(live)], capture_output=True, text=True,
    )
    if r.returncode != 0:
        console.print(
            f"  [red]❌ Failed to remove broken install {live}: "
            f"{r.stderr.strip() or r.stdout.strip()}[/]"
        )
        return False
    r = subprocess.run(
        ["sudo", "/bin/mv", str(staged), str(live)], capture_output=True, text=True,
    )
    if r.returncode != 0:
        console.print(
            f"  [red]❌ Failed to promote {staged.name}: "
            f"{r.stderr.strip() or r.stdout.strip()}[/]"
        )
        return False
    if not _openclaw_install_is_healthy(live):
        console.print(
            "  [red]❌ Promoted install still doesn't look resolvable — "
            "inspect manually.[/]"
        )
        return False
    console.print(
        f"  [green]✅ Promoted {staged.name} → {live.name} (openclaw {ver}); "
        "install resolvable again.[/]"
    )
    return True


def _check_and_clean_stale_npm_temp_dirs() -> bool:
    """Preflight: detect stale `.openclaw-*` temp dirs and offer to remove
    them before invoking `npm install -g`.

    Returns True if the upgrade should proceed (no stale dirs, or the
    operator confirmed cleanup and it succeeded). Returns False if the
    operator declined or the cleanup failed — caller should abort.
    """
    stale = _find_stale_npm_temp_dirs()
    if not stale:
        return True
    # Inverted / half-finished atomic-swap guard: if the live `openclaw`
    # install has no valid manifest (an unambiguous husk) while a COMPLETE
    # `.openclaw-XXX` sibling exists, that sibling is the recovery source, not
    # residue — blindly deleting it would destroy the only resolvable install.
    # Gated on the manifest (not full health) so an upstream entrypoint-layout
    # change can never make us delete a real, manifest-bearing install.
    live = _npm_node_modules_dir() / "openclaw"
    if not _openclaw_install_has_manifest(live):
        # Only real directories are promotable — a `.openclaw-*` file or symlink
        # is never a recovery source (a symlink would `mv` the link, not a tree).
        complete = [
            p for p in stale
            if p.is_dir() and not p.is_symlink() and _openclaw_install_is_healthy(p)
        ]
        if complete:
            if not _recover_from_staged_install(live, complete):
                return False
            # Promote succeeded → the promoted dir is now `live`. Any OTHER
            # `.openclaw-*` still on disk are genuine residue that would block
            # the upcoming `npm install` with ENOTEMPTY; fall through to the
            # normal confirm+delete flow to clear them.
            stale = _find_stale_npm_temp_dirs()
            if not stale:
                return True
    console.print(
        f"\n  [yellow]⚠️  Found {len(stale)} stale npm temp dir(s) "
        f"in {_npm_node_modules_dir()}:[/]"
    )
    for p in stale:
        age = _dir_age_days(p)
        age_str = f"{age}d old" if age is not None else "age unknown"
        console.print(f"    - {p.name}  ({age_str})")
    console.print(
        "     These are residue from a prior failed `npm install -g` and will block\n"
        "     this upgrade's rename step with `ENOTEMPTY: directory not empty`."
    )
    if not click.confirm("  Remove them now?", default=True):
        paths = " ".join(str(p) for p in stale)
        console.print(
            f"  [red]❌ Refusing to upgrade with stale temp dirs in place.[/]\n"
            f"     Remove manually with:\n"
            f"       sudo rm -rf {paths}\n"
            f"     then re-run the upgrade."
        )
        return False
    for p in stale:
        r = subprocess.run(
            ["sudo", "/bin/rm", "-rf", str(p)], capture_output=True, text=True,
        )
        if r.returncode != 0:
            console.print(
                f"  [red]❌ Failed to remove {p}: {r.stderr.strip() or r.stdout.strip()}[/]"
            )
            return False
        console.print(f"  [green]✅ Removed {p.name}[/]")
    return True


# ── oc upgrade ───────────────────────────────────────────────────────────────

@menu_group.command("upgrade")
@click.option("--force", is_flag=True, default=False, help="Reinstall even if already on latest")
@click.option("--no-restart", is_flag=True, default=False, help="Skip gateway restarts after upgrade")
@click.option("--neutralize-externalized", is_flag=True, default=False,
              help="Workaround openclaw#82301: snapshot bot configs, strip refs to "
                   "missing externalized plugins (brave/slack/discord/etc.), upgrade, "
                   "install plugins, restore configs. Use when the preflight reports "
                   "externalized-plugin blockers and you need to upgrade anyway.")
@click.option("--dry-run", is_flag=True, default=False,
              help="With --neutralize-externalized, print the planned dance and exit "
                   "without mutating anything.")
@click.pass_context
def oc_upgrade(
    ctx: click.Context, force: bool, no_restart: bool,
    neutralize_externalized: bool, dry_run: bool,
) -> None:
    """Upgrade OpenClaw npm package and remove conflicting user LaunchAgents.

    After upgrading, openclaw's post-install script installs a generic
    ai.openclaw.gateway LaunchAgent into each bot's ~/Library/LaunchAgents/.
    This command detects and removes those agents to prevent duplicate gateway
    processes competing with the existing system daemons.

    For releases that externalize previously-bundled plugins (2026.5.12
    moved slack/brave/discord/etc. out of the core tarball), pass
    --neutralize-externalized to auto-run the snapshot/strip/upgrade/
    install/restore dance documented in openclaw/openclaw#82301.
    """
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)

    # The sequence itself lives in oc_upgrade_apply (spec-oc-upgrade-from-ui-
    # 2026-07-28 §5) so the admin UI's "Run upgrade now" button and the root
    # helper can run it headless. This command is now purely its Rich
    # renderer: every event carries the exact markup string this function used
    # to `console.print`, with the same `end=`, so the terminal output is
    # byte-identical to the pre-extraction command (pinned by
    # tests/test_oc_upgrade_cli_golden.py).
    from .oc_upgrade_apply import run_upgrade

    # Bind the REAL console up front: run_upgrade swaps this module's
    # `console` global for its event shim while it runs (so the helpers that
    # still print — the dance phases, the LaunchAgent sweep, the verifiers —
    # route through the same event stream). A late `console.print` lookup here
    # would resolve to that shim and recurse forever.
    _term = console
    outcome = run_upgrade(
        network=network,
        emit=lambda ev: _term.print(ev.message, end=ev.end),
        confirm=lambda prompt, default: click.confirm(prompt, default=default),
        force=force,
        no_restart=no_restart,
        neutralize_externalized=neutralize_externalized,
        dry_run=dry_run,
    )
    if outcome.exit_code:
        sys.exit(outcome.exit_code)


# ── Post-upgrade send-surface verification ───────────────────────────────────
#
# OpenClaw 2026.6.1 removed the gateway's POST /api/message with no
# release-note entry; every scheduled user-facing send pod-wide died
# silently for 8 days (internal/decision-add-bot-m4-u1-proof-2026-06-11.md
# §"The 07:00 window"). The delivery convention is now `openclaw message
# send` (internal/spec-gallery-delivery-convention-2026-06-11.md) — upstream
# documents the command but commits to no stability contract, so every
# upgrade re-proves it here. Failure never blocks (the install already
# happened); the job is loud detection + rollback guidance, in the
# terminal AND on the Alerts page.

SEND_SURFACE_PROBE_PRODUCER = "send_surface_probe"
SEND_SURFACE_BROKEN_TYPE = "send_surface_broken"


def _verify_send_surface_post_upgrade(
    old_version: str,
    new_version: str,
    network: dict,
    shared_dir: Path | None = None,
) -> None:
    """Tri-state verification of the delivery surface on the new install.

    1. Contract probe (`message send --help` + required flags) — catches
       a removed/renamed surface (the 2026.6.1 shape).
    2. End to end, once per upgrade: one real message to the operator's
       alert channel through the same CLI surface the gallery scripts
       use. Catches what --help can't — e.g. OC 2026.6's CLI
       device-scope narrowing died at pairing, not at argument parsing.
       The message itself is the proof.

    "Couldn't verify" is reported as exactly that, never as a pass.
    """
    from . import safe_upgrade as _su

    console.print("\n  Verifying the message-send surface on the new version…")
    probe = _su.probe_send_surface()
    if probe["status"] == _su.SEND_SURFACE_FAILED:
        console.print(
            f"  [red]❌ The upgrade to {new_version} broke the message-send "
            f"surface ({probe['reason']}) — scheduled deliveries (briefings, "
            "sweeps, watchers) will fail on every bot.[/]"
        )
        console.print(
            f"       [yellow]→[/] Roll back: sudo npm install -g "
            f"--prefix={OPENCLAW_NPM_PREFIX} openclaw@{old_version}"
        )
        _emit_send_surface_broken_signal(
            old_version, new_version, probe,
            stage="contract_probe", shared_dir=shared_dir,
        )
        return
    if probe["status"] == _su.SEND_SURFACE_UNVERIFIED:
        console.print(
            f"  [yellow]⚠️  Couldn't verify the message-send surface "
            f"({probe['reason']}) — NOT confirmed working on {new_version}. "
            "Probe by hand: `openclaw message send --help`.[/]"
        )
        return
    console.print("  [green]✅ send-surface contract intact on the new version[/]")

    try:
        from .alerts import dispatcher as _dispatcher
        outcome = _dispatcher.send(
            shared_dir=shared_dir or DEFAULT_SHARED_DIR,
            network=network,
            source=SEND_SURFACE_PROBE_PRODUCER,
            message=(
                "🟢 OpenClaw update check\n"
                f"Updated to {new_version}. This message is itself the proof "
                "your bots can still send you their scheduled messages — "
                "nothing to do."
            ),
            severity=_dispatcher.Severity.INFO,
            dedup_key=None,
            # Subscription-completeness (spec-subscription-completeness-
            # 2026-06-24): the post-upgrade send-proof is a meta message
            # ABOUT the alerting/delivery surface. meta.send_probe is the
            # success-path handle (the FAILURE path is the safety-critical
            # system.send_surface_broken). Default-on so the operator keeps
            # seeing the green proof; mutable per-event without silencing
            # the failure alert.
            catalog_event="meta.send_probe",
        )
    except Exception as e:
        console.print(
            f"  [yellow]⚠️  End-to-end send check couldn't run ({e}) — "
            "contract probe passed, full path unverified.[/]"
        )
        return
    if outcome.result == _dispatcher.DispatchResult.SENT:
        console.print(
            "  [green]✅ end-to-end verified — a real message reached your "
            f"{outcome.channel} alerts channel[/]"
        )
    elif outcome.result == _dispatcher.DispatchResult.FAILED:
        console.print(
            f"  [red]❌ End-to-end send FAILED on {new_version}: "
            f"{outcome.error}[/]\n"
            "       Scheduled deliveries are likely broken pod-wide."
        )
        console.print(
            f"       [yellow]→[/] Roll back: sudo npm install -g "
            f"--prefix={OPENCLAW_NPM_PREFIX} openclaw@{old_version}"
        )
        _emit_send_surface_broken_signal(
            old_version, new_version,
            {**probe, "end_to_end_error": outcome.error},
            stage="end_to_end_send", shared_dir=shared_dir,
        )
    else:
        # no_recipient / suppressed / deferred — not a surface failure,
        # but not proof either. The contract probe's verdict stands.
        console.print(
            f"  [yellow]⚠️  End-to-end send not performed "
            f"({outcome.result.value}) — contract probe passed, full path "
            "unverified.[/]"
        )


def _emit_send_surface_broken_signal(
    old_version: str,
    new_version: str,
    probe: dict,
    *,
    stage: str,
    shared_dir: Path | None = None,
) -> None:
    """Pod-scope alert Signal: the upgrade broke message delivery.

    The console output above is seen only by whoever ran the upgrade;
    this lands the same fact on the Alerts page (and through the
    notifier, if any path to the operator still works). Same graceful-
    degradation shape as _emit_runtime_notes_review_signal.
    """
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR
    try:
        import importlib
        store = importlib.import_module("signals.store")
        schema = importlib.import_module("schema.signal")
    except Exception as e:
        console.print(
            f"  [yellow]⚠️  Could not load signal store for the send-surface "
            f"alert: {e}[/]"
        )
        return
    signature = schema.make_signature(
        SEND_SURFACE_PROBE_PRODUCER, SEND_SURFACE_BROKEN_TYPE, new_version,
    )
    try:
        store.observe(
            shared_dir,
            signature=signature,
            producer=SEND_SURFACE_PROBE_PRODUCER,
            type=SEND_SURFACE_BROKEN_TYPE,
            flavor="activity",
            severity="alert",
            scope="pod",
            category="platform",
            title="Messages from your bots may not be getting through",
            body=(
                "Messages from your bots may not be getting through after "
                f"the last OpenClaw update ({old_version} → {new_version}): "
                "the check that runs right after an update found the sending "
                "path broken. Scheduled messages (briefings, sweeps, "
                "watchers) will fail on every bot until this is fixed.\n"
                f"Roll back: sudo npm install -g --prefix={OPENCLAW_NPM_PREFIX} "
                f"openclaw@{old_version}"
            ),
            details={
                "old_version": old_version,
                "new_version": new_version,
                "stage": stage,
                "probe": probe,
            },
        )
    except Exception as e:
        console.print(
            f"  [yellow]⚠️  Could not emit the send-surface alert Signal: {e}[/]"
        )


# ── Post-upgrade OC-data-dependency verification ─────────────────────────────
#
# OpenClaw migrated bots' auth-profiles.json into SQLite on 2026-06-22 and
# Evolve read the absent JSON → the app scanner failed pod-wide with
# error_kind=missing_api_key. The send-surface re-probe above only covers
# delivery; it would NOT have caught a silent data-file migration. The
# safe-upgrade preflight's oc_data_dependencies gate runs the same readers
# BEFORE an upgrade — but it probes the currently-installed OC. This hook is
# the ALWAYS-ON post-install half: it re-exercises the real Evolve→OpenClaw
# readers against the JUST-UPGRADED OC, so an upgrade cannot complete without
# proving the readers still resolve. A load-bearing dependency that flips to
# broken here is loud, attributable evidence the upgrade did it.

OC_DEPS_PROBE_PRODUCER = "oc_deps_probe"
OC_DEPS_BROKEN_TYPE = "oc_dependency_broken"


def _verify_oc_dependencies_post_upgrade(
    old_version: str,
    new_version: str,
    network: dict,
    shared_dir: Path | None = None,
) -> None:
    """Re-run the OC-data-dependency probes against the new install.

    Iterates ``oc_deps.OC_DEPENDENCIES`` (per-bot probes across every bot,
    pod-scope probes once) and reports broken readers. A load-bearing
    dependency that resolves nothing is the auth-migration shape: print
    rollback guidance and land an Alerts-page Signal. Non-load-bearing or
    indeterminate results are reported but never escalated. Best-effort —
    the caller wraps this so verification never wedges the upgrade tail.
    """
    from . import oc_deps

    console.print("\n  Verifying Evolve→OpenClaw data dependencies on the new version…")
    blocking_broken: list[dict] = []
    advisory: list[dict] = []
    for dep in oc_deps.OC_DEPENDENCIES:
        targets = _bot_ids(network) if dep.scope == oc_deps.SCOPE_PER_BOT else [None]
        for bot_id in targets:
            try:
                pr = dep.probe(bot_id, network=network)
            except Exception as exc:  # noqa: BLE001 — a probe must never wedge the upgrade
                pr = oc_deps.ProbeResult(oc_deps.PROBE_INDETERMINATE, f"probe raised: {exc}")
            if pr.status in (oc_deps.PROBE_MISSING, oc_deps.PROBE_EMPTY):
                rec = {"key": dep.key, "bot_id": bot_id, "detail": pr.detail}
                (blocking_broken if dep.load_bearing else advisory).append(rec)
            elif pr.status == oc_deps.PROBE_INDETERMINATE:
                advisory.append({"key": dep.key, "bot_id": bot_id, "detail": pr.detail})

    if not blocking_broken and not advisory:
        console.print("  [green]✅ all Evolve→OpenClaw data readers resolve on the new version[/]")
        return

    for rec in advisory:
        where = f" ({rec['bot_id']})" if rec["bot_id"] else ""
        console.print(f"  [yellow]⚠️  {rec['key']}{where}: {rec['detail']}[/]")

    if blocking_broken:
        for rec in blocking_broken:
            where = f" ({rec['bot_id']})" if rec["bot_id"] else ""
            console.print(
                f"  [red]❌ The upgrade to {new_version} broke a load-bearing Evolve "
                f"reader — {rec['key']}{where}: {rec['detail']}[/]"
            )
        console.print(
            f"       [yellow]→[/] Roll back: sudo npm install -g "
            f"--prefix={OPENCLAW_NPM_PREFIX} openclaw@{old_version}"
        )
        _emit_oc_dependency_broken_signal(
            old_version, new_version, blocking_broken, shared_dir=shared_dir,
        )


def _emit_oc_dependency_broken_signal(
    old_version: str,
    new_version: str,
    broken: list[dict],
    *,
    shared_dir: Path | None = None,
) -> None:
    """Pod-scope alert Signal: the upgrade broke an out-of-band OC data reader.

    Same graceful-degradation shape as ``_emit_send_surface_broken_signal``.
    """
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR
    try:
        import importlib
        store = importlib.import_module("signals.store")
        schema = importlib.import_module("schema.signal")
    except Exception as e:
        console.print(
            f"  [yellow]⚠️  Could not load signal store for the OC-dependency alert: {e}[/]"
        )
        return
    keys = ",".join(sorted({rec["key"] for rec in broken}))
    signature = schema.make_signature(
        OC_DEPS_PROBE_PRODUCER, OC_DEPS_BROKEN_TYPE, f"{new_version}:{keys}",
    )
    try:
        store.observe(
            shared_dir,
            signature=signature,
            producer=OC_DEPS_PROBE_PRODUCER,
            type=OC_DEPS_BROKEN_TYPE,
            flavor="activity",
            severity="alert",
            scope="pod",
            category="platform",
            title="Evolve can't read OpenClaw data after the last update",
            body=(
                "After the last OpenClaw update "
                f"({old_version} → {new_version}) the check that runs right "
                "after an update found Evolve can no longer read a data file "
                f"it depends on ({keys}). This is the shape of the 2026-06-22 "
                "incident, where an auth-store migration left the app scanner "
                "failing pod-wide.\n"
                f"Roll back: sudo npm install -g --prefix={OPENCLAW_NPM_PREFIX} "
                f"openclaw@{old_version}"
            ),
            details={
                "old_version": old_version,
                "new_version": new_version,
                "broken": broken,
            },
        )
    except Exception as e:
        console.print(
            f"  [yellow]⚠️  Could not emit the OC-dependency alert Signal: {e}[/]"
        )


# ── RUNTIME_NOTES.md review reminder ─────────────────────────────────────────

RUNTIME_NOTES_REMINDER_PRODUCER = "oc_upgrade_runtime_notes_reminder"
RUNTIME_NOTES_REMINDER_TYPE = "runtime_notes_review_due"


def _emit_runtime_notes_review_signal(
    old_version: str,
    new_version: str,
    shared_dir: Path | None = None,
) -> None:
    """Fire a Signal nudging the operator to walk docs/system/RUNTIME_NOTES.md
    after an OC version change. Entries in that file are OC-version-tied
    and may be stale once upstream OC ships a fix. Operator-acked, no
    auto-resolve. No-op when old == new.
    """
    if old_version == new_version:
        return
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR

    try:
        import importlib
        store = importlib.import_module("signals.store")
        schema = importlib.import_module("schema.signal")
    except Exception as e:
        console.print(
            f"  [yellow]⚠️  Could not load signal store for RUNTIME_NOTES "
            f"reminder: {e}[/]"
        )
        return

    signature = schema.make_signature(
        RUNTIME_NOTES_REMINDER_PRODUCER,
        RUNTIME_NOTES_REMINDER_TYPE,
        new_version,
    )
    body = (
        f"OpenClaw upgraded from `{old_version}` to `{new_version}`.\n\n"
        f"Walk `docs/system/RUNTIME_NOTES.md` and delete any entry whose "
        f"underlying upstream constraint has been fixed in `{new_version}`. "
        f"Stale entries waste session context — RUNTIME_NOTES injects into "
        f"every bot session.\n\n"
        f"OC release notes: "
        f"https://github.com/openclaw/openclaw/releases/tag/v{new_version}"
    )
    try:
        store.observe(
            shared_dir,
            signature=signature,
            producer=RUNTIME_NOTES_REMINDER_PRODUCER,
            type=RUNTIME_NOTES_REMINDER_TYPE,
            flavor="maintenance",
            severity="info",
            scope="pod",
            title=f"Review RUNTIME_NOTES.md after OC upgrade to {new_version}",
            body=body,
            details={
                "old_version": old_version,
                "new_version": new_version,
                "runtime_notes_path": "docs/system/RUNTIME_NOTES.md",
            },
        )
    except Exception as e:
        console.print(
            f"  [yellow]⚠️  Could not emit RUNTIME_NOTES review reminder: {e}[/]"
        )


# ── Neutralize-externalized dance helpers ───────────────────────────────────

def _compute_neutralize_plan(report) -> dict[str, list[str]]:
    """Distill the preflight report into a per-bot list of plugin ids to
    neutralize. Returns an empty dict when there's no config_references
    blocker or no externalized plugins among the missing.
    """
    if report is None or not hasattr(report, "gates"):
        return {}
    from . import safe_upgrade as _su
    gate = report.gates.get("config_references")
    details = (gate.details if gate else None) or {}
    missing_by_bot = details.get("missing_by_bot") or {}

    plan: dict[str, list[str]] = {}
    for bot_id, missing in missing_by_bot.items():
        externalized = [
            p for p in missing if p in _su._KNOWN_EXTERNALIZED_PLUGINS
        ]
        if externalized:
            plan[bot_id] = sorted(externalized)
    return plan


def _compute_phantom_cleanup_plan(report) -> dict[str, list[str]]:
    """Distill the preflight's per-bot `phantom_installs` into a cleanup
    plan: `{bot_id: [plugin_id, ...]}`. Used by the dance to uninstall
    phantom install records (e.g. TS-source-only @openclaw/brave-plugin@
    2026.5.1-beta.1 from a previous failed upgrade attempt) before
    running a fresh install. Independent of the neutralize plan — a bot
    may have phantoms without having any missing plugins, or vice versa.
    """
    if report is None or not hasattr(report, "gates"):
        return {}
    gate = report.gates.get("config_references")
    details = (gate.details if gate else None) or {}
    bots = details.get("bots") or []
    cleanup: dict[str, list[str]] = {}
    for entry in bots:
        phantoms = entry.get("phantom_installs") or []
        if phantoms:
            cleanup[entry["bot_id"]] = sorted(phantoms)
    return cleanup


def _print_dance_preview(
    plan: dict[str, list[str]],
    cleanup: dict[str, list[str]],
    network: dict,
) -> None:
    from . import safe_upgrade as _su
    if cleanup:
        console.print(
            f"\n  [bold]Phantom install cleanup[/] ({len(cleanup)} bot(s)):"
        )
        for bot_id in sorted(cleanup.keys()):
            user = _gateway_runtime_user(bot_id, network)
            console.print(
                f"    • {bot_id} ({user}): uninstall stale records {cleanup[bot_id]} "
                "(install path lacks a loadable entry — likely TS-source-only "
                "from a previous failed upgrade attempt)"
            )

    console.print(
        "\n  [bold]--neutralize-externalized plan[/] "
        f"({len(plan)} bot(s)):"
    )
    for bot_id in sorted(plan.keys()):
        user = _gateway_runtime_user(bot_id, network)
        pkgs = ", ".join(
            _su._KNOWN_EXTERNALIZED_PLUGINS[p] for p in plan[bot_id]
        )
        console.print(
            f"    • {bot_id} ({user}): neutralize {plan[bot_id]} → install {pkgs}"
        )
    console.print(
        "    The dance will: clean up phantoms → snapshot each bot's "
        "openclaw.json → strip refs → upgrade openclaw → install externalized "
        "plugins → restore configs → restart."
    )


def _run_neutralize_phase(plan: dict[str, list[str]], network: dict) -> bool:
    """Snapshot + neutralize each bot's openclaw.json, then restart the
    affected gateways so the neutralized config takes effect on the OLD
    runtime. Returns True if every bot succeeded.
    """
    from . import oc_neutralize as _neut
    console.print("\n  Dance phase 1: snapshot + neutralize bot configs…")
    for bot_id, plugin_ids in plan.items():
        user = _gateway_runtime_user(bot_id, network)
        result = _neut.snapshot_and_neutralize_bot(bot_id, user, set(plugin_ids))
        if result.ok:
            console.print(
                f"    [green]✅ {bot_id} ({user}):[/] snapshot → {result.backup_path.name}, "
                f"neutralized {result.plugin_ids}"
            )
        else:
            console.print(f"    [red]❌ {bot_id} ({user}): {result.error}[/]")
            return False

    console.print("  Restarting affected gateways to load neutralized config…")
    for bot_id in plan:
        _restart_gateway(bot_id)
    return True


def _run_phantom_cleanup_phase(
    cleanup: dict[str, list[str]], network: dict,
) -> None:
    """Uninstall phantom install records before phase 2 runs `plugins
    install`. A phantom (install record exists but installPath has no
    loadable entry) silently survives `install --force` in some cases,
    so we explicitly remove it first.
    """
    from . import oc_neutralize as _neut
    total = sum(len(ps) for ps in cleanup.values())
    console.print(
        f"\n  Dance phase 0: uninstall {total} phantom install record(s)…"
    )
    for bot_id in cleanup:
        user = _gateway_runtime_user(bot_id, network)
        for plugin_id in cleanup[bot_id]:
            ok, err = _neut.uninstall_plugin(user, plugin_id)
            if ok:
                console.print(f"    [green]✅ {bot_id} ({user}):[/] uninstalled {plugin_id}")
            else:
                # Don't abort — uninstall is best-effort cleanup. The
                # subsequent install --force may still succeed.
                console.print(f"    [yellow]⚠️  {bot_id} ({user}):[/] {plugin_id} — {err}")


def _run_install_phase(plan: dict[str, list[str]], network: dict) -> None:
    """For each (bot, externalized plugin) pair, run `openclaw plugins
    install <pkg>`. Logs failures but doesn't abort — phase 3 (restore)
    still runs so bots aren't left with neutralized configs."""
    from . import oc_neutralize as _neut
    from . import safe_upgrade as _su
    total = sum(len(ps) for ps in plan.values())
    console.print(f"\n  Dance phase 2: install {total} externalized plugin(s)…")
    for bot_id in plan:
        user = _gateway_runtime_user(bot_id, network)
        for plugin_id in plan[bot_id]:
            pkg = _su._KNOWN_EXTERNALIZED_PLUGINS[plugin_id]
            ok, err = _neut.install_externalized_plugin(user, pkg)
            if ok:
                console.print(f"    [green]✅ {bot_id} ({user}):[/] {pkg}")
            else:
                console.print(f"    [red]❌ {bot_id} ({user}):[/] {pkg} — {err}")


def _run_restore_phase(plan: dict[str, list[str]], network: dict) -> None:
    """Copy each bot's openclaw.json.preupgrade back over openclaw.json.
    Logs failures — operator can manually `sudo cp` if needed."""
    from . import oc_neutralize as _neut
    console.print("\n  Dance phase 3: restore openclaw.json from .preupgrade snapshots…")
    for bot_id in plan:
        user = _gateway_runtime_user(bot_id, network)
        ok, err = _neut.restore_bot_config(user)
        if ok:
            console.print(f"    [green]✅ {bot_id} ({user}):[/] restored")
        else:
            console.print(
                f"    [red]❌ {bot_id} ({user}):[/] {err} — fix manually: "
                f"`sudo cp /Users/{user}/.openclaw/openclaw.json.preupgrade "
                f"/Users/{user}/.openclaw/openclaw.json`"
            )


def _restart_gateway(bot_id: str) -> None:
    svc = _bot_service(bot_id)
    ok, out = get_scheduler().restart(svc)
    if ok:
        console.print(f"  [green]✅ {svc} restarted[/]")
    else:
        console.print(f"  [yellow]⚠️  {bot_id}: {out.strip() or 'unknown error'}[/]")


# ── oc safe-upgrade ─────────────────────────────────────────────────────────

@menu_group.command("safe-upgrade")
@click.option("--target", default=None, help="Pin a specific target version (e.g. 2026.4.15).")
@click.option("--latest", "use_latest", is_flag=True, default=False,
              help="Use the npm 'latest' tag (default if --target is not set).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the raw report JSON to stdout instead of rendering.")
@click.option("--report-id", default=None,
              help="Show an existing report instead of running a new check.")
@click.pass_context
def oc_safe_upgrade(
    ctx: click.Context,
    target: str | None,
    use_latest: bool,
    as_json: bool,
    report_id: str | None,
) -> None:
    """Read-only preflight: would upgrading openclaw right now break Evolve?

    Runs the gates from internal/spec-safe-upgrade-2026-05-02.md against
    the current pod state and the candidate target. Persists the report
    under /Users/Shared/evolve/safe-upgrade/reports/.

    Exit codes: 0 = safe, 1 = blockers must be resolved, 2 = error.
    """
    from . import safe_upgrade as _su

    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)

    if report_id:
        data = _su.load_report(report_id)
        if data is None:
            console.print(f"[red]❌ No report with id {report_id}[/]")
            sys.exit(2)
    else:
        target_spec = target or "latest"
        if not as_json:
            console.print(f"\n[bold]── Safe-upgrade preflight ─────────────────────────────[/]")
            console.print(f"  Candidate: openclaw@{target_spec}")
            console.print("  Running safe-upgrade gates against current pod state…\n")
        try:
            report = _su.run_preflight(target_spec, network=network)
        except Exception as e:
            console.print(f"[red]❌ Preflight failed: {e}[/]")
            sys.exit(2)
        data = report.to_json()

    if as_json:
        click.echo(json.dumps(data, indent=2))
    else:
        _render_safety_report(data)

    sys.exit(0 if data.get("ok") else 1)


def _render_safety_report(report: dict) -> None:
    """Render a safety report (already-loaded dict) to the console."""
    rid       = report.get("report_id", "?")
    checked   = report.get("checked_at", "?")
    candidate = report.get("candidate", {}) or {}
    current   = report.get("current", {}) or {}
    gates     = report.get("gates", {}) or {}
    reqs      = report.get("requirements", []) or []
    ok        = bool(report.get("ok"))

    inst = current.get("installed_version") or "?"
    target_spec = candidate.get("target_spec", "?")
    resolved = candidate.get("resolved_version") or "?"
    arrow = f"openclaw {inst} → {resolved} (target: {target_spec})"

    console.print(f"\n[bold]── Upgrade Safety Report ──────────────────────────────[/]")
    console.print(f"  Checked {checked} · {arrow}")
    console.print()
    if ok:
        console.print("  Overall:  [green]✅ SAFE TO UPGRADE[/]")
    else:
        n = sum(1 for r in reqs if r.get("blocking"))
        console.print(f"  Overall:  [red]❌ NOT SAFE — {n} blocker{'s' if n != 1 else ''} must be resolved first[/]")

    console.print("\n  Gates:")
    label_for = {
        "node_version": "node-version",
        "stub_install": "stub-install",
        "config_references": "config-references",
        "user_launchagents": "user-launchagents",
        "plist_paths": "plist-paths",
        "port_owners": "port-owners",
        "send_surface": "send-surface",
    }
    for key in ["node_version", "stub_install", "config_references",
                "user_launchagents", "plist_paths", "port_owners",
                "send_surface"]:
        gate = gates.get(key) or {}
        gok  = bool(gate.get("ok"))
        icon = "[green]✅[/]" if gok else "[red]❌[/]"
        d    = gate.get("details") or {}
        console.print(f"    {icon} {label_for[key]:20s} {_summarize_gate(key, gate)}")
        if not gok and d.get("error"):
            console.print(f"       [red]error:[/] {d['error']}")

    if reqs:
        console.print("\n  Required before upgrade:")
        for i, r in enumerate(reqs, 1):
            console.print(f"    {i}. {r.get('summary', '?')}")
            rem = r.get("remediation")
            if rem:
                console.print(f"       [yellow]→[/] {rem}")
    else:
        console.print("\n  No remediation required.")

    console.print(f"\n  Report id: {rid}")


def _summarize_gate(key: str, gate: dict) -> str:
    d = gate.get("details") or {}
    if key == "node_version":
        cur = d.get("current") or "?"
        req = d.get("required") or "?"
        return f"Node {cur} vs engines.node {req!r}"
    if key == "stub_install":
        ver = d.get("resolved_version") or "?"
        sz  = d.get("size_kb")
        bp  = d.get("bin_present")
        return f"resolved={ver}, size={sz} KB, bin_present={bp}"
    if key == "config_references":
        if d.get("error"):
            return f"could not inspect candidate: {d['error']}"
        missing = d.get("missing_by_bot") or {}
        n_plugins = len(d.get("candidate_plugins") or [])
        if not missing:
            n_bots = len(d.get("bots") or [])
            return f"candidate ships {n_plugins} plugins; {n_bots} bot config(s) clean"
        affected = sorted(missing.keys())
        all_missing = sorted({m for ms in missing.values() for m in ms})
        return f"missing in candidate: {all_missing} (affects: {', '.join(affected)})"
    if key == "user_launchagents":
        scanned = d.get("scanned_users") or []
        found = d.get("found_agents") or []
        if not found:
            return f"no orphan agents across {len(scanned)} bot user(s)"
        bot_list = sorted({f['bot_id'] for f in found})
        return f"orphan agents in: {', '.join(bot_list)}"
    if key == "plist_paths":
        stale = d.get("stale_plists") or []
        if not stale:
            return f"all gateway plists resolve"
        return f"stale: {', '.join(stale)}"
    if key == "send_surface":
        status = d.get("status")
        if status == "ok":
            return "`message send` contract verified on the installed CLI"
        if status == "failed":
            extra = ""
            if d.get("missing_flags"):
                extra = f" (missing: {', '.join(d['missing_flags'])})"
            return f"broken: {d.get('reason')}{extra}"
        return f"couldn't verify: {d.get('reason')}"
    if key == "port_owners":
        ports = d.get("ports") or []
        bad = [p for p in ports if not p.get("ok")]
        if not bad:
            return f"all {len(ports)} gateway port(s) owned correctly"
        not_running = sum(1 for p in bad if p.get("error") == "no listener on port")
        misowned = sum(1 for p in bad if (p.get("error") or "").startswith("listener user "))
        parts: list[str] = []
        if not_running:
            parts.append(f"{not_running} not running")
        if misowned:
            parts.append(f"{misowned} misowned")
        other = len(bad) - not_running - misowned
        if other:
            parts.append(f"{other} probe error")
        return f"{', '.join(parts)} of {len(ports)} gateway port(s)"
    return ""


# ── oc info ──────────────────────────────────────────────────────────────────

@menu_group.command("info")
@click.argument("bot_id")
@click.pass_context
def oc_info(ctx: click.Context, bot_id: str) -> None:
    """Show config, auth profile status, and gateway process for a bot."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)
    user = _gateway_runtime_user(bot_id, network)

    console.print(f"\n[bold]── {bot_id.upper()} ({user}) ────────────────────────────[/]")

    # Model config
    oc_json = _openclaw_json(user)
    if oc_json.exists():
        try:
            cfg = _load_json_permissive(oc_json)
            mc = cfg.get("agents", {}).get("defaults", {}).get("model", {})
            console.print(f"  Primary:    {mc.get('primary', '(none)')}")
            for i, fb in enumerate(mc.get("fallbacks", []), 1):
                console.print(f"  Fallback {i}: {fb}")
            port = cfg.get("gateway", {}).get("port", "?")
            console.print(f"  Port:       {port}")
        except Exception as e:
            console.print(f"  [yellow]⚠️  Could not read {oc_json}: {e}[/]")
    else:
        console.print(f"  [yellow]⚠️  {oc_json} not found[/]")

    # Auth profiles
    auth_json = _auth_json(user)
    if auth_json.exists():
        try:
            auth = _load_json_permissive(auth_json)
            now_ms = int(datetime.now().timestamp() * 1000)
            last_good = auth.get("lastGood", {})
            stats = auth.get("usageStats", {})
            console.print()
            for pname, pdata in auth.get("profiles", {}).items():
                pstats   = stats.get(pname, {})
                last_used = pstats.get("lastUsed", 0)
                cooldown  = pstats.get("cooldownUntil", 0)
                errors    = pstats.get("errorCount", 0)
                is_good   = any(v == pname for v in last_good.values())
                is_cool   = cooldown > now_ms
                cool_secs = max(0, (cooldown - now_ms) // 1000)
                atype     = pdata.get("type", pdata.get("mode", "?"))
                type_label = {"token": "MAX/token", "api_key": "API key"}.get(atype, atype)
                provider  = pdata.get("provider", "?")
                lu_str    = datetime.fromtimestamp(last_used / 1000).strftime("%H:%M") if last_used else "never"
                if is_cool:
                    status = f"[red]rate limited ({cool_secs // 60}m {cool_secs % 60}s)[/]"
                elif errors > 0:
                    status = f"[yellow]⚠️  {errors} error(s)[/]"
                elif is_good:
                    status = "[green]✅ ok[/]"
                else:
                    status = ""
                console.print(f"    {pname:20s} [{provider}/{type_label}] used:{lu_str} {status}")
        except Exception as e:
            console.print(f"  [yellow]⚠️  Could not read auth profiles: {e}[/]")

    # Gateway process
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    procs = [l for l in result.stdout.splitlines()
             if "openclaw-gateway" in l and l.split()[0] == user]
    console.print()
    if not procs:
        console.print("  Gateway: [red]❌ NOT RUNNING[/]")
    elif len(procs) == 1:
        pid = procs[0].split()[1]
        console.print(f"  Gateway: [green]✅ running (PID {pid})[/]")
    else:
        pids = ", ".join(l.split()[1] for l in procs)
        console.print(f"  Gateway: [yellow]⚠️  {len(procs)} processes (PIDs {pids})[/]")


# ── oc logs ──────────────────────────────────────────────────────────────────

@menu_group.command("logs")
@click.argument("bot_id")
@click.option("-n", "--lines", default=30, show_default=True, help="Lines to tail")
@click.option("--err", is_flag=True, default=False, help="Show error log only")
@click.pass_context
def oc_logs(ctx: click.Context, bot_id: str, lines: int, err: bool) -> None:
    """Tail gateway logs for a bot."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)
    user = _gateway_runtime_user(bot_id, network)

    logs = []
    if err:
        logs = [(_gateway_err_log(user), "gateway.err.log")]
    else:
        logs = [
            (_gateway_log(user),     "gateway.log"),
            (_gateway_err_log(user), "gateway.err.log"),
        ]

    for path, label in logs:
        console.print(f"\n[bold]── {bot_id.upper()} {label} ──[/]")
        if path.exists():
            r = subprocess.run(["tail", f"-{lines}", str(path)], capture_output=True, text=True)
            console.print(r.stdout or "  (empty)")
        else:
            console.print(f"  [yellow]not found: {path}[/]")


# ── oc processes ─────────────────────────────────────────────────────────────

@menu_group.command("processes")
@click.option("--kill", "do_kill", is_flag=True, default=False,
              help="Prompt to kill duplicate processes")
@click.pass_context
def oc_processes(ctx: click.Context, do_kill: bool) -> None:
    """Show all running gateway processes across all bots."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)

    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)

    t = Table(show_header=True, header_style="bold blue")
    t.add_column("Bot")
    t.add_column("User")
    t.add_column("PID")
    t.add_column("Status")
    t.add_column("Started")

    all_duplicates: list[tuple[str, list[str]]] = []

    for bot_id in _bot_ids(network):
        user = _gateway_runtime_user(bot_id, network)
        procs = [l for l in result.stdout.splitlines()
                 if _is_gateway_proc(l, user)]
        if not procs:
            t.add_row(bot_id, user, "—", "[red]not running[/]", "—")
        else:
            for i, line in enumerate(procs):
                parts = line.split()
                pid     = parts[1]
                started = parts[8] if len(parts) > 8 else "?"
                status  = "[green]✅[/]" if i == 0 and len(procs) == 1 else "[yellow]⚠️  duplicate[/]"
                t.add_row(bot_id if i == 0 else "", user if i == 0 else "", pid, status, started)
            if len(procs) > 1:
                all_duplicates.append((bot_id, [l.split()[1] for l in procs]))

    console.print(t)

    if do_kill and all_duplicates:
        for bot_id, pids in all_duplicates:
            console.print(f"\n[bold]{bot_id}[/] has {len(pids)} gateway processes: {', '.join(pids)}")
            console.print("  Keeping newest (highest PID), killing the rest.")
            to_kill = sorted(pids, key=int)[:-1]
            if click.confirm(f"  Kill PIDs {', '.join(to_kill)}?", default=False):
                for pid in to_kill:
                    subprocess.run(["kill", "-9", pid])
                console.print(f"  [green]✅ Killed {', '.join(to_kill)} — launchd will manage restart[/]")


# ── oc plugins ───────────────────────────────────────────────────────────────

KNOWN_PLUGINS = {"slack", "telegram", "brave", "unity", "gmail-watcher", "discord"}


@menu_group.command("plugins")
@click.argument("bot_id")
@click.pass_context
def oc_plugins(ctx: click.Context, bot_id: str) -> None:
    """Manage plugin allow/deny lists and enabled state for a bot (interactive)."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)
    user = _gateway_runtime_user(bot_id, network)
    oc_json = _openclaw_json(user)

    if not oc_json.exists():
        console.print(f"[red]❌ {oc_json} not found[/]")
        sys.exit(1)

    def load():
        return _load_json_permissive(oc_json)

    def save(cfg):
        _preserve_write(cfg, oc_json)
        console.print(f"  [green]💾 Saved: {oc_json}[/]")

    while True:
        cfg = load()
        plugins_cfg = cfg.get("plugins", {})
        entries = plugins_cfg.get("entries", {})
        allow   = plugins_cfg.get("allow", [])
        deny    = plugins_cfg.get("deny", [])

        console.print(f"\n[bold]── {bot_id.upper()} Plugins ──────────────────────────[/]")
        console.print(f"  allow: {allow if allow else '(empty — all allowed)'}")
        console.print(f"  deny:  {deny if deny else '(empty)'}")
        if entries:
            console.print("\n  Entries:")
            for pname, pdata in entries.items():
                enabled = pdata.get("enabled", "?") if isinstance(pdata, dict) else "?"
                icon = "[green]✅[/]" if enabled is True else "[red]❌[/]" if enabled is False else "?"
                console.print(f"    {icon} {pname}")

        console.print()
        console.print("  [1] Edit allow list   [2] Edit deny list   [3] Toggle entry   [q] Back")
        sub = click.prompt("  Choice", default="q").strip()

        if sub == "1":
            console.print(f"  Current allow: {allow}")
            console.print("  [yellow]⚠️  Non-empty allow list EXCLUDES all other plugins![/]")
            console.print(f"  Known: {', '.join(sorted(KNOWN_PLUGINS))}")
            raw = click.prompt("  allow (comma-separated, empty = allow all)", default="").strip()
            new_allow = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
            unknown = [x for x in new_allow if x not in KNOWN_PLUGINS]
            if unknown and not click.confirm(f"  Unrecognized plugins: {unknown}. Continue?", default=False):
                continue
            conflicts = [x for x in new_allow if x in deny]
            if conflicts:
                console.print(f"  [yellow]⚠️  Removing {conflicts} from deny list (conflict)[/]")
                deny = [x for x in deny if x not in conflicts]
                plugins_cfg["deny"] = deny
            if new_allow:
                plugins_cfg["allow"] = new_allow
            else:
                plugins_cfg.pop("allow", None)
            cfg["plugins"] = plugins_cfg
            save(cfg)

        elif sub == "2":
            console.print(f"  Current deny: {deny}")
            raw = click.prompt("  deny (comma-separated, empty = deny none)", default="").strip()
            new_deny = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
            conflicts = [x for x in new_deny if x in allow]
            if conflicts:
                console.print(f"  [yellow]⚠️  Removing {conflicts} from allow list (conflict)[/]")
                allow = [x for x in allow if x not in conflicts]
                if allow:
                    plugins_cfg["allow"] = allow
                else:
                    plugins_cfg.pop("allow", None)
            if new_deny:
                plugins_cfg["deny"] = new_deny
            else:
                plugins_cfg.pop("deny", None)
            cfg["plugins"] = plugins_cfg
            save(cfg)

        elif sub == "3":
            if not entries:
                console.print("  No entries to toggle.")
                continue
            plist = list(entries.keys())
            for i, pname in enumerate(plist, 1):
                enabled = entries[pname].get("enabled", "?") if isinstance(entries[pname], dict) else "?"
                icon = "[green]✅[/]" if enabled is True else "[red]❌[/]"
                console.print(f"    {i}. {icon} {pname}")
            num = click.prompt("  Toggle number", default="").strip()
            try:
                idx = int(num) - 1
                pname = plist[idx]
                current = entries[pname].get("enabled", True) if isinstance(entries[pname], dict) else True
                entries[pname]["enabled"] = not current
                plugins_cfg["entries"] = entries
                cfg["plugins"] = plugins_cfg
                save(cfg)
                new_status = "[green]enabled[/]" if not current else "[red]disabled[/]"
                console.print(f"  ✅ {pname} → {new_status}")
            except (ValueError, IndexError):
                console.print("  Invalid number.")

        else:
            break


# ── oc exec-approvals ────────────────────────────────────────────────────────

SECURITY_LEVELS   = ["deny", "allowlist", "full"]
ASK_MODES         = ["off", "on-miss", "always"]
ASK_FALLBACKS     = ["deny", "allowlist", "full"]

COMMON_ALLOWLIST_PATTERNS = [
    ("/opt/homebrew/bin/python3*",  "Homebrew Python 3"),
    ("/usr/bin/python3*",           "System Python 3"),
    ("/opt/homebrew/bin/node",      "Homebrew Node.js"),
    ("/opt/homebrew/bin/npx",       "Homebrew npx"),
    ("/opt/homebrew/bin/git",       "Homebrew git"),
    ("/usr/bin/git",                "System git"),
    ("/opt/homebrew/bin/bash",      "Homebrew bash"),
    ("/bin/bash",                   "System bash"),
    ("/bin/sh",                     "System sh"),
    ("/opt/homebrew/bin/zsh",       "Homebrew zsh"),
    ("/bin/zsh",                    "System zsh"),
]


def _load_exec_approvals(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"version": 1, "defaults": {}, "agents": {}}


def _save_exec_approvals(path: Path, user: str, data: dict) -> None:
    if path.exists():
        st = os.stat(path)
        uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    else:
        uid = gid = mode = None

    path.write_text(json.dumps(data, indent=2))

    if uid is not None:
        try:
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        except OSError:
            pass
    else:
        try:
            import pwd
            u = pwd.getpwnam(user)
            os.chown(path, u.pw_uid, -1)
            os.chmod(path, 0o600)
        except Exception:
            pass
    console.print(f"  [green]💾 Saved: {path}[/]")


def _show_exec_approvals(bot_id: str, path: Path) -> None:
    data = _load_exec_approvals(path)
    exists = path.exists()
    console.print(f"\n[bold]── {bot_id.upper()} Exec Approvals ──────────────────────────────[/]")
    console.print(f"  File: {path}  {'[green]✅ exists[/]' if exists else '[red]❌ not found[/]'}")

    defaults = data.get("defaults", {})
    console.print(f"\n  Defaults:")
    console.print(f"    security:        {defaults.get('security', '(not set)')}")
    console.print(f"    ask:             {defaults.get('ask', '(not set)')}")
    console.print(f"    askFallback:     {defaults.get('askFallback', '(not set)')}")
    console.print(f"    autoAllowSkills: {defaults.get('autoAllowSkills', False)}")

    main = data.get("agents", {}).get("main", {})
    if main:
        allowlist = main.get("allowlist", [])
        console.print(f"\n  Agent 'main':")
        console.print(f"    security:        {main.get('security', '(inherited)')}")
        console.print(f"    ask:             {main.get('ask', '(inherited)')}")
        console.print(f"    askFallback:     {main.get('askFallback', '(inherited)')}")
        console.print(f"    autoAllowSkills: {main.get('autoAllowSkills', False)}")
        if allowlist:
            console.print(f"    Allowlist ({len(allowlist)} entries):")
            for i, entry in enumerate(allowlist, 1):
                pattern = entry.get("pattern", entry) if isinstance(entry, dict) else entry
                comment = entry.get("comment", "") if isinstance(entry, dict) else ""
                c_str = f"  # {comment}" if comment else ""
                console.print(f"      {i:2}. {pattern}{c_str}")
        else:
            console.print("    Allowlist: (empty)")
    else:
        console.print("\n  Agent 'main': (not configured)")


@menu_group.command("exec-approvals")
@click.argument("bot_id")
@click.pass_context
def oc_exec_approvals(ctx: click.Context, bot_id: str) -> None:
    """Manage exec-approvals.json for a bot (interactive)."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)
    user = _gateway_runtime_user(bot_id, network)
    path = _exec_approvals_json(user)

    while True:
        _show_exec_approvals(bot_id, path)
        console.print()
        console.print("  [s] Set security/ask   [a] Add pattern   [r] Remove pattern")
        console.print("  [q] Add common patterns (quick)   [t] Toggle autoAllowSkills   [Enter] Back")
        sub = click.prompt("  Choice", default="").strip().lower()

        if sub == "s":
            data = _load_exec_approvals(path)
            sec = click.prompt(f"  security ({'/'.join(SECURITY_LEVELS)})").strip().lower()
            if sec not in SECURITY_LEVELS:
                console.print(f"  [red]Invalid. Choose: {SECURITY_LEVELS}[/]")
                continue
            ask = click.prompt(f"  ask ({'/'.join(ASK_MODES)})").strip().lower()
            if ask not in ASK_MODES:
                console.print("[red]  Invalid.[/]")
                continue
            fb = click.prompt(f"  askFallback ({'/'.join(ASK_FALLBACKS)})").strip().lower()
            if fb not in ASK_FALLBACKS:
                console.print("[red]  Invalid.[/]")
                continue
            for scope in [data.setdefault("defaults", {}),
                          data.setdefault("agents", {}).setdefault("main", {})]:
                scope["security"] = sec
                scope["ask"] = ask
                scope["askFallback"] = fb
            _save_exec_approvals(path, user, data)

        elif sub == "a":
            data = _load_exec_approvals(path)
            pattern = click.prompt("  Pattern (e.g. /opt/homebrew/bin/python3*)").strip()
            if not pattern:
                continue
            comment = click.prompt("  Comment (optional)", default="").strip()
            entry = {"pattern": pattern}
            if comment:
                entry["comment"] = comment
            allowlist = data.setdefault("agents", {}).setdefault("main", {}).setdefault("allowlist", [])
            existing = {e.get("pattern", e) if isinstance(e, dict) else e for e in allowlist}
            if pattern in existing:
                console.print("  Already in allowlist.")
                continue
            allowlist.append(entry)
            _save_exec_approvals(path, user, data)
            console.print(f"  [green]✅ Added: {pattern}[/]")

        elif sub == "r":
            data = _load_exec_approvals(path)
            allowlist = data.get("agents", {}).get("main", {}).get("allowlist", [])
            if not allowlist:
                console.print("  Nothing to remove.")
                continue
            for i, entry in enumerate(allowlist, 1):
                pattern = entry.get("pattern", entry) if isinstance(entry, dict) else entry
                comment = entry.get("comment", "") if isinstance(entry, dict) else ""
                console.print(f"    {i:2}. {pattern}  {'# ' + comment if comment else ''}")
            num = click.prompt("  Number to remove", default="").strip()
            try:
                idx = int(num) - 1
                removed = allowlist.pop(idx)
                p = removed.get("pattern", removed) if isinstance(removed, dict) else removed
                _save_exec_approvals(path, user, data)
                console.print(f"  [green]✅ Removed: {p}[/]")
            except (ValueError, IndexError):
                console.print("  Invalid number.")

        elif sub == "q":
            data = _load_exec_approvals(path)
            console.print("\n  Common patterns:")
            for i, (pattern, label) in enumerate(COMMON_ALLOWLIST_PATTERNS, 1):
                console.print(f"    {i:2}. {label:38s} {pattern}")
            raw = click.prompt("  Numbers to add (space-separated, or 'all')", default="").strip()
            if raw.lower() == "all":
                indices = list(range(len(COMMON_ALLOWLIST_PATTERNS)))
            else:
                try:
                    indices = [int(x) - 1 for x in raw.split()]
                except ValueError:
                    console.print("  Invalid input.")
                    continue
            allowlist = data.setdefault("agents", {}).setdefault("main", {}).setdefault("allowlist", [])
            existing = {e.get("pattern", e) if isinstance(e, dict) else e for e in allowlist}
            added = []
            for idx in indices:
                try:
                    pattern, label = COMMON_ALLOWLIST_PATTERNS[idx]
                    if pattern not in existing:
                        allowlist.append({"pattern": pattern, "comment": label})
                        existing.add(pattern)
                        added.append(label)
                except IndexError:
                    pass
            if added:
                _save_exec_approvals(path, user, data)
                console.print(f"  [green]✅ Added: {', '.join(added)}[/]")
            else:
                console.print("  Nothing new to add.")

        elif sub == "t":
            data = _load_exec_approvals(path)
            main = data.setdefault("agents", {}).setdefault("main", {})
            current = main.get("autoAllowSkills", False)
            main["autoAllowSkills"] = not current
            _save_exec_approvals(path, user, data)
            console.print(f"  [green]✅ autoAllowSkills → {not current}[/]")

        else:
            break


# ── oc calendars ─────────────────────────────────────────────────────────────

def _cal_config_path(user: str) -> Path:
    return Path(f"/Users/{user}/.openclaw/workspace/ops/config/calendars.json")


def _load_calendars(user: str) -> dict:
    path = _cal_config_path(user)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"calendars": []}


def _save_calendars(user: str, data: dict) -> None:
    path = _cal_config_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        st = os.stat(str(path))
        uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    else:
        uid = gid = mode = None
    path.write_text(json.dumps(data, indent=2))
    if uid is not None:
        try:
            os.chown(str(path), uid, gid)
            os.chmod(str(path), mode)
        except OSError:
            pass
    else:
        try:
            import pwd
            u = pwd.getpwnam(user)
            os.chown(str(path), u.pw_uid, -1)
            os.chmod(str(path), 0o600)
        except Exception:
            pass
    console.print(f"  [green]💾 Saved: {path}[/]")


@menu_group.command("calendars")
@click.argument("bot_id")
@click.pass_context
def oc_calendars(ctx: click.Context, bot_id: str) -> None:
    """Manage secret calendar URLs for a bot (interactive)."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)
    user = _gateway_runtime_user(bot_id, network)

    while True:
        data = _load_calendars(user)
        calendars = data.setdefault("calendars", [])
        path = _cal_config_path(user)

        console.print(f"\n[bold]── {bot_id.upper()} Calendar URLs ──────────────────────────────────[/]")
        console.print(f"  File: {path}")
        if not calendars:
            console.print("  (none configured)")
        else:
            for entry in calendars:
                url = entry.get("url", "")
                masked = url[:40] + "…" if len(url) > 40 else url
                console.print(f"  [{entry.get('id','')}] {entry.get('name','')}  ({entry.get('type','')})")
                console.print(f"    url: {masked}")

        console.print()
        console.print("  [a] Add   [r] Remove   [Enter] Back")
        sub = click.prompt("  Choice", default="").strip().lower()

        if sub == "a":
            name = click.prompt("  Name (e.g. 'Example Corp Gmail')").strip()
            if not name:
                continue
            cal_id = re.sub(r'[^a-z0-9\-]', '-', name.lower()).strip('-')
            cal_id = re.sub(r'-+', '-', cal_id)
            cal_id = click.prompt(f"  ID", default=cal_id).strip()
            if any(e.get("id") == cal_id for e in calendars):
                if not click.confirm(f"  '{cal_id}' exists. Overwrite?", default=False):
                    continue
                calendars[:] = [e for e in calendars if e.get("id") != cal_id]
            cal_type = click.prompt("  Type", default="ical_url").strip()
            console.print("  [yellow]⚠️  Secret URLs are like passwords — don't share them.[/]")
            url = click.prompt("  URL").strip()
            if not url:
                continue
            calendars.append({"id": cal_id, "name": name, "type": cal_type, "url": url})
            _save_calendars(user, data)
            console.print(f"  [green]✅ Saved '{name}' (id: {cal_id})[/]")

        elif sub == "r":
            if not calendars:
                console.print("  Nothing to remove.")
                continue
            for i, e in enumerate(calendars, 1):
                console.print(f"    {i}. [{e.get('id','')}] {e.get('name','')}")
            num = click.prompt("  Number to remove", default="").strip()
            try:
                idx = int(num) - 1
                entry = calendars[idx]
                if click.confirm(f"  Remove '{entry.get('name','')}' ({entry.get('id','')})?", default=False):
                    calendars.pop(idx)
                    _save_calendars(user, data)
                    console.print("  [green]✅ Removed[/]")
            except (ValueError, IndexError):
                console.print("  Invalid number.")

        else:
            break


# ── oc gws ───────────────────────────────────────────────────────────────────

GWS_ACCOUNTS: dict[str, str] = {}  # populated from network config at runtime
GWS_CONFIG_DIR = ".config/gws"
GWS_TOKEN_MAX_AGE_DAYS = 14


def _gws_account(bot_id: str, network: dict) -> str | None:
    """Return the Google account email for a bot, from network config or built-in map."""
    bot_cfg = network.get("bots", {}).get(bot_id, {})
    return bot_cfg.get("gws_account")


def _gws_token_status(user: str) -> dict:
    cfg = Path(f"/Users/{user}") / GWS_CONFIG_DIR
    result: dict = {"configured": False, "token_age_days": None, "token_fresh": False, "missing_files": []}
    for fname in ("client_secret.json", "credentials.enc", "token_cache.json"):
        if not (cfg / fname).exists():
            result["missing_files"].append(fname)
    if result["missing_files"]:
        return result
    result["configured"] = True
    token_cache = cfg / "token_cache.json"
    age_days = (datetime.now().timestamp() - token_cache.stat().st_mtime) / 86400
    result["token_age_days"] = round(age_days, 1)
    result["token_fresh"] = age_days < GWS_TOKEN_MAX_AGE_DAYS
    return result


@menu_group.command("gws")
@click.argument("bot_id")
@click.option("--reauth", is_flag=True, default=False, help="Run OAuth reauth flow")
@click.pass_context
def oc_gws(ctx: click.Context, bot_id: str, reauth: bool) -> None:
    """Show Google Workspace token status for a bot; optionally reauth."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)
    user    = _gateway_runtime_user(bot_id, network)
    account = _gws_account(bot_id, network)

    console.print(f"\n[bold]── {bot_id.upper()} Google Workspace ──────────────────────────[/]")

    if not account:
        console.print(f"  [yellow](no gws_account configured for {bot_id} in network.json)[/]")
        console.print(f"  Add  \"gws_account\": \"bot@example.com\"  to bots.{bot_id} in network.json")
        return

    console.print(f"  Account: {account}")
    status = _gws_token_status(user)

    if status["missing_files"]:
        console.print(f"  [red]❌ Missing: {', '.join(status['missing_files'])}[/]")
        console.print("  Run with --reauth to set up credentials.")
    else:
        age  = status["token_age_days"]
        fresh = status["token_fresh"]
        icon = "[green]✅[/]" if fresh else "[yellow]⚠️ [/]"
        age_str = f"{age}d ago" if age is not None else "unknown"
        console.print(f"  Token: {icon} last refreshed {age_str}"
                      + ("" if fresh else f"  (>{GWS_TOKEN_MAX_AGE_DAYS}d — likely expired)"))

        # Quick validity check
        cfg_dir = Path(f"/Users/{user}") / GWS_CONFIG_DIR
        try:
            tc = json.loads((cfg_dir / "token_cache.json").read_text())
            expiry = tc.get("expiry") or tc.get("token", {}).get("expiry")
            if expiry:
                exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                if exp_dt < now_dt:
                    console.print(f"  [red]❌ Token expired at {exp_dt.strftime('%Y-%m-%d %H:%M UTC')}[/]")
                else:
                    mins = int((exp_dt - now_dt).total_seconds() / 60)
                    console.print(f"  [green]✅ Token valid for ~{mins}m[/]")
        except Exception:
            pass

    if reauth or click.confirm("\n  Run OAuth reauth flow now?", default=False):
        # The CLI `--reauth` path requires a desktop browser (npx
        # @googleworkspace/cli auth login). Production runs on a headless
        # mini, so we direct the user to the dashboard wizard which works
        # without a local display.
        console.print(
            f"\n  [yellow]⚠️  `oc gws --reauth` is deprecated.[/] "
            f"Use the dashboard wizard instead — it works on headless hosts:"
        )
        console.print(f"    [cyan]Integrations & Keys → Google Workspace row → Set Up[/]")
        _dash_url = f"{resolve_admin_base_url(load_network())}/#bots-{bot_id}"
        console.print(
            f"  Dashboard URL: {_dash_url} "
            f"(the wizard handles consent + token refresh automatically)."
        )
        console.print(
            f"\n  Falling back to the legacy CLI flow (requires a desktop browser):"
        )
        if not click.confirm("  Proceed with legacy CLI flow?", default=False):
            return
        cmd = ["sudo", "-u", user, "bash", "-c",
               f"cd /Users/{user} && {OPENCLAW_NPX} @googleworkspace/cli auth login --account {account}"]
        console.print(f"\n  Running: sudo -u {user} npx @googleworkspace/cli auth login --account {account}\n")
        r = subprocess.run(cmd)
        if r.returncode == 0:
            console.print(f"\n  [green]✅ Reauth complete.[/]")
        else:
            console.print(f"\n  [red]❌ Reauth failed (rc={r.returncode}).[/]")
            console.print(f"  Manual: sudo -u {user} bash -c \"cd /Users/{user} && {OPENCLAW_NPX} @googleworkspace/cli auth login --account {account}\"")


# ── oc usage ─────────────────────────────────────────────────────────────────

_RESET = "\033[0m"

_MODEL_COLORS: dict[str, str] = {
    "anthropic/claude-sonnet-4-6": "\033[36m",
    "anthropic/claude-opus-4-6":   "\033[96m",
    "anthropic/claude-haiku-4-5":  "\033[6m",
    "anthropic:api_key":           "\033[96m",
    "openai/gpt-4o":               "\033[32m",
    "openai/gpt-4.1":              "\033[92m",
    "google/gemini-1.5-pro":       "\033[33m",
    "xai/grok-3":                  "\033[35m",
    "anthropic":                   "\033[36m",
    "openai":                      "\033[32m",
    "google":                      "\033[33m",
    "xai":                         "\033[35m",
    "mistral":                     "\033[34m",
    "unknown":                     "\033[37m",
}

COLLECTOR = "/Users/Shared/openclaw-usage/turn-collector.py"


def _load_user_maps(network: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    """Load channel-keyed user_id → display name maps from network config.

    Returns the `user_maps` section of network.json (e.g.
    `{"slack": {"U0...": "Alice"}, "telegram": {"123": "Bob"}}`), or `{}` if
    the section is missing or the file cannot be read. Personal identity data
    must not be hardcoded — it lives in the pod's network.json.
    """
    if network is None:
        try:
            network = load_network()
        except Exception:
            return {}
    return network.get("user_maps") or {}


def _resolve_user(channel: str, user_id: str,
                  user_maps: dict[str, dict[str, str]] | None = None) -> str:
    """Resolve a channel-specific user_id to a display name.

    `user_maps` should be the dict returned by `_load_user_maps()`. When
    omitted it is loaded lazily. Falls back to returning `user_id` when the
    channel or id is not present in the config.
    """
    if user_maps is None:
        user_maps = _load_user_maps()
    return (user_maps.get(channel) or {}).get(user_id, user_id)


def _turn_key(turn: dict) -> str:
    model    = turn.get("model", "unknown")
    provider = model.split("/")[0] if "/" in model else "unknown"
    if provider == "anthropic" and turn.get("auth_mode") == "api_key":
        return f"{model}:api_key"
    return model


def _model_color(key: str) -> str:
    if key in _MODEL_COLORS:
        return _MODEL_COLORS[key]
    base = key.replace(":api_key", "")
    if base in _MODEL_COLORS:
        return _MODEL_COLORS.get("anthropic:api_key", "\033[96m")
    provider = key.split("/")[0] if "/" in key else key.split(":")[0]
    return _MODEL_COLORS.get(provider, _MODEL_COLORS["unknown"])


def _load_turns(network: dict, days: int = 1, end_date: datetime | None = None,
                bot_filter: str | None = None) -> list[dict]:
    if end_date is None:
        end_date = datetime.now()
    dates = [(end_date - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(days - 1, -1, -1)]

    bot_ids = [bot_filter] if bot_filter else _bot_ids(network)
    turns: list[dict] = []
    for bot_id in bot_ids:
        user = _gateway_runtime_user(bot_id, network)
        mem_dir = Path(f"/Users/{user}/.openclaw/workspace/memory")
        for date_str in dates:
            path = mem_dir / f"turns-{date_str}.jsonl"
            if not path.exists():
                continue
            try:
                for line in path.read_text().splitlines():
                    line = line.strip()
                    if line:
                        try:
                            rec = json.loads(line)
                            rec["instance"] = bot_id
                            turns.append(rec)
                        except Exception:
                            pass
            except Exception:
                pass
    return turns


def _print_usage(turns: list[dict], days: int, bot_filter: str | None = None) -> None:
    if not turns:
        console.print("  No turn data found.")
        return

    by_date:     dict = defaultdict(int)
    by_date_key: dict = defaultdict(lambda: defaultdict(int))
    by_instance: dict = defaultdict(int)
    by_channel:  dict = defaultdict(int)
    by_model:    dict = defaultdict(int)
    by_user:     dict = defaultdict(int)

    user_maps = _load_user_maps()

    for t in turns:
        date    = t.get("ts", "")[:10]
        key     = _turn_key(t)
        channel = t.get("channel", "?")
        user_id = t.get("user_id", "?")
        by_date[date] += 1
        by_date_key[date][key] += 1
        by_instance[t.get("instance", "?")] += 1
        by_channel[channel] += 1
        by_model[t.get("model", "?")] += 1
        by_user[f"{channel}:{_resolve_user(channel, user_id, user_maps)}"] += 1

    scope = bot_filter or "all bots"
    BAR_WIDTH = 35
    seen_keys: set = set()
    for dk in by_date_key.values():
        seen_keys.update(dk.keys())

    print(f"\n  {'─'*56}")
    print(f"  Usage — {scope} — last {days} day{'s' if days != 1 else ''} — {len(turns)} turns total")
    print(f"  {'─'*56}")

    print("\n  By date:")
    for d in sorted(by_date):
        count = by_date[d]
        day_keys = by_date_key[d]
        total = sum(day_keys.values())
        bar = ""
        allocated = 0
        keys_sorted = sorted(day_keys.items(), key=lambda x: -x[1])
        for i, (key, cnt) in enumerate(keys_sorted):
            color = _model_color(key)
            blocks = (BAR_WIDTH - allocated) if i == len(keys_sorted) - 1 else round(cnt / total * BAR_WIDTH)
            allocated += blocks
            bar += f"{color}{'█' * blocks}{_RESET}"
        print(f"    {d}  {count:4d}  {bar}")

    print("\n  Legend:")
    for key in sorted(seen_keys):
        color = _model_color(key)
        label = key.replace(":api_key", "  [API key/metered]")
        print(f"    {color}██{_RESET}  {color}{label}{_RESET}")

    if not bot_filter:
        print("\n  By bot:")
        for k, v in sorted(by_instance.items(), key=lambda x: -x[1]):
            print(f"    {k:10s}  {v:4d}")

    print("\n  By channel:")
    for k, v in sorted(by_channel.items(), key=lambda x: -x[1]):
        print(f"    {k:12s}  {v:4d}")

    print("\n  By model:")
    for k, v in sorted(by_model.items(), key=lambda x: -x[1]):
        color = _model_color(k)
        print(f"    {color}██{_RESET}  {color}{k:43s}{_RESET}  {v:4d}")

    anthropic_token   = sum(1 for t in turns if t.get("model","").startswith("anthropic/") and t.get("auth_mode") == "token")
    anthropic_api_key = sum(1 for t in turns if t.get("model","").startswith("anthropic/") and t.get("auth_mode") != "token")
    non_anthropic     = sum(1 for t in turns if not t.get("model","").startswith("anthropic/"))
    total_metered     = anthropic_api_key + non_anthropic

    print("\n  Billing summary:")
    print(f"    {'MAX subscription (flat — Anthropic token)':42s}  {anthropic_token:4d}")
    print(f"    {'API key / metered (total)':42s}  {total_metered:4d}")
    print(f"      {'└ Anthropic API key fallback':40s}  {anthropic_api_key:4d}")
    print(f"      {'└ Non-Anthropic':40s}  {non_anthropic:4d}")
    if anthropic_token + total_metered > 0:
        pct = round(100 * anthropic_token / (anthropic_token + total_metered))
        print(f"    {'Anthropic MAX coverage':42s}  {pct}%")

    print("\n  By user (top 10):")
    for k, v in sorted(by_user.items(), key=lambda x: -x[1])[:10]:
        print(f"    {k:45s}  {v:4d}")
    print()


@menu_group.command("usage")
@click.option("--bot", "bot_filter", default=None, help="Filter to a specific bot")
@click.option("--days", default=7, show_default=True, help="Number of days to look back")
@click.option("--start", default=None, help="Start date YYYY-MM-DD (overrides --days)")
@click.option("--end", default=None, help="End date YYYY-MM-DD (default: today)")
@click.option("--channel", default=None, help="Filter by channel")
@click.option("--csv-out", "csv_out", default=None, help="Export turns to CSV file")
@click.option("--collect", is_flag=True, default=False,
              help="Re-collect turns from gateway log before reporting")
@click.pass_context
def oc_usage(ctx: click.Context, bot_filter: str | None, days: int,
             start: str | None, end: str | None, channel: str | None,
             csv_out: str | None, collect: bool) -> None:
    """Turn-level usage analytics with billing breakdown."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)

    if collect:
        _collect_turns(network, bot_filter)

    end_date = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
    if start:
        start_date = datetime.strptime(start, "%Y-%m-%d")
        days = (end_date - start_date).days + 1

    turns = _load_turns(network, days=days, end_date=end_date, bot_filter=bot_filter)

    if channel:
        turns = [t for t in turns if t.get("channel") == channel]

    if csv_out:
        fields = ["ts", "instance", "channel", "user_id", "model", "source", "msg_id"]
        with open(csv_out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(turns, key=lambda t: t.get("ts", "")))
        console.print(f"[green]✅ Exported {len(turns)} turns to {csv_out}[/]")
        return

    _print_usage(turns, days, bot_filter)


def _collect_turns(network: dict, bot_filter: str | None = None) -> None:
    if not os.path.exists(COLLECTOR):
        console.print(f"[yellow]⚠️  Collector not found: {COLLECTOR}[/]")
        return
    bot_ids = [bot_filter] if bot_filter else _bot_ids(network)
    for bot_id in bot_ids:
        user    = _gateway_runtime_user(bot_id, network)
        log     = _gateway_log(user)
        mem_dir = Path(f"/Users/{user}/.openclaw/workspace/memory")
        if not log.exists():
            console.print(f"  [yellow]⚠️  {bot_id}: log not found ({log})[/]")
            continue
        r = subprocess.run(
            ["python3", COLLECTOR, "--instance", bot_id, "--log", str(log), "--out", str(mem_dir)],
            capture_output=True, text=True,
        )
        for line in (r.stdout + r.stderr).strip().splitlines():
            console.print(f"  {line}")


# ── Interactive menu ─────────────────────────────────────────────────────────
#
# Replicates the single-letter navigation from the original openclaw-admin.py.
# `evolve-admin menu` (or just running with no subcommand) drops into this.

MENU_HELP = """
  ══════════════════════════════════════════════════════════
  Evolve Admin — Menu Help
  ══════════════════════════════════════════════════════════

  MENU OPTIONS (single bot)
  ─────────────────────────
  [a] Add model        Add a model to the catalog
  [r] Remove model     Remove a model (must not be active)
  [o] Set order        Set primary + fallback order by number
  [k] Rotate keys      Update API keys: Anthropic, OpenAI, Brave, Slack, Telegram
  [g] Restart gateway  Restart via launchctl kickstart
  [i] Info             Model config, auth profile status, gateway process
  [l] Logs             Tail gateway.log and gateway.err.log
  [x] Processes        Show/kill gateway processes
  [p] Plugins          Manage plugin allow/deny lists and enable/disable
                       ⚠️  Non-empty allow list EXCLUDES all other plugins!
  [d] Doctor           Run `openclaw doctor` for the bot
  [w] Google WS        Check GWS token status and run OAuth reauth
  [c] Calendars        Manage secret iCal/feed URLs
  [u] Usage reports    Turn-level stats by date/channel/model; CSV export
  [v] Version          Check installed vs latest OpenClaw; upgrade
  [e] Exec approvals   Configure shell command execution approvals
  [b] Switch bot       Switch to another bot or 'all'
  [h] Help             This screen
  [q] Quit

  KEY CONCEPTS
  ────────────
  plugins.allow    Non-empty = ONLY listed plugins load. Empty = all load.
  plugins.deny     Listed plugins are skipped.
  execApprovals    Controls exec gating — bots cannot run shell commands without it.
  lastGood         Which auth profile last succeeded — gateway auto-prefers it.

  COMMON WORKFLOWS
  ────────────────
  Rotate a key:       [k] → enter new key → gateway auto-reloads
  Bot not responding: [i] check profile status, [l] recent errors, [g] restart
  Plugin not loading: [p] → check allow list isn't excluding it
  Duplicate process:  [x] → kill older PID
  ══════════════════════════════════════════════════════════
"""


def _mask(key: str) -> str:
    if not key or len(key) < 12:
        return key or "(empty)"
    return key[:8] + "..." + key[-4:]


def _find_profile(profiles: dict, provider: str, mode: str):
    for name, p in profiles.items():
        if p.get("provider") == provider:
            pmode = p.get("type", p.get("mode", ""))
            if pmode == mode:
                field = "token" if mode == "token" else "key"
                if field in p:
                    return name, p, field
    return None, None, None


PROVIDER_META = {
    "anthropic": {"hint": "sk-ant-api03-...", "token_hint": "sk-ant-oat01-...", "has_token": True},
    "openai":    {"hint": "sk-...",           "has_token": False},
    "google":    {"hint": "AIza...",          "has_token": False},
    "xai":       {"hint": "xai-...",          "has_token": False},
    "mistral":   {"hint": "mistral-...",      "has_token": False},
    "groq":      {"hint": "gsk_...",          "has_token": False},
    "perplexity":{"hint": "pplx-...",         "has_token": False},
    "moonshot":  {"hint": "sk-...",           "has_token": False},
}


def _build_provider_list(catalog: dict, profiles: dict) -> list:
    providers: set = set()
    for model in catalog:
        if "/" in model:
            providers.add(model.split("/")[0])
    for p in profiles.values():
        prov = p.get("provider", "")
        if prov:
            providers.add(prov)
    result = []
    seen: set = set()
    for provider in sorted(providers):
        meta = PROVIDER_META.get(provider, {"hint": "...", "has_token": False})
        key = (provider, "api_key")
        if key not in seen:
            result.append((provider, "api_key", f"{provider.capitalize()} API Key ({meta['hint']})"))
            seen.add(key)
        if meta.get("has_token"):
            key2 = (provider, "token")
            if key2 not in seen:
                result.append((provider, "token", f"{provider.capitalize()} MAX Token ({meta.get('token_hint','...')})"))
                seen.add(key2)
    return result


def _get_model_config(cfg: dict) -> dict:
    return cfg.get("agents", {}).get("defaults", {}).get("model", {})


def _set_model_config(cfg: dict, mc: dict) -> None:
    cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = mc


def _get_catalog(cfg: dict) -> dict:
    return cfg.get("agents", {}).get("defaults", {}).get("models", {})


def _set_catalog(cfg: dict, catalog: dict) -> None:
    cfg.setdefault("agents", {}).setdefault("defaults", {})["models"] = catalog


def _display_models(model_config: dict, catalog: dict) -> list:
    primary   = model_config.get("primary", "(none)")
    fallbacks = model_config.get("fallbacks", [])
    print(f"  Primary:   {primary}")
    for i, f in enumerate(fallbacks, 1):
        print(f"  Fallback {i}: {f}")
    active = [primary] + [f for f in fallbacks if f != primary]
    all_models = list(active)
    for m in catalog:
        if m not in all_models:
            all_models.append(m)
    print("  Catalog:")
    for i, m in enumerate(all_models, 1):
        alias = catalog.get(m, {}).get("alias", "") if isinstance(catalog.get(m), dict) else ""
        alias_str = f"  [{alias}]" if alias else ""
        if m == primary:
            tag = "  ← PRIMARY"
        elif m in fallbacks:
            tag = f"  ← FALLBACK {fallbacks.index(m)+1}"
        else:
            tag = ""
        print(f"    {i:2}. {m}{alias_str}{tag}")
    return all_models


def _menu_load_bot(bot_id: str, network: dict) -> tuple:
    """Load (user, config_path, auth_path, cfg, auth) for a bot."""
    user      = _gateway_runtime_user(bot_id, network)
    cfg_path  = _openclaw_json(user)
    auth_path = _auth_json(user)
    cfg  = _load_json_permissive(cfg_path) if cfg_path.exists() else {}
    try:
        auth = _load_json_permissive(auth_path) if auth_path.exists() else {"profiles": {}}
    except Exception:
        auth = {"profiles": {}}
    return user, cfg_path, auth_path, cfg, auth


def _menu_save_cfg(cfg: dict, path: Path) -> None:
    _preserve_write(cfg, path)
    print(f"  💾 Saved: {path}")


def _menu_save_auth(auth: dict, path: Path) -> None:
    _preserve_write(auth, path)
    print(f"  💾 Saved: {path}")


def _menu_restart_gateway(bot_id: str) -> None:
    svc = _bot_service(bot_id)
    ok, out = get_scheduler().restart(svc)
    if ok:
        print(f"  ✅ {svc} restarted")
    else:
        print(f"  ⚠️  {out.strip() or 'unknown error'}")


def _menu_show_info(bot_id: str, network: dict) -> None:
    user, cfg_path, auth_path, cfg, auth = _menu_load_bot(bot_id, network)
    print(f"\n  ── {bot_id.upper()} ({user}) ────────────────────────────")
    mc      = _get_model_config(cfg)
    catalog = _get_catalog(cfg)
    print(f"  Primary: {mc.get('primary', '(none)')}")
    for i, f in enumerate(mc.get("fallbacks", []), 1):
        print(f"  Fallback {i}: {f}")
    port = cfg.get("gateway", {}).get("port", "?")
    print(f"  Port: {port}")
    now_ms    = int(datetime.now().timestamp() * 1000)
    last_good = auth.get("lastGood", {})
    stats     = auth.get("usageStats", {})
    for pname, pdata in auth.get("profiles", {}).items():
        pstats   = stats.get(pname, {})
        last_used = pstats.get("lastUsed", 0)
        cooldown  = pstats.get("cooldownUntil", 0)
        errors    = pstats.get("errorCount", 0)
        is_good   = any(v == pname for v in last_good.values())
        is_cool   = cooldown > now_ms
        cool_secs = max(0, (cooldown - now_ms) // 1000)
        atype     = pdata.get("type", pdata.get("mode", "?"))
        type_label = {"token": "MAX/token", "api_key": "API key"}.get(atype, atype)
        provider  = pdata.get("provider", "?")
        lu_str    = datetime.fromtimestamp(last_used / 1000).strftime("%H:%M") if last_used else "never"
        if is_cool:
            status = f"🔴 rate limited ({cool_secs // 60}m {cool_secs % 60}s)"
        elif errors > 0:
            status = f"⚠️  {errors} error(s)"
        elif is_good:
            status = "✅ ok"
        else:
            status = ""
        print(f"    {pname:20s} [{provider}/{type_label}] used:{lu_str} {status}")
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    procs = [l for l in result.stdout.splitlines()
             if "openclaw-gateway" in l and l.split()[0] == user]
    if not procs:
        print("  Gateway: ❌ NOT RUNNING")
    elif len(procs) == 1:
        print(f"  Gateway: ✅ running (PID {procs[0].split()[1]})")
    else:
        pids = ", ".join(l.split()[1] for l in procs)
        print(f"  Gateway: ⚠️  {len(procs)} processes (PIDs {pids})")


def _menu_show_logs(bot_id: str, network: dict, n: int = 30) -> None:
    user = _gateway_runtime_user(bot_id, network)
    print(f"\n  ── {bot_id.upper()} logs ──────────────────────────────")
    for path, label in [(_gateway_log(user), "gateway.log"),
                         (_gateway_err_log(user), "gateway.err.log")]:
        if path.exists():
            r = subprocess.run(["tail", f"-{n}", str(path)], capture_output=True, text=True)
            print(f"  {label}:")
            print(r.stdout or "  (empty)")
        else:
            print(f"  {label}: not found")


def _menu_show_process(bot_id: str, network: dict) -> list:
    user = _gateway_runtime_user(bot_id, network)
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    procs = [l for l in result.stdout.splitlines()
             if "openclaw-gateway" in l and l.split()[0] == user]
    print(f"\n  ── {bot_id.upper()} ({user}) ──")
    if not procs:
        print("  ❌ NOT RUNNING")
    else:
        for line in procs:
            parts = line.split()
            pid  = parts[1]
            stat = parts[7] if len(parts) > 7 else "?"
            t    = parts[8] if len(parts) > 8 else "?"
            print(f"  PID {pid}  stat={stat}  started={t}")
    return procs


def _menu_rotate_keys_single(bot_id: str, network: dict) -> None:
    user, cfg_path, auth_path, cfg, auth = _menu_load_bot(bot_id, network)
    catalog  = _get_catalog(cfg)
    profiles = auth.get("profiles", {})

    print(f"\n  Rotating keys for {bot_id.upper()}. Enter to keep current.")
    auth_changed = False

    for provider, mode, description in _build_provider_list(catalog, profiles):
        name, profile, field = _find_profile(profiles, provider, mode)
        current = profile.get(field, "") if profile else ""
        print(f"\n  {description}")
        print(f"    Current: {_mask(current) if profile else '(not configured)'}")
        new_key = input("    New key (Enter to skip): ").strip()
        if new_key:
            if profile is None:
                pname = f"{provider}:default"
                if pname in profiles:
                    pname = f"{provider}:api"
                profiles[pname] = {"provider": provider, "type": mode}
                profile = profiles[pname]
                field = "token" if mode == "token" else "key"
            profile[field] = new_key
            auth_changed = True
            print("    ✅ Updated")
    if auth_changed:
        _menu_save_auth(auth, auth_path)

    # Brave key
    print("\n  Brave Search API Key")
    canonical = (cfg.get("plugins", {}).get("entries", {})
                    .get("brave", {}).get("config", {})
                    .get("webSearch", {}).get("apiKey", ""))
    legacy = (cfg.get("tools", {}).get("web", {})
                 .get("search", {}).get("apiKey", ""))
    if canonical:
        print(f"    Current (canonical): {_mask(canonical)}")
    elif legacy:
        print(f"    Current (⚠️  legacy path): {_mask(legacy)}")
        migrate = input("    Migrate to canonical path? (Y/n): ").strip().lower()
        if migrate != "n":
            (cfg.setdefault("plugins", {}).setdefault("entries", {})
               .setdefault("brave", {}).setdefault("config", {})
               .setdefault("webSearch", {}))["apiKey"] = legacy
            cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {}).pop("apiKey", None)
            cfg["tools"]["web"]["search"]["provider"] = "brave"
            _menu_save_cfg(cfg, cfg_path)
            print("    ✅ Migrated")
    else:
        print("    Current: (not configured)")
    new_key = input("    New Brave key (Enter to skip): ").strip()
    if new_key:
        (cfg.setdefault("plugins", {}).setdefault("entries", {})
           .setdefault("brave", {}).setdefault("config", {})
           .setdefault("webSearch", {}))["apiKey"] = new_key
        cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {}).pop("apiKey", None)
        cfg["tools"]["web"]["search"]["provider"] = "brave"
        _menu_save_cfg(cfg, cfg_path)
        print("    ✅ Brave key updated")

    # Channel tokens
    channels_cfg = cfg.get("channels", {})
    tg = channels_cfg.get("telegram", {})
    if isinstance(tg, dict):
        enabled_str = "" if tg.get("enabled") else "  [disabled]"
        current = tg.get("botToken", "")
        print(f"\n  Telegram Bot Token{enabled_str}")
        print(f"    Current: {_mask(current) if current else '(not configured)'}")
        new_key = input("    New token (Enter to skip): ").strip()
        if new_key:
            tg["botToken"] = new_key
            cfg.setdefault("channels", {})["telegram"] = tg
            _menu_save_cfg(cfg, cfg_path)
            print("    ✅ Telegram token updated (restart gateway to apply)")

    slack = channels_cfg.get("slack", {})
    if isinstance(slack, dict):
        enabled_str = "" if slack.get("enabled") else "  [disabled]"
        for field, label in [("botToken", "Slack Bot Token (xoxb-...)"),
                              ("appToken", "Slack App Token (xapp-...)")]:
            current = slack.get(field, "")
            print(f"\n  {label}{enabled_str}")
            print(f"    Current: {_mask(current) if current else '(not configured)'}")
            new_key = input("    New token (Enter to skip): ").strip()
            if new_key:
                slack[field] = new_key
                cfg.setdefault("channels", {})["slack"] = slack
                _menu_save_cfg(cfg, cfg_path)
                print(f"    ✅ {label.split()[1]} updated (restart gateway to apply)")


def _menu_plugins_single(bot_id: str, network: dict) -> None:
    user, cfg_path, auth_path, cfg, auth = _menu_load_bot(bot_id, network)

    while True:
        plugins_cfg = cfg.get("plugins", {})
        entries = plugins_cfg.get("entries", {})
        allow   = plugins_cfg.get("allow", [])
        deny    = plugins_cfg.get("deny", [])

        print(f"\n  ── {bot_id.upper()} Plugins ──────────────────────────")
        print(f"  allow: {allow if allow else '(empty — all allowed)'}")
        print(f"  deny:  {deny if deny else '(empty)'}")
        if entries:
            print("  Entries:")
            for pname, pdata in entries.items():
                enabled = pdata.get("enabled", "?") if isinstance(pdata, dict) else "?"
                icon = "✅" if enabled is True else "❌" if enabled is False else "?"
                print(f"    {icon} {pname}")
        print()
        print("  [1] Edit allow   [2] Edit deny   [3] Toggle entry   [Enter] Back")
        sub = input("  Choice: ").strip()

        if sub == "1":
            print(f"  Current allow: {allow}")
            print("  ⚠️  Non-empty allow EXCLUDES all other plugins!")
            raw = input("  allow (comma-separated, empty = allow all): ").strip()
            new_allow = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
            unknown = [x for x in new_allow if x not in KNOWN_PLUGINS]
            if unknown:
                print(f"  ⚠️  Unrecognized: {unknown}")
                if input("  Continue? (y/N): ").strip().lower() != "y":
                    continue
            conflicts = [x for x in new_allow if x in deny]
            if conflicts:
                print(f"  ⚠️  Removing {conflicts} from deny (conflict)")
                deny = [x for x in deny if x not in conflicts]
                plugins_cfg["deny"] = deny
            if new_allow:
                plugins_cfg["allow"] = new_allow
            else:
                plugins_cfg.pop("allow", None)
            cfg["plugins"] = plugins_cfg
            _menu_save_cfg(cfg, cfg_path)

        elif sub == "2":
            print(f"  Current deny: {deny}")
            raw = input("  deny (comma-separated, empty = deny none): ").strip()
            new_deny = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
            conflicts = [x for x in new_deny if x in allow]
            if conflicts:
                print(f"  ⚠️  Removing {conflicts} from allow (conflict)")
                allow = [x for x in allow if x not in conflicts]
                if allow:
                    plugins_cfg["allow"] = allow
                else:
                    plugins_cfg.pop("allow", None)
            if new_deny:
                plugins_cfg["deny"] = new_deny
            else:
                plugins_cfg.pop("deny", None)
            cfg["plugins"] = plugins_cfg
            _menu_save_cfg(cfg, cfg_path)

        elif sub == "3":
            if not entries:
                print("  No entries.")
                continue
            plist = list(entries.keys())
            for i, pname in enumerate(plist, 1):
                enabled = entries[pname].get("enabled", "?") if isinstance(entries[pname], dict) else "?"
                icon = "✅" if enabled is True else "❌"
                print(f"    {i}. {icon} {pname}")
            num = input("  Toggle number: ").strip()
            try:
                idx = int(num) - 1
                pname = plist[idx]
                current = entries[pname].get("enabled", True) if isinstance(entries[pname], dict) else True
                entries[pname]["enabled"] = not current
                plugins_cfg["entries"] = entries
                cfg["plugins"] = plugins_cfg
                _menu_save_cfg(cfg, cfg_path)
                print(f"  ✅ {pname} → {'enabled' if not current else 'disabled'}")
            except (ValueError, IndexError):
                print("  Invalid number.")
        else:
            break


def _menu_gws_single(bot_id: str, network: dict) -> None:
    user = _gateway_runtime_user(bot_id, network)
    account = _gws_account(bot_id, network)
    print(f"\n  ── {bot_id.upper()} Google Workspace ──────────────────────────")
    if not account:
        print(f"  (no gws_account configured for {bot_id} in network.json)")
        return
    print(f"  Account: {account}")
    status = _gws_token_status(user)
    if status["missing_files"]:
        print(f"  ❌ Missing: {', '.join(status['missing_files'])}")
    else:
        age   = status["token_age_days"]
        fresh = status["token_fresh"]
        icon  = "✅" if fresh else "⚠️ "
        print(f"  Token: {icon} last refreshed {age}d ago"
              + ("" if fresh else f" (>{GWS_TOKEN_MAX_AGE_DAYS}d — likely expired)"))
    print()
    sub = input("  [r] Reauth  [Enter] Back: ").strip().lower()
    if sub == "r":
        if not account:
            print("  ❌ No Google account configured.")
            return
        print("\n  ⚠️  `oc gws` reauth is deprecated. Use the dashboard wizard:")
        print("       Integrations & Keys → Google Workspace row → Set Up")
        print("       (works on headless hosts; CLI path requires a desktop browser).")
        if input("  Proceed with legacy CLI flow anyway? (y/N): ").strip().lower() != "y":
            return
        cmd = ["sudo", "-u", user, "bash", "-c",
               f"cd /Users/{user} && {OPENCLAW_NPX} @googleworkspace/cli auth login --account {account}"]
        result = subprocess.run(cmd)
        if result.returncode == 0:
            print("  ✅ Reauth complete.")
        else:
            print(f"  ❌ Failed (rc={result.returncode})")


def _menu_version(network: dict) -> None:
    installed = _installed_version()
    print(f"\n  ── OpenClaw Version ──────────────────────────────────")
    print(f"  Installed:  {installed}")
    print("  Checking npm registry...", end="", flush=True)
    latest = _latest_version()
    print(f"\r  Latest:     {latest}      ")
    if latest.startswith("(error"):
        print("  ⚠️  Could not reach npm registry.")
        return installed, latest, False
    up_to_date = installed == latest
    if up_to_date:
        print("  ✅ Up to date.")
    else:
        print(f"  🔼 Update available: {installed} → {latest}")

    print(f"\n  ── Gateway processes ──────────────────────────────────")
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    for bot_id in _bot_ids(network):
        user = _gateway_runtime_user(bot_id, network)
        procs = [l for l in result.stdout.splitlines()
                 if _is_gateway_proc(l, user)]
        if procs:
            pid = procs[0].split()[1]
            status = "✅" if len(procs) == 1 else f"⚠️  {len(procs)} procs"
            print(f"  {bot_id:8s}  PID {pid}  {status}")
        else:
            print(f"  {bot_id:8s}  ❌ not running")
    return installed, latest, not up_to_date


def _menu_version_loop(network: dict) -> None:
    while True:
        result = _menu_version(network)
        if result is None:
            break
        installed, latest, update_available = result
        print()
        if update_available:
            print("  [u] Upgrade to latest   [r] Restart all gateways   [Enter] Back")
        else:
            print("  [r] Restart all gateways   [Enter] Back")
        sub = input("  Choice: ").strip().lower()
        if sub == "u":
            if latest.startswith("(error"):
                print("  ❌ Cannot reach npm registry.")
                continue
            print(f"\n  Upgrading {installed} → {latest}")
            if input("  Proceed? (y/N): ").strip().lower() != "y":
                print("  Cancelled.")
                continue
            # Mirror the click `oc upgrade` preflight so the interactive
            # `[u]` path benefits from the same ENOTEMPTY-on-orphan rescue.
            if not _check_and_clean_stale_npm_temp_dirs():
                continue
            target_spec = f"openclaw@{latest}"
            cmd = ["npm", "install", "-g", f"--prefix={OPENCLAW_NPM_PREFIX}", target_spec]
            print(f"  Running: {' '.join(cmd[1:])}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  ❌ npm install failed:\n{r.stderr[:500]}")
                console.print(_format_npm_install_error_hint(r.stderr))
                continue
            new_ver = _installed_version()
            if new_ver != latest:
                print(
                    f"  ❌ npm reported success but {OPENCLAW_PACKAGE_JSON} still reads "
                    f"{new_ver} (expected {latest})."
                )
                if r.stdout.strip():
                    print(f"\n  npm stdout:\n{r.stdout[:800]}")
                if r.stderr.strip():
                    print(f"\n  npm stderr:\n{r.stderr[:800]}")
                npm_root = subprocess.run(
                    ["npm", "root", "-g"], capture_output=True, text=True,
                ).stdout.strip()
                print(
                    f"\n  npm root -g: {npm_root}\n"
                    f"  Expected:    {OPENCLAW_PACKAGE_JSON.parent}\n"
                    f"  If those differ, the gateway plists are pinned to a path npm isn't installing to."
                )
                continue
            print(f"  ✅ Installed: {new_ver}")
            print("\n  Checking for conflicting user-level LaunchAgents...")
            _remove_conflicting_user_agents(network)
            print("\n  Restarting all gateways...")
            for bot_id in _bot_ids(network):
                _menu_restart_gateway(bot_id)
            print("\n  ✅ Upgrade complete.")
        elif sub == "r":
            if input("  Restart ALL gateways? (y/N): ").strip().lower() == "y":
                for bot_id in _bot_ids(network):
                    _menu_restart_gateway(bot_id)
        else:
            break


def _single_bot_loop(bot_id: str, network: dict) -> None:
    while True:
        user, cfg_path, auth_path, cfg, auth = _menu_load_bot(bot_id, network)
        model_config = _get_model_config(cfg)
        catalog      = _get_catalog(cfg)

        print()
        print(f"━━━ {bot_id.upper()} ({user}) ━━━")
        all_models = _display_models(model_config, catalog)
        print()
        print("  [a] Add model    [r] Remove model   [o] Set order")
        print("  [k] Rotate keys  [g] Restart gateway")
        print("  [i] Info         [l] Logs            [x] Processes")
        print("  [p] Plugins      [d] Doctor          [b] Switch bot")
        print("  [w] Google Workspace                 [c] Calendars")
        print("  [u] Usage        [v] Version         [e] Exec approvals")
        print("  [h] Help         [q] Quit")
        print()
        choice = input("  Choice: ").strip().lower()

        if choice == "i":
            _menu_show_info(bot_id, network)

        elif choice == "l":
            n_str = input("  Lines? (default 30): ").strip()
            _menu_show_logs(bot_id, network, int(n_str) if n_str.isdigit() else 30)

        elif choice == "x":
            procs = _menu_show_process(bot_id, network)
            if procs:
                if input("\n  Kill process(es)? (y/N): ").strip().lower() == "y":
                    for line in procs:
                        pid = line.split()[1]
                        subprocess.run(["kill", "-9", pid])
                    print("  ✅ Killed — launchd will restart")

        elif choice == "p":
            _menu_plugins_single(bot_id, network)

        elif choice == "d":
            print(f"\n  Running doctor for {bot_id.upper()} ({user})...")
            # Both the bot home and the openclaw binary are platform-keyed
            # (#3227 follow-up): home via the pwd-first `user_home` resolver
            # ({user_home_root}/{user} fallback — /home on Linux, /Users on
            # macOS), binary via `_openclaw_cli_path` (Homebrew on macOS,
            # /usr/bin on Linux). cd first so Node's startup uv_cwd() lands in
            # a dir the bot can traverse (see CLAUDE.md SSH note).
            home = user_home(user)
            oc   = _openclaw_cli_path()
            subprocess.run(["sudo", "-u", user, "bash", "-c",
                            f"cd {home} && {oc} doctor"])

        elif choice == "a":
            new_model = input("  Model name (e.g. anthropic/claude-sonnet-4-6): ").strip()
            if not new_model:
                continue
            alias = input("  Alias (Enter to skip): ").strip()
            catalog[new_model] = {"alias": alias} if alias else {}
            _set_catalog(cfg, catalog)
            _menu_save_cfg(cfg, cfg_path)

        elif choice == "r":
            num = input("  Number to remove: ").strip()
            try:
                idx   = int(num) - 1
                model = all_models[idx]
                primary   = model_config.get("primary", "")
                fallbacks = model_config.get("fallbacks", [])
                if model == primary or model in fallbacks:
                    print(f"  ⚠️  {model} is active — update order [o] first.")
                    continue
                catalog.pop(model, None)
                _set_catalog(cfg, catalog)
                _menu_save_cfg(cfg, cfg_path)
                print(f"  ✅ Removed {model}")
            except (ValueError, IndexError):
                print("  Invalid number.")

        elif choice == "o":
            print("  Numbers space-separated. First = primary, rest = fallbacks.")
            raw = input("  Order: ").strip()
            try:
                nums     = [int(x) for x in raw.split()]
                selected = [all_models[n - 1] for n in nums]
                model_config["primary"]   = selected[0]
                model_config["fallbacks"] = selected[1:]
                _set_model_config(cfg, model_config)
                for m in selected:
                    if m not in catalog:
                        catalog[m] = {}
                _set_catalog(cfg, catalog)
                _menu_save_cfg(cfg, cfg_path)
                print(f"  ✅ Primary: {selected[0]}")
                for i, f in enumerate(selected[1:], 1):
                    print(f"  ✅ Fallback {i}: {f}")
            except (ValueError, IndexError) as e:
                print(f"  Invalid: {e}")

        elif choice == "k":
            _menu_rotate_keys_single(bot_id, network)

        elif choice == "w":
            _menu_gws_single(bot_id, network)

        elif choice == "c":
            # Reuse the interactive calendar loop
            user_local = _gateway_runtime_user(bot_id, network)
            while True:
                data = _load_calendars(user_local)
                calendars = data.setdefault("calendars", [])
                path = _cal_config_path(user_local)
                print(f"\n  ── {bot_id.upper()} Calendar URLs ──────────────────────")
                print(f"  File: {path}")
                if not calendars:
                    print("  (none configured)")
                else:
                    for entry in calendars:
                        url = entry.get("url", "")
                        masked = url[:40] + "…" if len(url) > 40 else url
                        print(f"  [{entry.get('id','')}] {entry.get('name','')}  ({entry.get('type','')})")
                        print(f"    url: {masked}")
                print()
                print("  [a] Add   [r] Remove   [Enter] Back")
                sub = input("  Choice: ").strip().lower()
                if sub == "a":
                    name = input("  Name: ").strip()
                    if not name:
                        continue
                    cal_id = re.sub(r'[^a-z0-9\-]', '-', name.lower()).strip('-')
                    cal_id = re.sub(r'-+', '-', cal_id)
                    cal_id = input(f"  ID (default: {cal_id}): ").strip() or cal_id
                    if any(e.get("id") == cal_id for e in calendars):
                        if input(f"  '{cal_id}' exists. Overwrite? (y/N): ").strip().lower() != "y":
                            continue
                        calendars[:] = [e for e in calendars if e.get("id") != cal_id]
                    cal_type = input("  Type (default: ical_url): ").strip() or "ical_url"
                    print("  ⚠️  Secret URLs are like passwords.")
                    url = input("  URL: ").strip()
                    if not url:
                        continue
                    calendars.append({"id": cal_id, "name": name, "type": cal_type, "url": url})
                    _save_calendars(user_local, data)
                    print(f"  ✅ Saved '{name}'")
                elif sub == "r":
                    if not calendars:
                        print("  Nothing to remove.")
                        continue
                    for i, e in enumerate(calendars, 1):
                        print(f"    {i}. [{e.get('id','')}] {e.get('name','')}")
                    num = input("  Number to remove: ").strip()
                    try:
                        idx = int(num) - 1
                        entry = calendars[idx]
                        if input(f"  Remove '{entry.get('name','')}' ({entry.get('id','')})?  (y/N): ").strip().lower() == "y":
                            calendars.pop(idx)
                            _save_calendars(user_local, data)
                            print("  ✅ Removed")
                    except (ValueError, IndexError):
                        print("  Invalid number.")
                else:
                    break

        elif choice == "u":
            _menu_usage_loop(bot_id, network)

        elif choice == "v":
            _menu_version_loop(network)

        elif choice == "e":
            user_local = _gateway_runtime_user(bot_id, network)
            path = _exec_approvals_json(user_local)
            # Reuse existing interactive exec approvals loop
            _menu_exec_approvals_loop(bot_id, user_local, path)

        elif choice == "h":
            print(MENU_HELP)
            input("  Press Enter to continue...")

        elif choice == "g":
            if input(f"\n  Restart {bot_id} gateway? (y/N): ").strip().lower() == "y":
                _menu_restart_gateway(bot_id)

        elif choice == "b":
            bot_ids = _bot_ids(network)
            print(f"\n  Bots: {', '.join(bot_ids)} | all")
            new_bot = input("  Switch to: ").strip().lower()
            if new_bot == "all":
                _all_bots_loop(network)
                return
            elif new_bot in bot_ids:
                _single_bot_loop(new_bot, network)
                return
            else:
                print(f"  ❌ Unknown: {new_bot}")

        elif choice == "q":
            print("Bye.")
            sys.exit(0)

        else:
            print("  Unknown option.")


def _menu_exec_approvals_loop(bot_id: str, user: str, path: Path) -> None:
    while True:
        _show_exec_approvals(bot_id, path)
        print()
        print("  [s] Set security/ask   [a] Add pattern   [r] Remove pattern")
        print("  [q] Add common patterns  [t] Toggle autoAllowSkills  [Enter] Back")
        sub = input("  Choice: ").strip().lower()

        if sub == "s":
            data = _load_exec_approvals(path)
            sec = input(f"  security ({'/'.join(SECURITY_LEVELS)}): ").strip().lower()
            if sec not in SECURITY_LEVELS:
                print(f"  ❌ Choose from: {SECURITY_LEVELS}")
                continue
            ask = input(f"  ask ({'/'.join(ASK_MODES)}): ").strip().lower()
            if ask not in ASK_MODES:
                print("  ❌ Invalid.")
                continue
            fb = input(f"  askFallback ({'/'.join(ASK_FALLBACKS)}): ").strip().lower()
            if fb not in ASK_FALLBACKS:
                print("  ❌ Invalid.")
                continue
            for scope in [data.setdefault("defaults", {}),
                          data.setdefault("agents", {}).setdefault("main", {})]:
                scope["security"] = sec
                scope["ask"] = ask
                scope["askFallback"] = fb
            _save_exec_approvals(path, user, data)

        elif sub == "a":
            data = _load_exec_approvals(path)
            pattern = input("  Pattern: ").strip()
            if not pattern:
                continue
            comment = input("  Comment (optional): ").strip()
            entry = {"pattern": pattern}
            if comment:
                entry["comment"] = comment
            allowlist = data.setdefault("agents", {}).setdefault("main", {}).setdefault("allowlist", [])
            existing = {e.get("pattern", e) if isinstance(e, dict) else e for e in allowlist}
            if pattern in existing:
                print("  Already in allowlist.")
                continue
            allowlist.append(entry)
            _save_exec_approvals(path, user, data)
            print(f"  ✅ Added: {pattern}")

        elif sub == "r":
            data = _load_exec_approvals(path)
            allowlist = data.get("agents", {}).get("main", {}).get("allowlist", [])
            if not allowlist:
                print("  Nothing to remove.")
                continue
            for i, entry in enumerate(allowlist, 1):
                p = entry.get("pattern", entry) if isinstance(entry, dict) else entry
                c = entry.get("comment", "") if isinstance(entry, dict) else ""
                print(f"    {i:2}. {p}  {'# ' + c if c else ''}")
            num = input("  Number to remove: ").strip()
            try:
                idx = int(num) - 1
                removed = allowlist.pop(idx)
                p = removed.get("pattern", removed) if isinstance(removed, dict) else removed
                _save_exec_approvals(path, user, data)
                print(f"  ✅ Removed: {p}")
            except (ValueError, IndexError):
                print("  Invalid number.")

        elif sub == "q":
            data = _load_exec_approvals(path)
            print("\n  Common patterns:")
            for i, (pattern, label) in enumerate(COMMON_ALLOWLIST_PATTERNS, 1):
                print(f"    {i:2}. {label:38s} {pattern}")
            raw = input("  Numbers to add (space-separated, or 'all'): ").strip()
            if raw.lower() == "all":
                indices = list(range(len(COMMON_ALLOWLIST_PATTERNS)))
            else:
                try:
                    indices = [int(x) - 1 for x in raw.split()]
                except ValueError:
                    print("  Invalid input.")
                    continue
            allowlist = data.setdefault("agents", {}).setdefault("main", {}).setdefault("allowlist", [])
            existing = {e.get("pattern", e) if isinstance(e, dict) else e for e in allowlist}
            added = []
            for idx in indices:
                try:
                    pattern, label = COMMON_ALLOWLIST_PATTERNS[idx]
                    if pattern not in existing:
                        allowlist.append({"pattern": pattern, "comment": label})
                        existing.add(pattern)
                        added.append(label)
                except IndexError:
                    pass
            if added:
                _save_exec_approvals(path, user, data)
                print(f"  ✅ Added: {', '.join(added)}")
            else:
                print("  Nothing new to add.")

        elif sub == "t":
            data = _load_exec_approvals(path)
            main = data.setdefault("agents", {}).setdefault("main", {})
            current = main.get("autoAllowSkills", False)
            main["autoAllowSkills"] = not current
            _save_exec_approvals(path, user, data)
            print(f"  ✅ autoAllowSkills → {not current}")

        else:
            break


def _menu_usage_loop(bot_id: str, network: dict) -> None:
    scope = bot_id if bot_id != "all" else "all"
    bot_filter = None if bot_id == "all" else bot_id

    while True:
        print(f"\n  ── Usage Reports ({scope}) ──────────────────────────")
        print("    [1] Today      [2] Last 7 days   [3] Last 30 days")
        print("    [4] Custom     [5] By channel    [c] Collect turns   [x] Export CSV")
        print("    [b] Back")
        choice = input("  Choice: ").strip().lower()

        if choice == "b":
            break
        elif choice == "1":
            turns = _load_turns(network, days=1, bot_filter=bot_filter)
            _print_usage(turns, 1, bot_filter)
        elif choice == "2":
            turns = _load_turns(network, days=7, bot_filter=bot_filter)
            _print_usage(turns, 7, bot_filter)
        elif choice == "3":
            turns = _load_turns(network, days=30, bot_filter=bot_filter)
            _print_usage(turns, 30, bot_filter)
        elif choice == "4":
            raw = input("  Start date (YYYY-MM-DD): ").strip()
            try:
                start = datetime.strptime(raw, "%Y-%m-%d")
                end_raw = input("  End date (YYYY-MM-DD, Enter for today): ").strip()
                end = datetime.strptime(end_raw, "%Y-%m-%d") if end_raw else datetime.now()
                days = (end - start).days + 1
                turns = _load_turns(network, days=days, end_date=end, bot_filter=bot_filter)
                _print_usage(turns, days, bot_filter)
            except ValueError:
                print("  Invalid date format.")
        elif choice == "5":
            chan = input("  Channel (telegram/slack/discord/...): ").strip().lower()
            days_raw = input("  Days (default 7): ").strip()
            days = int(days_raw) if days_raw.isdigit() else 7
            turns = _load_turns(network, days=days, bot_filter=bot_filter)
            turns = [t for t in turns if t.get("channel") == chan]
            _print_usage(turns, days, bot_filter)
        elif choice == "c":
            _collect_turns(network, bot_filter)
            print("  Done.")
        elif choice == "x":
            days_raw = input("  Days to export (default 30): ").strip()
            days = int(days_raw) if days_raw.isdigit() else 30
            out = input("  Output file (default: /tmp/turns-export.csv): ").strip() or "/tmp/turns-export.csv"
            turns = _load_turns(network, days=days, bot_filter=bot_filter)
            turns.sort(key=lambda t: t.get("ts", ""))
            fields = ["ts", "instance", "channel", "user_id", "model", "source", "msg_id"]
            with open(out, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(turns)
            print(f"  ✅ Exported {len(turns)} turns to {out}")
        else:
            print("  Unknown option.")


def _all_bots_loop(network: dict) -> None:
    bot_ids = _bot_ids(network)
    print()
    print("━━━ ALL BOTS ━━━")

    while True:
        print()
        print("  [a] Add model to all     [r] Remove model from all")
        print("  [o] Set order for all    [k] Rotate key for all")
        print("  [g] Restart all gateways")
        print("  [i] Info for all         [l] Logs for all")
        print("  [x] Processes for all    [w] Google Workspace (all)")
        print("  [u] Usage (all)          [v] Version & upgrade")
        print("  [h] Help                 [b] Switch to single bot")
        print("  [q] Quit")
        print()
        choice = input("  Choice: ").strip().lower()

        if choice == "i":
            for bot_id in bot_ids:
                _menu_show_info(bot_id, network)

        elif choice == "l":
            n_str = input("  Lines per bot? (default 10): ").strip()
            n = int(n_str) if n_str.isdigit() else 10
            for bot_id in bot_ids:
                _menu_show_logs(bot_id, network, n)

        elif choice == "x":
            all_procs: dict = {}
            for bot_id in bot_ids:
                procs = _menu_show_process(bot_id, network)
                if procs:
                    all_procs[bot_id] = procs
            if all_procs:
                kill = input("\n  Kill a gateway? Enter bot name or Enter to skip: ").strip().lower()
                if kill in all_procs:
                    for line in all_procs[kill]:
                        pid = line.split()[1]
                        subprocess.run(["kill", "-9", pid])
                    print(f"  ✅ Killed {kill} — launchd will restart")

        elif choice == "a":
            new_model = input("  Model name: ").strip()
            if not new_model:
                continue
            alias = input("  Alias (Enter to skip): ").strip()
            for bot_id in bot_ids:
                user, cfg_path, _, cfg, _ = _menu_load_bot(bot_id, network)
                catalog = _get_catalog(cfg)
                catalog[new_model] = {"alias": alias} if alias else {}
                _set_catalog(cfg, catalog)
                _menu_save_cfg(cfg, cfg_path)
                print(f"  ✅ {bot_id}: added {new_model}")

        elif choice == "r":
            model = input("  Model name to remove: ").strip()
            if not model:
                continue
            for bot_id in bot_ids:
                user, cfg_path, _, cfg, _ = _menu_load_bot(bot_id, network)
                catalog      = _get_catalog(cfg)
                model_config = _get_model_config(cfg)
                primary      = model_config.get("primary", "")
                fallbacks    = model_config.get("fallbacks", [])
                if model == primary or model in fallbacks:
                    print(f"  ⚠️  {bot_id}: {model} is active — skipping")
                    continue
                if model in catalog:
                    catalog.pop(model)
                    _set_catalog(cfg, catalog)
                    _menu_save_cfg(cfg, cfg_path)
                    print(f"  ✅ {bot_id}: removed {model}")
                else:
                    print(f"  ─  {bot_id}: not in catalog")

        elif choice == "o":
            # Build unified model list
            unified: dict = {}
            for bot_id in bot_ids:
                _, _, _, cfg, _ = _menu_load_bot(bot_id, network)
                catalog = _get_catalog(cfg)
                mc = _get_model_config(cfg)
                active = [mc.get("primary", "")] + mc.get("fallbacks", [])
                for m in active:
                    if m and m not in unified:
                        unified[m] = catalog.get(m, {}).get("alias", "") if isinstance(catalog.get(m), dict) else ""
                for m, meta in catalog.items():
                    if m not in unified:
                        unified[m] = meta.get("alias", "") if isinstance(meta, dict) else ""
            all_unified = list(unified.keys())
            print("  Available models (union of all bot catalogs):")
            for i, m in enumerate(all_unified, 1):
                alias = unified[m]
                print(f"    {i:2}. {m}{('  [' + alias + ']') if alias else ''}")
            raw = input("  Order (space-separated numbers, first = primary): ").strip()
            if not raw:
                continue
            try:
                nums     = [int(x) for x in raw.split()]
                selected = [all_unified[n - 1] for n in nums]
            except (ValueError, IndexError) as e:
                print(f"  Invalid: {e}")
                continue
            for bot_id in bot_ids:
                user, cfg_path, _, cfg, _ = _menu_load_bot(bot_id, network)
                catalog      = _get_catalog(cfg)
                model_config = _get_model_config(cfg)
                model_config["primary"]   = selected[0]
                model_config["fallbacks"] = selected[1:]
                _set_model_config(cfg, model_config)
                added = []
                for m in selected:
                    if m not in catalog:
                        catalog[m] = {}
                        added.append(m)
                _set_catalog(cfg, catalog)
                _menu_save_cfg(cfg, cfg_path)
                added_str = f"  (auto-added: {', '.join(added)})" if added else ""
                print(f"  ✅ {bot_id}: primary={selected[0]}, fallbacks={selected[1:]}{added_str}")

        elif choice == "k":
            _menu_rotate_keys_all(bot_ids, network)

        elif choice == "w":
            for bot_id in bot_ids:
                account = _gws_account(bot_id, network)
                if account:
                    _menu_gws_single(bot_id, network)
                    break  # show one at a time then prompt
            sub = input("\n  [r] Reauth a bot  [Enter] Back: ").strip().lower()
            if sub == "r":
                gws_bots = [b for b in bot_ids if _gws_account(b, network)]
                print(f"  Bots with Google accounts: {', '.join(gws_bots)}")
                target = input("  Which bot? ").strip().lower()
                if target in bot_ids:
                    _menu_gws_single(target, network)

        elif choice == "u":
            _menu_usage_loop("all", network)

        elif choice == "v":
            _menu_version_loop(network)

        elif choice == "h":
            print(MENU_HELP)
            input("  Press Enter to continue...")

        elif choice == "g":
            if input("\n  Restart ALL gateways? (y/N): ").strip().lower() == "y":
                for bot_id in bot_ids:
                    _menu_restart_gateway(bot_id)

        elif choice == "b":
            print(f"\n  Bots: {', '.join(bot_ids)}")
            new_bot = input("  Switch to: ").strip().lower()
            if new_bot in bot_ids:
                _single_bot_loop(new_bot, network)
                return
            else:
                print(f"  ❌ Unknown: {new_bot}")

        elif choice == "q":
            print("Bye.")
            sys.exit(0)

        else:
            print("  Unknown option.")


def _menu_rotate_keys_all(bot_ids: list, network: dict) -> None:
    print("\n  Rotating keys for ALL bots. Enter to keep current.")
    # Build unified provider list
    all_catalogs: dict = {}
    all_profiles: dict = {}
    for bot_id in bot_ids:
        _, _, _, cfg, auth = _menu_load_bot(bot_id, network)
        all_catalogs.update(_get_catalog(cfg))
        all_profiles.update(auth.get("profiles", {}))

    new_ai_keys: dict = {}
    for provider, mode, description in _build_provider_list(all_catalogs, all_profiles):
        print(f"\n  {description}")
        new_key = input("    New key (Enter to skip): ").strip()
        if new_key:
            new_ai_keys[(provider, mode)] = new_key

    print("\n  Brave Search API Key")
    new_brave = input("    New key (Enter to skip): ").strip()
    print("\n  Telegram Bot Token")
    new_tg = input("    New token (Enter to skip): ").strip()
    print("\n  Slack Bot Token (xoxb-...)")
    new_slack_bot = input("    New token (Enter to skip): ").strip()
    print("\n  Slack App Token (xapp-...)")
    new_slack_app = input("    New token (Enter to skip): ").strip()

    if not new_ai_keys and not new_brave and not new_tg and not new_slack_bot and not new_slack_app:
        print("  No keys entered.")
        return

    for bot_id in bot_ids:
        user, cfg_path, auth_path, cfg, auth = _menu_load_bot(bot_id, network)
        profiles = auth.get("profiles", {})
        auth_changed = False

        for (provider, mode), new_key in new_ai_keys.items():
            field = "token" if mode == "token" else "key"
            name, profile, fld = _find_profile(profiles, provider, mode)
            if profile is not None:
                profile[fld] = new_key
                auth_changed = True
                print(f"  ✅ {bot_id}: updated {provider}/{mode}")
            else:
                pname = f"{provider}:default"
                if pname in profiles:
                    pname = f"{provider}:api"
                profiles[pname] = {"provider": provider, "type": mode, field: new_key}
                auth_changed = True
                print(f"  ✅ {bot_id}: created {provider}/{mode} profile")
        if auth_changed:
            _menu_save_auth(auth, auth_path)

        if new_brave:
            (cfg.setdefault("plugins", {}).setdefault("entries", {})
               .setdefault("brave", {}).setdefault("config", {})
               .setdefault("webSearch", {}))["apiKey"] = new_brave
            cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {}).pop("apiKey", None)
            cfg["tools"]["web"]["search"]["provider"] = "brave"
            _menu_save_cfg(cfg, cfg_path)
            print(f"  ✅ {bot_id}: Brave key updated")

        if new_tg:
            tg = cfg.get("channels", {}).get("telegram", {})
            if isinstance(tg, dict) and tg.get("enabled"):
                tg["botToken"] = new_tg
                cfg.setdefault("channels", {})["telegram"] = tg
                _menu_save_cfg(cfg, cfg_path)
                print(f"  ✅ {bot_id}: Telegram token updated")
            else:
                print(f"  ─  {bot_id}: Telegram not enabled, skipping")

        if new_slack_bot or new_slack_app:
            slack = cfg.get("channels", {}).get("slack", {})
            if isinstance(slack, dict) and slack.get("enabled"):
                if new_slack_bot:
                    slack["botToken"] = new_slack_bot
                if new_slack_app:
                    slack["appToken"] = new_slack_app
                cfg.setdefault("channels", {})["slack"] = slack
                _menu_save_cfg(cfg, cfg_path)
                print(f"  ✅ {bot_id}: Slack token(s) updated")
            else:
                print(f"  ─  {bot_id}: Slack not enabled, skipping")


# ── oc menu (and default entrypoint) ─────────────────────────────────────────

@menu_group.command("menu")
@click.pass_context
def oc_menu(ctx: click.Context) -> None:
    """Interactive single-letter menu — full admin interface (default)."""
    network_path: Path = ctx.obj.get("network_path", DEFAULT_NETWORK_CONFIG)
    network = _load_network_safe(network_path)

    if os.geteuid() != 0:
        print("❌ Run with sudo: sudo evolve-admin menu")
        sys.exit(1)

    # Restore terminal to sane state in case a previous run left it in raw/cbreak mode
    # (symptom: Enter shows ^M instead of submitting input)
    subprocess.run(["/bin/stty", "sane"], check=False)

    bot_ids = _bot_ids(network)
    if not bot_ids:
        print("❌ No bots found in network config. Run 'evolve-admin setup' first.")
        sys.exit(1)

    print("=" * 62)
    print("  Evolve Admin Menu")
    print("  Changes save automatically. Enter bot name or 'all'.")
    print("=" * 62)
    print()
    print(f"  Bots: {', '.join(bot_ids)} | all")
    target = input("  Which bot? ").strip().lower()

    if target == "all":
        _all_bots_loop(network)
    elif target in bot_ids:
        _single_bot_loop(target, network)
    else:
        print(f"❌ Unknown: {target}")
        sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_network_safe(network_path: Path) -> dict:
    try:
        return load_network(network_path)
    except Exception:
        return {}


# ── Backwards-compatible `oc` alias ──────────────────────────────────────────
# Operators have muscle memory for `evolve-admin oc ...`. Keep the path
# working but hide it from `--help` so `menu` is the one canonical name
# people discover. Both groups share the same `commands` dict (Python
# reference, not a copy), so subcommands stay in sync automatically.
oc_group = click.Group(
    name="oc",
    commands=menu_group.commands,
    callback=menu_group.callback,
    params=menu_group.params,
    invoke_without_command=True,
    hidden=True,
    help="(alias for `evolve-admin menu`)",
)
