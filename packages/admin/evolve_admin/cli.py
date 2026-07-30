"""
evolve-admin CLI — admin tool for the Evolve network.
Run with sudo for operations that write to other users' home directories.

Entry point: evolve-admin

Commands:
  setup               Interactive setup wizard — guided first-time configuration
  status              Show pod health summary
  deploy --bot <id>   Deploy/update Evolve on a bot
  deploy --all        Deploy/update all bots in network config
  upgrade             Version-aware upgrade: compare install.json, migrate, preserve data
  remove --bot <id>   Remove a bot from the pod
  setup-shared        Create/fix the shared directory
  serve               Start local admin web UI (127.0.0.1:5050)
  config show         Print current network config
  config set-primary  [DEPRECATED] Set which bot is primary — fixed at setup time now
  config set-alert    Configure Telegram alert target
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from platform_profile import get_profile
import sys
import time
from pathlib import Path
from typing import Any

# rich reads os.getcwd() on import (rich/__init__.py:
# `_IMPORT_CWD = os.path.abspath(os.getcwd())`). That raises
# PermissionError when the process's cwd is a directory the current
# user can't stat — most commonly seen when SSH lands in one user's
# home and then `sudo -u <other-user>` inherits that cwd. Bot-user
# home dirs (/Users/admin_bot/, /Users/security_bot/, etc.) are mode 700, so
# any `sudo -n -u evolve evolve-admin ...` from the admin's session
# crashes on import before main() runs.
#
# Fix: probe cwd before importing rich. If getcwd() fails, chdir to
# a world-stattable directory. Normal local runs skip the chdir.
try:
    os.getcwd()
except (FileNotFoundError, PermissionError):
    os.chdir("/")

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from .wizard import run_wizard
from .config import (
    DEFAULT_NETWORK_CONFIG,
    DEFAULT_SHARED_DIR,
    load_network,
    save_network,
    get_bot_user,
    is_reserved_account,
    bot_home as _bot_home,
    KNOWN_BOTS,
)
from .deploy import (
    deploy_bot, deploy_shared_dir,
    reinstall_evolve_admin, build_plugin, fix_plugin_permissions,
    ensure_plugin_config, install_oc_plugin, fix_shared_dir_permissions,
    restart_gateway, install_bot_gateway_plist, verify_plugin_live, install_staged_plists,
    inject_pod_conduct, repair_security_bot_config,
    read_install_json, write_install_json, EVOLVE_VERSION,
    record_bot_deploy, find_orphaned_plists, remove_orphaned_plists,
    get_bot_sync_status, install_evolve_infra_jobs,
    ensure_pod_perms,
    run_smoke_audit, SmokeAuditResult,
)
from .deploy_steps import _deploy_step, verify_gateway_loaded_new_plugin
from .runtime import get_launchd_scheduler, get_scheduler
from .status import network_status
from .ocadmin import menu_group, oc_group

console = Console()


_MAIN_EPILOG = """\
\b
Common starting points:
  evolve-admin setup --fresh   First-time install on a new Mac (run on the deploy box).
  evolve-admin connect         Open the admin UI from your laptop via SSH tunnel.
  evolve-admin menu            Interactive bot-config menu — no flag syntax to remember.
  evolve-admin status          One-screen pod health summary.

Full command reference is below. Most commands run without sudo; deploy /
setup / refresh-sudoers / restart-gateways require sudo on the deploy box.
"""


@click.group(epilog=_MAIN_EPILOG)
@click.option(
    "--network",
    "network_path",
    default=str(DEFAULT_NETWORK_CONFIG),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to network.json",
)
@click.pass_context
def main(ctx: click.Context, network_path: Path) -> None:
    """Evolve network admin — run with sudo for privileged operations."""
    from .telemetry import setup_logging
    setup_logging()
    ctx.ensure_object(dict)
    ctx.obj["network_path"] = network_path


# ── pair (admin-UI device auth) ─────────────────────────────────────────────────


def _auth_shared_dir(ctx: click.Context) -> Path:
    network_path: Path = ctx.obj["network_path"]
    shared_dir = Path("/Users/Shared/evolve")
    try:
        shared_dir = Path(load_network(network_path).get("sharedDir", shared_dir))
    except Exception:
        pass
    return shared_dir


@main.command()
@click.pass_context
def pair(ctx: click.Context) -> None:
    """Print a one-time code to pair a browser/device with the admin UI.

    Admin auth is ON BY DEFAULT (roadmap 2.6): the server enforces a paired
    device cookie unless the operator opted out (``evolve-admin auth disable``).
    This command mints the admin-auth key (if absent) and prints the current
    pairing code. Open the admin UI; it redirects to /pair; enter this code
    (valid a few minutes — re-run for a fresh one).

    **Run with sudo:** ``sudo evolve-admin pair``. The admin-auth key is owned
    by the ``evolve`` daemon user (mode 0600); minting/reading it and chowning
    it to ``evolve:staff`` both need root. Run as a normal user, this would
    leave the daemon unable to read the key — a lockout, not pairing.
    """
    import os as _os
    from .web import admin_auth

    shared_dir = _auth_shared_dir(ctx)
    key_path = admin_auth._key_path(shared_dir)
    # Guard the lockout: minting a fresh key (or reading the evolve-owned one
    # to compute the code) requires root. Refuse BEFORE creating a key the
    # daemon can't read.
    if _os.geteuid() != 0 and not _os.access(key_path, _os.R_OK):
        click.echo("Run this with sudo: `sudo evolve-admin pair`.")
        click.echo(
            "  The admin-auth key is owned by the evolve daemon (mode 0600); "
            "minting/reading it needs root. Running as a normal user would "
            "leave the daemon unable to read the key (a lockout)."
        )
        raise SystemExit(1)

    code = admin_auth.current_pairing_code(shared_dir)  # generates the key if absent
    # The admin server runs as the evolve user; make the key readable to it,
    # mirroring the signing-key setup. We are root here (guarded above), so the
    # chown succeeds; verify and fail loudly rather than leave a broken key.
    chown = subprocess.run(
        [get_profile().chown, "evolve:staff", str(key_path)],
        check=False, capture_output=True, text=True,
    )
    if chown.returncode != 0:
        click.echo(
            f"WARNING: could not chown the admin-auth key to evolve:staff "
            f"({chown.stderr.strip()}). The daemon may not be able to read it; "
            f"re-run with sudo."
        )

    if admin_auth.is_optout(shared_dir):
        click.echo("NOTE: auth is currently DISABLED (opt-out recorded). Run")
        click.echo("`evolve-admin auth enable` to enforce pairing.\n")
    click.echo(f"  Pairing code:  {code}\n")
    click.echo("Open the admin UI in your browser — it redirects to /pair. Enter")
    click.echo("this 6-digit code there. Valid a few minutes; re-run for a fresh one.")


# ── auth (enforce / opt-out) ────────────────────────────────────────────────────


@main.group()
def auth() -> None:
    """Manage admin-server device-pairing enforcement (roadmap 2.6)."""


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show whether the admin server enforces device pairing."""
    from .web import admin_auth

    shared_dir = _auth_shared_dir(ctx)
    if admin_auth.is_optout(shared_dir):
        click.echo("Admin auth: DISABLED (operator opt-out recorded).")
        click.echo(f"  marker: {admin_auth._optout_path(shared_dir)}")
        click.echo("  Re-enable with: evolve-admin auth enable")
    else:
        keyed = admin_auth._key_path(shared_dir).exists()
        click.echo("Admin auth: ENABLED (enforced by default).")
        click.echo(f"  paired key present: {'yes' if keyed else 'no — run `evolve-admin pair`'}")


@auth.command("disable")
@click.option("--accept-risk", required=True,
              help="Why you accept running the control plane open (recorded).")
@click.pass_context
def auth_disable(ctx: click.Context, accept_risk: str) -> None:
    """Record an explicit opt-out — the admin server stops enforcing pairing.

    This re-opens the control plane to any local process that can reach the
    loopback port. Only appropriate on a genuinely single-tenant, dedicated
    host; the acceptance reason is recorded in the marker file (and should be
    noted in docs/threat-model.md §6.1).
    """
    import getpass
    from .web import admin_auth

    shared_dir = _auth_shared_dir(ctx)
    admin_auth.record_optout(shared_dir, by=getpass.getuser(), reason=accept_risk)
    click.echo("Admin auth DISABLED (opt-out recorded).")
    click.echo(f"  marker: {admin_auth._optout_path(shared_dir)}")
    click.echo(f"  reason: {accept_risk}")


@auth.command("enable")
@click.pass_context
def auth_enable(ctx: click.Context) -> None:
    """Remove the opt-out marker — the admin server enforces pairing again."""
    from .web import admin_auth

    shared_dir = _auth_shared_dir(ctx)
    had = admin_auth.clear_optout(shared_dir)
    click.echo("Admin auth ENABLED (enforced)." if had
               else "Admin auth already enabled (no opt-out marker present).")
    if not admin_auth._key_path(shared_dir).exists():
        click.echo("Run `evolve-admin pair` to mint a pairing code.")


# ── status ────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--json", "json_output", is_flag=True, default=False,
              help="machine-readable JSON output (for finding evidence sections)")
@click.option("--category", "category", default=None,
              help="comma-separated list: network,process,subprocess,schema (default: all)")
def diagnose(json_output: bool, category: str | None) -> None:
    """Bundled investigation team_bot_a: probe network state, process inventory,
    subprocess introspection, schema sanity. Designed for incident response.

    See docs/spec-etr-diagnose-tool-2026-04-26.md.
    """
    from .diagnose import run_all, format_report, format_json
    cats = None
    if category:
        cats = [c.strip() for c in category.split(",") if c.strip()]
    result = run_all(categories=cats)
    if json_output:
        print(format_json(result))
    else:
        print(format_report(result))
    # Exit code reflects verdict: 0 healthy, 1 anomaly, 2 unhealthy, 3 mixed/unknown
    overall = result["verdict"]["overall"]
    sys.exit({"healthy": 0, "anomaly": 1, "unhealthy": 2}.get(overall, 3))


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show pod health summary."""
    network_path: Path = ctx.obj["network_path"]

    with console.status("Querying network..."):
        data = network_status(network_path)

    shared_dir = Path("/Users/Shared/evolve")
    install_info = read_install_json(shared_dir)
    try:
        _net = load_network(network_path)
    except Exception:
        _net = {}
    bot_sync = get_bot_sync_status(_net, install_info)

    console.print(Panel(
        f"[bold]Network:[/] {data['network_id']}  "
        f"[bold]Primary:[/] {data['primary']}  "
        f"[bold]Pending proposals:[/] {data['pending_proposals']}  "
        f"[bold]Evolve:[/] v{EVOLVE_VERSION}",
        title="⚡ Evolve Pod Status",
    ))

    t = Table(show_header=True, header_style="bold blue")
    t.add_column("Bot", style="bold")
    t.add_column("Role")
    t.add_column("Port")
    t.add_column("Live")
    t.add_column("Last metric")
    t.add_column("Score")
    t.add_column("Maint ratio")
    t.add_column("Evolve ver")

    for bot_id, info in data["bots"].items():
        live = "[green]✓[/]" if info["live"] else "[red]✗[/]"
        metric = info.get("last_metric") or {}
        score = str(metric.get("score", "—"))
        maint = metric.get("maintenance_ratio_7d")
        maint_str = f"{maint*100:.0f}%" if maint is not None else "—"
        maint_color = "red" if maint and maint > 0.35 else "yellow" if maint and maint > 0.2 else "green"

        sync = bot_sync.get(bot_id, {})
        deployed_ver = sync.get("deployed_version")
        if deployed_ver is None:
            ver_cell = "[dim]unknown[/]"
        elif sync.get("synced"):
            ver_cell = f"[green]v{deployed_ver}[/]"
        else:
            ver_cell = f"[yellow]v{deployed_ver} ⚠[/]"

        t.add_row(
            bot_id,
            info["role"],
            str(info["port"] or "—"),
            live,
            info.get("last_metric_date") or "—",
            score,
            f"[{maint_color}]{maint_str}[/]",
            ver_cell,
        )

    console.print(t)

    # Warn if any bots are out of sync
    out_of_sync = [
        bid for bid, s in bot_sync.items()
        if not s.get("synced")
    ]
    if out_of_sync:
        console.print(
            f"\n[yellow]⚠  {len(out_of_sync)} bot(s) not on v{EVOLVE_VERSION}: "
            f"{', '.join(out_of_sync)}[/]"
        )
        console.print("[dim]  Run: sudo evolve-admin upgrade   or   sudo evolve-admin deploy --all[/]")


# ── deploy ────────────────────────────────────────────────────────────────────

@main.group(invoke_without_command=True)
@click.option("--network", "network_path", default=None, type=click.Path(path_type=Path), help="Override network.json path")
@click.option("--fresh", is_flag=True, default=False, help="Full fresh-machine setup: create accounts, install OC, deploy Evolve")
@click.option("--non-interactive", "non_interactive", is_flag=True, default=False, help="Use defaults everywhere (for scripted/CI deploys)")
@click.option("--bots-manifest", "bots_manifest", default=None, type=click.Path(exists=True, path_type=Path),
              help="JSON file listing the bots to register. Required with --fresh --non-interactive. "
                   "Format: {\"bots\": [{\"bot_id\": \"admin_bot\", \"port\": 19000, \"user\": \"...\", \"role\": \"member\", \"multi_user\": false}, ...]}")
@click.option("--platform", "platform_opt", default=None, type=click.Choice(["macos", "linux"]),
              help="Optional explicit platform override for the fresh wizard. Both macOS "
                   "and Linux hosts auto-detect — you never need this flag. Pass it only "
                   "to fail loudly if the host isn't the platform you expect.")
@click.pass_context
def setup(ctx: click.Context, network_path: Path | None, fresh: bool, non_interactive: bool, bots_manifest: Path | None, platform_opt: str | None) -> None:
    """Interactive setup wizard — guided first-time network configuration.

    Subcommands:
      evolve-user    Create the evolve macOS system user, OC config, and shared dirs

    Use --fresh on a new machine to go from zero to a running pod in one command.
    It will create user accounts, install OpenClaw, configure channels,
    deploy Evolve, and start the gateways.

    Without --fresh, runs the standard wizard for an existing OC installation.

    Pod membership is explicit: when running --fresh --non-interactive, you
    must pass --bots-manifest with the exact list of bots to register. The
    wizard refuses to guess from filesystem state.
    """
    if ctx.invoked_subcommand is not None:
        return
    net = network_path or ctx.obj.get("network_path") or DEFAULT_NETWORK_CONFIG

    if fresh:
        from .setup_wizard import run_fresh_wizard
        run_fresh_wizard(
            non_interactive=non_interactive,
            network_path=net,
            bots_manifest=bots_manifest,
            platform_opt=platform_opt,
        )
    else:
        # Auto-detect: if no network config and no OC installs exist, suggest --fresh
        from .wizard import find_oc_candidates
        if not net.exists() and not find_oc_candidates():
            console.print("[yellow]No existing OC installations found.[/]")
            console.print("For a fresh machine, run: [bold]evolve-admin setup --fresh[/]")
            console.print("Or install OpenClaw manually first, then re-run without --fresh.\n")
            if not non_interactive:
                import click as _click
                if _click.confirm("  Run full fresh-machine setup now?", default=True):
                    from .setup_wizard import run_fresh_wizard
                    run_fresh_wizard(
                        non_interactive=non_interactive,
                        network_path=net,
                        bots_manifest=bots_manifest,
                        platform_opt=platform_opt,
                    )
                    return
        run_wizard(net)


# ── setup evolve-user ─────────────────────────────────────────────────────────

# (Removed 2026-04-27: _SUDOERS_CONTENT literal that had drifted to be
# missing sections 12+ relative to setup_wizard._render_evolve_sudoers.
# Step 13 below now imports the renderer directly so the two paths share
# one source of truth and can't drift again.)

_SOUL_MD = """\
# SOUL.md — Evolve Bot

You are Evolve — an AI infrastructure manager for an OpenClaw pod.

Your purpose:
- Keep the pod running (monitor, heal, alert)
- Observe quality across all bots
- Generate and validate improvement proposals
- Present findings to Pod_admin for approval

Rules:
- You have access to bot config files. Use this ONLY for config management.
- You do NOT read conversation transcripts, memory files, or personal data.
- You do NOT act autonomously on production bots — human approval required.
- When in doubt: read only, never write without approval.
- You are infrastructure, not a user-facing assistant.
"""

_AGENTS_MD = """\
# AGENTS.md — Evolve Bot

Read SOUL.md first. Always.

## Memory
- memory/YYYY-MM-DD.md — operational logs
- memory/tasks.md — pending tasks

## What you do
- Read bot configs from /Users/Shared/evolve/ and bot config paths
- Generate analysis and proposals
- Never write to production bot configs without approval
"""

# No model is seeded here — this bootstrap (setup evolve-user) runs before
# any LLM credential exists, so presuming a provider (the old
# "anthropic/claude-haiku-3-5" literal — a stale model id, too) violates
# docs/principle-llm-provider-agnostic.md. deploy.ensure_plugin_config seeds
# agents.defaults.model from the bot's tier config / credentialed provider
# on the first deploy after credentials land.
_OC_JSON_EVOLVE = {
    "agents": {"defaults": {}},
    "plugins": {
        "entries": [],
    },
    "channels": {},
}


@setup.command("evolve-user")
@click.option("--uid", default=None, type=int, help="Override UID (auto-detected from 500–599 range if not set)")
def setup_evolve_user(uid: int | None) -> None:
    """Create the evolve macOS user, configure OpenClaw, set up shared dirs, and generate sudoers.

    Must be run with sudo (requires root).
    """
    import secrets

    from .runtime.isolation import IsolationError, get_isolation

    # macOS-only bootstrap (dscl UIDs, staff group, createhomedir) — on Linux
    # the fresh setup wizard owns evolve-user creation; fail fast, not mid-run.
    if get_profile().name != "macos":
        console.print("[red]setup evolve-user is macOS-only — on Linux run: sudo evolve-admin setup --fresh[/]")
        sys.exit(1)

    if os.geteuid() != 0:
        console.print("[red]This command must be run with sudo:[/]")
        console.print("  sudo evolve-admin setup evolve-user")
        sys.exit(1)

    console.print("[bold]Setting up evolve macOS user[/]\n")
    iso = get_isolation()

    # ── Step 1: Check if evolve user already exists ────────────────────────────
    ok, fail = _deploy_step("Checking if evolve user exists...")
    user_exists = iso.user_exists("evolve")
    if user_exists:
        ok("already exists — skipping user creation")
    else:
        ok("not found, will create")

        # ── Step 2: Find next available UID ───────────────────────────────────
        ok, fail = _deploy_step("Finding next available UID...")
        if uid is None:
            uids = iso.used_uids()
            if not uids:
                # dscl -list always lists at least root; an empty set means
                # the probe itself failed — keep the historical fail-loud.
                fail("dscl -list failed: no user records returned")
            bot_range = [u for u in uids if 500 <= u <= 599]
            uid = (max(bot_range) + 1) if bot_range else 500
        ok(f"UID = {uid}")

        # ── Step 3: Create macOS user via dscl ────────────────────────────────
        ok, fail = _deploy_step("Creating evolve user via dscl...")
        try:
            # create_home=False — Step 6 below builds /Users/evolve itself
            # (mkdir + chown + chmod + best-effort createhomedir), matching
            # the historical flow which ran those steps even for an
            # already-existing user.
            iso.create_user(
                "evolve", uid,
                real_name="Evolve Infrastructure", create_home=False,
            )
            ok()
        except IsolationError as e:
            fail(str(e))

        # ── Step 4: Set random password ────────────────────────────────────────
        ok, fail = _deploy_step("Setting random password...")
        password = secrets.token_hex(16)
        if iso.set_password("evolve", password):
            ok()
            console.print(f"\n  [yellow bold]Password (save this):[/] {password}\n")
        else:
            fail("dscl -passwd /Users/evolve failed")

        # ── Step 5: Add to wheel group ─────────────────────────────────────────
        ok, fail = _deploy_step("Adding evolve to wheel group...")
        if iso.add_to_group("evolve", "wheel"):
            ok()
        else:
            ok("skipped (already member or group unavailable)")

    # ── Step 6: Create home directory ─────────────────────────────────────────
    ok, fail = _deploy_step("Creating home directory...")
    try:
        Path("/Users/evolve").mkdir(parents=True, exist_ok=True)
        subprocess.run(["chown", "-R", "evolve:staff", "/Users/evolve"], check=True, capture_output=True)
        subprocess.run(["chmod", "755", "/Users/evolve"], check=True, capture_output=True)
        # createhomedir populates skeleton files (non-fatal if it fails)
        subprocess.run(["createhomedir", "-c", "-u", "evolve"], capture_output=True)
        ok()
    except subprocess.CalledProcessError as e:
        fail(str(e))

    # ── Step 7: Create OpenClaw directory structure ────────────────────────────
    ok, fail = _deploy_step("Setting up .openclaw directory structure...")
    try:
        for d in [
            "/Users/evolve/.openclaw/workspace/memory",
            "/Users/evolve/.openclaw/logs",
            "/Users/evolve/.openclaw/agents/main/agent",
        ]:
            Path(d).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["chown", "-R", "evolve:staff", "/Users/evolve/.openclaw"],
            check=True, capture_output=True,
        )
        ok()
    except subprocess.CalledProcessError as e:
        fail(str(e))

    # ── Step 8: Write minimal openclaw.json ───────────────────────────────────
    ok, fail = _deploy_step("Writing openclaw.json...")
    try:
        oc_json_path = Path("/Users/evolve/.openclaw/openclaw.json")
        oc_json_path.write_text(json.dumps(_OC_JSON_EVOLVE, indent=2))
        subprocess.run(["chown", "evolve:staff", str(oc_json_path)], check=True, capture_output=True)
        # 0600: the primary bot's openclaw.json holds the gateway token. evolve
        # owns it (line above), so the gateway + admin server read it as owner.
        subprocess.run(["chmod", "600", str(oc_json_path)], check=True, capture_output=True)
        ok()
    except (OSError, subprocess.CalledProcessError) as e:
        fail(str(e))

    # ── Step 9: Write SOUL.md ─────────────────────────────────────────────────
    ok, fail = _deploy_step("Writing SOUL.md...")
    try:
        soul_path = Path("/Users/evolve/.openclaw/workspace/SOUL.md")
        soul_path.write_text(_SOUL_MD)
        subprocess.run(["chown", "evolve:staff", str(soul_path)], check=True, capture_output=True)
        ok()
    except (OSError, subprocess.CalledProcessError) as e:
        fail(str(e))

    # ── Step 10: Write AGENTS.md ──────────────────────────────────────────────
    ok, fail = _deploy_step("Writing AGENTS.md...")
    try:
        agents_path = Path("/Users/evolve/.openclaw/workspace/AGENTS.md")
        agents_path.write_text(_AGENTS_MD)
        subprocess.run(["chown", "evolve:staff", str(agents_path)], check=True, capture_output=True)
        ok()
    except (OSError, subprocess.CalledProcessError) as e:
        fail(str(e))

    # ── Step 11: Create /Users/Shared/evolve/ directory structure ─────────────
    ok, fail = _deploy_step("Creating /Users/Shared/evolve/ dirs...")
    try:
        for d in [
            "/Users/Shared/evolve/annotations",
            "/Users/Shared/evolve/metrics",
            "/Users/Shared/evolve/proposals/pending",
            "/Users/Shared/evolve/proposals/approved",
            "/Users/Shared/evolve/proposals/rejected",
            "/Users/Shared/evolve/alerts",
            "/Users/Shared/evolve/sandbox/baseline",
            "/Users/Shared/evolve/sandbox/current",
            "/Users/Shared/evolve/sandbox/results",
            "/Users/Shared/evolve/logs",
        ]:
            Path(d).mkdir(parents=True, exist_ok=True)
        audit_log = Path("/Users/Shared/evolve/audit-log.jsonl")
        if not audit_log.exists():
            audit_log.touch()
        subprocess.run(
            ["chown", "-R", "evolve:staff", "/Users/Shared/evolve"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["chmod", "-R", "755", "/Users/Shared/evolve"],
            check=True, capture_output=True,
        )
        # Re-assert special modes stripped by the -R 755 pass above.
        # This step may be re-run on existing installations (e.g. to update
        # sudoers), so we must restore 1777/777 on dirs that were already set.
        for _d, _mode in [
            ("/Users/Shared/evolve", "1777"),
            ("/Users/Shared/evolve/metrics", "1777"),
            ("/Users/Shared/evolve/annotations", "1777"),
        ]:
            if Path(_d).exists():
                subprocess.run(["chmod", _mode, _d], capture_output=True)
        for _turns in Path("/Users/Shared/evolve").glob("*/turns"):
            if _turns.is_dir():
                subprocess.run(["chmod", "1777", str(_turns)], capture_output=True)
        ok()
    except (OSError, subprocess.CalledProcessError) as e:
        fail(str(e))

    # ── Step 12: Generate HMAC signing key ────────────────────────────────────
    ok, fail = _deploy_step("Generating HMAC signing key...")
    try:
        import secrets as _secrets
        keystore_dir = Path("/Users/Shared/evolve/keystore")
        keystore_dir.mkdir(parents=True, exist_ok=True)
        signing_key_path = keystore_dir / "evolve-signing.key"
        if signing_key_path.exists():
            ok("already exists — skipping")
        else:
            signing_key_path.write_text(_secrets.token_hex(32))
            subprocess.run(["chmod", "600", str(signing_key_path)], check=True, capture_output=True)
            subprocess.run(["chown", "evolve:staff", str(signing_key_path)], check=True, capture_output=True)
            subprocess.run(["chmod", "700", str(keystore_dir)], check=True, capture_output=True)
            subprocess.run(["chown", "evolve:staff", str(keystore_dir)], check=True, capture_output=True)
            ok()
    except (OSError, subprocess.CalledProcessError) as e:
        fail(str(e))

    # ── Step 13: Install sudoers file ─────────────────────────────────────────
    # Sudoers content is rendered by setup_wizard._render_evolve_sudoers
    # — single source of truth shared with `evolve-admin refresh-sudoers`.
    # Pre-2026-04-27, this step had its own _SUDOERS_CONTENT literal which
    # had drifted to be missing sections 12+ (workspace AGENTS.md, POD_CONDUCT,
    # etc.). Importing the renderer eliminates the drift class.
    ok, fail = _deploy_step("Installing /etc/sudoers.d/evolve...")
    try:
        from .setup_wizard import _render_evolve_sudoers
        sudoers_content = _render_evolve_sudoers()
        if sudoers_content is None:
            fail("openclaw CLI not found — install openclaw first, then re-run setup-evolve-user")
        else:
            import tempfile as _tempfile
            with _tempfile.NamedTemporaryFile(mode="w", suffix=".sudoers", delete=False) as _tmp:
                _tmp.write(sudoers_content)
                _tmp_path = _tmp.name
            _r = subprocess.run(["sudo", "/usr/sbin/visudo", "-c", "-f", _tmp_path], capture_output=True, text=True)  # sudo-grant: root-only — setup-evolve-user CLI step, run as root (bare cp/chmod/chown below confirm root)
            if _r.returncode != 0:
                Path(_tmp_path).unlink(missing_ok=True)
                fail(f"visudo validation failed: {_r.stderr.strip()}")
            else:
                _dst = Path("/etc/sudoers.d/evolve")
                subprocess.run(["cp", _tmp_path, str(_dst)], check=True, capture_output=True)
                subprocess.run(["chmod", "440", str(_dst)], capture_output=True)
                subprocess.run(["chown", "root:wheel", str(_dst)], capture_output=True)
                Path(_tmp_path).unlink(missing_ok=True)
                ok()
    except (OSError, subprocess.CalledProcessError) as e:
        fail(str(e))

    # ── Summary ────────────────────────────────────────────────────────────────
    console.print("\n[bold green]evolve user setup complete.[/]\n")
    console.print("\n[bold]Remaining manual steps:[/]")
    console.print("  1. Set Anthropic API key in auth-profiles.json:")
    console.print("       /Users/evolve/.openclaw/agents/main/agent/auth-profiles.json")
    console.print("  2. Install sudoers file (commands above)")
    console.print("  3. Install the primary gateway plist /Library/LaunchDaemons/ai.openclaw.<primary>-gateway.plist (ai.openclaw.evo-gateway on a fresh pod)")
    console.print("  4. Verify: id evolve")


def _full_deploy(bot_id: str, network_path: Path, network: dict, dry_run: bool) -> SmokeAuditResult:
    """Deploy the OC plugin to a single bot and ensure its gateway is running.

    Steps:
      1. Build TypeScript plugin
      2. Fix plugin permissions
      3. Inject plugin config
      4. Install OC plugin on bot
      5. Inject POD_CONDUCT.md
      6. Register bot in network.json + set up workspace (deploy_bot)
      7. Install gateway LaunchDaemon if not present, then restart
      7.5. Verify the gateway actually bounced onto the freshly-installed
         plugin (force a restart if the byte-identical-unit install skipped its
         bounce, then probe /evolve/status) — evolve-vps darwin, #3362.
      8. Smoke audit — re-run the OC security audit with the time-gate bypassed
         so deploy fails loudly on critical findings introduced by the deploy
         itself (evolve-ops/evolve#1088). Result is returned to the caller for
         the exit-code decision.

    Infra jobs (cron, admin server) belong on the evolve user — use
    'evolve-admin setup evolve-user' for those.
    """
    bots_cfg = network.get("bots", {})
    bot_cfg = bots_cfg.get(bot_id, {})
    port = bot_cfg.get("port")

    if dry_run:
        console.print(f"  [dim][dry-run] would deploy plugin to {bot_id}[/]")
        return SmokeAuditResult()

    # Captured BEFORE any gateway bounce so Step 6.5 can prove the running
    # gateway PID is NEWER than the deploy (i.e. it re-loaded the new plugin).
    deploy_began_at = time.time()

    # ── Pre-flight: Slack-doctor check (non-blocking) ────────────────────────
    # Surfaces silent-failure conditions the operator would otherwise hit
    # post-deploy (team_bot_a's bug 1 + bug 2 pattern). Phase 1 is non-blocking —
    # we report findings but always proceed. Phase 2's policy layer
    # promotes FAIL findings to a hard gate.
    _run_slack_doctor_preflight(bot_id, network)

    # ── Step 1: Build TypeScript plugin ───────────────────────────────────────
    ok, fail = _deploy_step("🔨 Building plugin...")
    try:
        build_plugin()
        ok()
    except Exception as e:
        fail(str(e))

    # ── Step 2: Fix plugin file permissions ───────────────────────────────────
    ok, fail = _deploy_step("🔒 Fixing plugin permissions...")
    try:
        fix_plugin_permissions()
        ok()
    except Exception as e:
        fail(str(e))

    # ── Step 3: Inject plugin config + install OC plugin ─────────────────────
    ok, fail = _deploy_step(f"Injecting plugin config for {bot_id}...")
    try:
        ensure_plugin_config(bot_id, network)
        ok()
    except Exception as e:
        fail(str(e))

    ok, fail = _deploy_step(f"Installing plugin on {bot_id}...")
    try:
        install_oc_plugin(bot_id, port=port, network=network)
        ok()
    except Exception as e:
        fail(str(e))

    # ── Step 5: Register bot in network.json + workspace setup ────────────────
    # (includes POD_CONDUCT.md injection + AGENTS.md maintenance)
    ok, fail = _deploy_step(f"Setting up workspace for {bot_id}...")
    try:
        role = bot_cfg.get("role") or "member"
        result = deploy_bot(bot_id, role=role, port=port, network_path=network_path)
        if result.success:
            ok()
        else:
            ok(f"done (warnings: {'; '.join(result.errors[:2])})")
    except Exception as e:
        # Non-fatal — workspace setup failure should not block gateway start
        ok(f"done (workspace warning: {e})")

    # ── Step 6: Install / refresh gateway LaunchDaemon plist ──────────────────
    # Always re-install when we have a port: install_bot_gateway_plist() is
    # fully idempotent (writes /tmp + sudo cp + chown + chmod + bootout +
    # bootstrap) and re-installing is the only way to pick up content drift —
    # new env vars (HOME, TMPDIR, NODE_EXTRA_CA_CERTS, OPENCLAW_*), changes
    # to ProgramArguments, or a node-binary path move. The previous
    # `if not system_plist.exists()` gate caused the same predicate-drift
    # bug class as PR #312: existing pods kept stale plists forever and
    # restart_gateway() only kickstarted what was already there. When no
    # port is known we fall back to a plain restart of whatever's installed.
    system_plist = Path(f"/Library/LaunchDaemons/ai.openclaw.{bot_id}-gateway.plist")
    if port:
        ok, fail = _deploy_step(f"Installing gateway plist for {bot_id}...")
        # Resolve the bot's macOS username — may differ from bot_id (e.g.
        # bot_id != macOS user). Without this, the gateway plist
        # gets the wrong UserName + log paths and oc_cli probes fail.
        bot_user = get_bot_user(bot_id, network)
        gateway_ok, gateway_detail = install_bot_gateway_plist(
            bot_id, port, user=bot_user,
        )
        if gateway_ok:
            ok()
        else:
            # Surface the actual stderr (sudoers gap, port collision,
            # whatever) instead of always pointing at manual bootstrap.
            fail(
                f"gateway plist install failed: {gateway_detail}. "
                f"Manual recovery (only if the file made it onto disk): "
                f"sudo launchctl bootstrap system {system_plist}"
            )
    else:
        # No port to template — just restart whatever's already installed.
        ok, fail = _deploy_step(f"Restarting gateway for {bot_id}...")
        try:
            restart_gateway(bot_id)
            ok()
        except Exception as e:
            fail(str(e))

    # ── Step 6.5: Guarantee + verify the gateway bounced onto the new plugin ──
    # The install above bounces the daemon only as a side effect; on a
    # plugin-ONLY change (byte-identical unit) that can silently no-op and leave
    # the OLD plugin serving (evolve-vps darwin, #3362). Force + verify here.
    verify_gateway_loaded_new_plugin(bot_id, port, deploy_began_at)

    # ── Step 7: Record deploy version ─────────────────────────────────────────
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    try:
        record_bot_deploy(bot_id, shared_dir)
    except Exception:
        pass  # non-fatal — version tracking should not block deploy

    # ── Step 7.5: Refresh Slack workspace directory (if Slack-enabled) ─────
    # Catches the "operator just turned Slack on, now needs the directory"
    # case the user called out. Skipped silently for non-Slack bots and
    # when no token is available yet. Best-effort: never blocks deploy.
    _refresh_slack_directory_if_enabled(bot_id, network, shared_dir)

    # ── Step 8: Post-deploy smoke audit ───────────────────────────────────────
    # Bypasses the 23h time-gate so deploy can fail loudly the moment criticals
    # appear. See evolve-ops/evolve#1088 — phantom criticals can run unchecked for
    # an arbitrary window because nothing else asserts cleanliness at deploy.
    audit_result = _run_post_deploy_smoke_audit(bot_id, shared_dir)
    return audit_result


# Max detail lines printed per finding in the inline deploy summary. Audit
# findings like `security.trust_model.multi_user_heuristic` can carry 8+
# lines of explanation, which is great for the standalone audit report but
# floods the deploy log when several bots trip the same heuristic on a
# `deploy --all`. The cap balances "operator sees what fired and why" against
# "deploy log scrolls off the terminal". Tail-line tells them where the full
# detail lives.
_AUDIT_DETAIL_LINES = 3


def _render_finding_detail(detail: str) -> None:
    """Print a finding's detail field, capped to ``_AUDIT_DETAIL_LINES``.

    Empty / whitespace-only detail prints nothing. When the cap clips lines
    we append a hint line showing how many were elided and where to find
    them. Rich prefix is applied here (not in callers) so criticals and
    warns get identical treatment.
    """
    if not detail or not detail.strip():
        return
    lines = detail.splitlines()
    head = lines[:_AUDIT_DETAIL_LINES]
    for ln in head:
        console.print(f"      [dim]{ln}[/]")
    elided = len(lines) - len(head)
    if elided > 0:
        # Don't promise a follow-up command we can't actually run — the audit
        # findings live only in the in-process audit_oc_security result; the
        # operator's options for full detail are the Signal store (where the
        # finding gets mirrored under {shared}/signals/) or the audit module
        # source. Just say how many lines we dropped.
        console.print(f"      [dim](…{elided} more line(s) elided)[/]")


def _run_post_deploy_smoke_audit(bot_id: str, shared_dir: Path) -> SmokeAuditResult:
    """Run the smoke audit and render its outcome to the console.

    Returns the raw result so the caller can decide on the exit code. Criticals
    get a bold red block — the operator should not be able to miss them in
    scrollback. Warns get a one-line summary with a pointer at the audit tool.
    Audit-run failures are themselves surfaced (they're news, not silent).
    """
    console.print(f"\n[bold]→ Smoke audit: {bot_id}[/]")
    result = run_smoke_audit(bot_id, shared_dir)

    if result.error:
        console.print(
            f"  [red bold]DEPLOY SMOKE AUDIT FAILED TO RUN[/] — {result.error}\n"
            f"  [dim]The audit itself errored; treat this as a regression — fix "
            f"before relying on the next scheduled run.[/]"
        )
        return result

    crits = result.critical_findings
    warns = result.warn_findings
    if crits:
        console.print(
            f"  [red bold]DEPLOY SMOKE AUDIT: {len(crits)} CRITICAL "
            f"finding(s) — fix these before continuing.[/]"
        )
        for f in crits:
            console.print(f"    [red]🔴 {f.message}[/]")
            _render_finding_detail(getattr(f, "detail", "") or "")
    if warns:
        # Render warns inline (same shape as criticals above). The old code
        # pointed the operator at `evolve-admin audit run --bot <bot>`, but
        # that command doesn't exist — the only way to see the findings was
        # to read packages/analyzer/audit.py. Cheaper to just print them.
        console.print(f"  [yellow]{len(warns)} warn finding(s):[/]")
        for f in warns:
            console.print(f"    [yellow]⚠ {f.message}[/]")
            _render_finding_detail(getattr(f, "detail", "") or "")
    if not crits and not warns:
        console.print(f"  [green]✓ smoke audit: clean (0 critical, 0 warn)[/]")
    elif not crits:
        console.print(f"  [green]✓ smoke audit: clean (0 critical, {len(warns)} warn)[/]")
    return result


_SLACK_DOCTOR_SKIP_ENV = "EVOLVE_SKIP_SLACK_DOCTOR"


def _refresh_slack_directory_if_enabled(
    bot_id: str, network: dict, shared_dir: Path,
) -> None:
    """Refresh the Slack workspace directory if the bot has Slack on.

    Called from ``_full_deploy`` after a successful deploy. Skipped:
    - For bots with no Slack block (channels.slack absent in openclaw.json)
    - For bots with ``enabled: false`` (Slack is administratively off)
    - When no bot token can be resolved (probably a fresh setup that
      hasn't completed Slack OAuth — the next deploy will retry)

    Best-effort: any failure prints a short note and returns. Never
    blocks the deploy.
    """
    try:
        from .integrations.slack.directory import refresh_workspace_directory
        from .integrations.slack.oc_config import load_openclaw_view
    except Exception:
        return
    try:
        home = _bot_home(bot_id, network)
        view = load_openclaw_view(bot_id, home)
    except Exception:
        return
    # Gate: bot must have a slack block AND not be disabled AND have a token.
    if view.slack_enabled is False:
        return
    if not view.bot_token:
        # No Slack at all, or token not yet configured. Skip silently.
        return
    try:
        result = refresh_workspace_directory(
            bot_id, bot_token=view.bot_token, shared_dir=shared_dir,
        )
    except Exception as exc:
        console.print(f"  [dim]slack-directory refresh skipped: {exc}[/]")
        return
    if not result.ok:
        console.print(f"  [dim]slack-directory refresh: {result.error}[/]")
        return
    console.print(
        f"  [dim]slack-directory: {result.user_count} user(s) "
        f"({result.team_name or result.team_id or 'workspace'}) cached for {bot_id}[/]"
    )


def _run_slack_doctor_preflight(bot_id: str, network: dict) -> None:
    """Pre-deploy Slack-doctor pass.

    Blocking on FAIL findings when a policy exists for the bot (Phase 2);
    non-blocking otherwise (Phase 1 transitional behavior). The
    ``EVOLVE_SKIP_SLACK_DOCTOR=1`` env var is the explicit escape hatch
    for emergencies (e.g. Slack down, bot needs to redeploy regardless).

    Best-effort: any exception in setup is logged and swallowed — the
    doctor must never block a deploy because of an infrastructure
    failure in the doctor itself.
    """
    if os.environ.get(_SLACK_DOCTOR_SKIP_ENV) == "1":
        console.print(
            f"  [dim]slack-doctor skipped ({_SLACK_DOCTOR_SKIP_ENV}=1)[/]"
        )
        return
    try:
        from .integrations.slack.doctor import run_doctor
    except Exception as exc:
        console.print(f"  [dim]slack-doctor skipped: import failed ({exc})[/]")
        return
    try:
        home = _bot_home(bot_id, network)
        shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
        result = run_doctor(bot_id, bot_home=home, shared_dir=shared_dir)
    except Exception as exc:
        console.print(f"  [dim]slack-doctor skipped: {exc}[/]")
        return
    fails = result.by_severity("fail")
    warns = result.by_severity("warn")
    infos = result.by_severity("info")
    if not fails and not warns and not infos:
        return
    console.print("  [bold]slack-doctor pre-flight:[/]")
    for f in fails:
        console.print(f"    [red]✗ {f.code}[/] {f.title}")
    for f in warns:
        console.print(f"    [yellow]⚠ {f.code}[/] {f.title}")
    # Infos surface dead-config and mid-setup states (SLK012 dormant
    # slack block, SLK007 token not yet installed). PR #1697 made the
    # doctor short-circuit after SLK012 so the dormant-block case now
    # produces only one info finding — without surfacing infos here it
    # would be invisible to the operator, and the dead config in
    # openclaw.json would just sit there indefinitely.
    for f in infos:
        console.print(f"    [dim]• {f.code} {f.title}[/]")

    # Phase 2: block on FAIL when a policy exists. Phase 1 (no policy)
    # stays non-blocking — we don't break existing deploys.
    if fails:
        try:
            from .integrations.slack.policy import policy_path
            policy_exists = policy_path(
                Path(network.get("sharedDir") or "/Users/Shared/evolve"), bot_id,
            ).exists()
        except Exception:
            policy_exists = False
        if policy_exists:
            console.print(
                f"  [red bold]Deploy blocked by {len(fails)} FAIL finding(s).[/] "
                f"Fix the issues above or set "
                f"[bold]{_SLACK_DOCTOR_SKIP_ENV}=1[/] to override "
                f"(emergency only)."
            )
            sys.exit(1)
        console.print(
            "  [dim]No policy file — Phase 1 non-blocking mode. "
            f"Run [bold]evolve-admin slack-policy init {bot_id}[/][dim] to "
            "adopt Phase 2 (blocking) once findings are clean.[/]"
        )


@main.command("install-infra-jobs")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be done without doing it")
@click.option(
    "--with-opik",
    is_flag=True,
    default=False,
    help="Also install the self-hosted Opik observability companion "
    "(requires Docker; runs as a launchd-managed docker-compose stack).",
)
@click.pass_context
def install_infra_jobs(ctx: click.Context, dry_run: bool, with_opik: bool) -> None:
    """Install Evolve infrastructure launchd jobs on the evolve user.

    Must be run with sudo (requires root). Installs cron-style launchd plists
    for the evolve user: analyzer, reporter, task runner, weekly review, etc.

    With ``--with-opik``, also installs the self-hosted Opik observability
    server (Apache-2.0; runs at http://localhost:5173). Requires Docker
    to be installed and reachable. See v1.5 sprint Opik integration notes.

    Typically run once after 'setup evolve-user'.
    """
    if os.geteuid() != 0:
        console.print("[red]This command must be run with sudo:[/]")
        console.print("  sudo evolve-admin install-infra-jobs")
        sys.exit(1)

    evolve_dir = Path("/Users/evolve")
    result = install_evolve_infra_jobs(evolve_dir, dry_run=dry_run)
    for line in result.steps:
        console.print(line)
    if not result.success:
        sys.exit(1)

    if with_opik:
        from .deploy import install_opik_companion
        opik_result = install_opik_companion(evolve_dir, dry_run=dry_run)
        for line in opik_result.steps:
            console.print(line)
        if not opik_result.success:
            console.print(
                "[yellow]Opik companion install reported failures; "
                "Evolve will fall back to the JSONL observability backend.[/]"
            )


# ── features ───────────────────────────────────────────────────────────────
#
# Read/write the install.json feature-gating layer. See
# docs/spec-upstream-issue-watcher-2026-05-22.md and
# packages/analyzer/install_profile.py.
#
# Power features (currently: upstream_issues_watcher) are off-by-default on
# household installs. Operators who want to opt in either set the profile
# globally ("set-profile developer") or flip a single feature ("set <name> on").


def _features_helpers():
    """Lazy import of the analyzer-side gating helpers.

    Kept lazy so the whole CLI doesn't crash at module load if the import
    goes sideways — only the features CLI subcommands need this, and they
    fail with a clear message instead.
    """
    from install_profile import (  # type: ignore[import-not-found]
        DEFAULT_PROFILE, PROFILE_DEFAULTS, VALID_PROFILES,
        get_feature_config, get_feature_profile, is_feature_enabled,
    )
    return {
        "DEFAULT_PROFILE": DEFAULT_PROFILE,
        "PROFILE_DEFAULTS": PROFILE_DEFAULTS,
        "VALID_PROFILES": VALID_PROFILES,
        "get_feature_config": get_feature_config,
        "get_feature_profile": get_feature_profile,
        "is_feature_enabled": is_feature_enabled,
    }


_INSTALL_JSON_PATH = Path("/Users/Shared/evolve/install.json")


def _read_install_json_safe() -> dict:
    """Read install.json; return ``{}`` on missing/malformed. Mirrors the
    permissive read in install_profile but kept local so the CLI doesn't
    pull analyzer for a simple JSON read."""
    if not _INSTALL_JSON_PATH.exists():
        return {}
    try:
        return json.loads(_INSTALL_JSON_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_install_json_safe(data: dict) -> None:
    """Atomic write of install.json, with /tmp staging + sudo fallback for
    the case where the running user doesn't own the file directly."""
    import tempfile, shutil, subprocess
    _INSTALL_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-install-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        try:
            shutil.copy2(tmp, _INSTALL_JSON_PATH)
            os.chmod(_INSTALL_JSON_PATH, 0o644)
        except PermissionError:
            subprocess.run(
                ["sudo", "/bin/cp", tmp, str(_INSTALL_JSON_PATH)],  # sudo-grant: root-only — this CLI writer runs as operator root; the evolve DAEMON writes install.json via deploy.write_install_json (sudoers §10a)
                check=True, capture_output=True,
            )
            subprocess.run(
                ["sudo", "/bin/chmod", "644", str(_INSTALL_JSON_PATH)],  # sudo-grant: root-only — operator-root CLI writer (see cp above; daemon path is deploy §10a)
                check=False, capture_output=True,
            )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@main.group("features")
def features_group() -> None:
    """Manage install.json feature profile and per-feature flags.

    Resolution: explicit per-feature ``enabled`` always wins over the
    profile default. See ``docs/spec-upstream-issue-watcher-2026-05-22.md``.
    """
    pass


@features_group.command("list")
def features_list_cmd() -> None:
    """Show resolved profile + per-feature enabled/disabled state.

    Reports what every catalogued power feature would actually do on this
    install, taking into account both the profile default and any explicit
    overrides written to install.json::features.
    """
    try:
        helpers = _features_helpers()
    except Exception as exc:
        console.print(f"[red]Could not load install_profile: {exc}[/]")
        sys.exit(1)
    profile = helpers["get_feature_profile"](_INSTALL_JSON_PATH)
    console.print(f"[bold]feature_profile:[/] {profile}")
    catalogued = sorted(
        {name for names in helpers["PROFILE_DEFAULTS"].values() for name in names}
    )
    if not catalogued:
        console.print("[dim](no power features catalogued in PROFILE_DEFAULTS)[/]")
        return
    data = _read_install_json_safe()
    overrides = (data.get("features") or {}) if isinstance(data, dict) else {}
    console.print()
    console.print("[bold]features:[/]")
    for name in catalogued:
        enabled = helpers["is_feature_enabled"](name, _INSTALL_JSON_PATH)
        marker = "[green]on[/]" if enabled else "[dim]off[/]"
        entry = overrides.get(name)
        if isinstance(entry, dict) and "enabled" in entry:
            source = f"explicit ({entry['enabled']!r})"
        elif name in helpers["PROFILE_DEFAULTS"].get(profile, frozenset()):
            source = f"profile default ({profile})"
        else:
            source = "off by default"
        console.print(f"  {name:38s} {marker:8s}  {source}")


@features_group.command("set")
@click.argument("feature_name")
@click.argument("state", type=click.Choice(("on", "off"), case_sensitive=False))
def features_set_cmd(feature_name: str, state: str) -> None:
    """Write an explicit enabled flag for FEATURE_NAME to install.json.

    Explicit flags always win over the profile default. To clear an
    override and fall back to the profile, edit install.json by hand and
    remove the entry — there's no 'unset' here on purpose, to keep the
    CLI surface small. Most operators will only ever flip one feature.
    """
    enabled = state.lower() == "on"
    data = _read_install_json_safe()
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    entry = features.get(feature_name) if isinstance(features.get(feature_name), dict) else {}
    entry["enabled"] = enabled
    features[feature_name] = entry
    data["features"] = features
    try:
        _write_install_json_safe(data)
    except Exception as exc:
        console.print(f"[red]Could not write install.json: {exc}[/]")
        sys.exit(1)
    console.print(
        f"[green]✓[/] features.{feature_name}.enabled = {enabled}"
    )
    console.print(
        "[dim]Run 'sudo evolve-admin install-infra-jobs' to apply launchd "
        "changes if this feature owns a daemon.[/]"
    )


@features_group.command("set-profile")
@click.argument("profile", type=click.Choice(("standard", "developer", "minimal"),
                                              case_sensitive=False))
def features_set_profile_cmd(profile: str) -> None:
    """Set install.json::feature_profile.

    Per-feature explicit overrides still win — switching profiles only
    changes the defaults for features that don't have an explicit flag.
    Use 'features list' afterward to see what actually resolved.
    """
    data = _read_install_json_safe()
    data["feature_profile"] = profile.lower()
    try:
        _write_install_json_safe(data)
    except Exception as exc:
        console.print(f"[red]Could not write install.json: {exc}[/]")
        sys.exit(1)
    console.print(f"[green]✓[/] feature_profile = {profile.lower()}")
    console.print(
        "[dim]Run 'sudo evolve-admin install-infra-jobs' to deploy/remove "
        "launchd jobs gated by this profile.[/]"
    )


# ── enable-https / disable-https ──────────────────────────────────────────────
#
# Phase 4.1.b of the PWA Phase 0 HTTPS-on-LAN sub-spec
# (docs/spec-pwa-phase0-https-2026-05-18.md). Standalone CLI surfaces
# for setting up / tearing down Tailscale-served HTTPS in front of the
# admin UI. Wizard integration is 4.1.c (separate PR).

@main.command("enable-https")
@click.pass_context
def enable_https_cmd(ctx: click.Context) -> None:
    """Enable Tailscale-served HTTPS on the admin UI.

    Runs `tailscale serve --bg --https=443 http://127.0.0.1:5050` and
    rewrites adminBaseUrl in network.json to the tailnet HTTPS URL.
    Idempotent — re-running on a pod that's already on HTTPS is a no-op.
    On verification failure, network.json is rolled back and the serve
    proxy is cleared.

    See docs/spec-pwa-phase0-https-2026-05-18.md.
    """
    from . import https_setup
    network_path: Path = ctx.obj["network_path"]
    try:
        result = https_setup.enable_https(network_path=network_path)
    except https_setup.HttpsSetupError as exc:
        console.print(f"[red]✗ {exc}[/]")
        sys.exit(exc.exit_code or 1)
    for line in result.messages:
        console.print(f"[green]✓[/] {line}" if result.changed else f"[dim]· {line}[/]")
    if result.changed:
        console.print(f"\n[bold green]Pod now serving HTTPS at {result.url}[/]")
    else:
        console.print(f"[dim]Pod already on HTTPS at {result.url}[/]")


@main.command("disable-https")
@click.pass_context
def disable_https_cmd(ctx: click.Context) -> None:
    """Disable Tailscale-served HTTPS — revert the admin UI to HTTP.

    Runs `tailscale serve --https=443 off` and rewrites adminBaseUrl
    back to the derived http://<host>:5050 default. Idempotent —
    re-running on an HTTP pod is a no-op.

    See docs/spec-pwa-phase0-https-2026-05-18.md §3.6.
    """
    from . import https_setup
    network_path: Path = ctx.obj["network_path"]
    try:
        result = https_setup.disable_https(network_path=network_path)
    except https_setup.HttpsSetupError as exc:
        console.print(f"[red]✗ {exc}[/]")
        sys.exit(exc.exit_code or 1)
    for line in result.messages:
        console.print(f"[green]✓[/] {line}" if result.changed else f"[dim]· {line}[/]")
    if result.changed:
        console.print(f"\n[bold green]Pod reverted to HTTP at {result.url}[/]")
    else:
        console.print(f"[dim]Pod already on HTTP at {result.url}[/]")

from .google_workspace_setup import google_workspace_setup_cmd as _gws_cmd; main.add_command(_gws_cmd)  # noqa: E401,E402,E702
@main.group("slack-policy")
def slack_policy_group() -> None:
    """Manage per-bot Slack policy files (Phase 2 of spec-slack-policy)."""
    pass


@slack_policy_group.command("show")
@click.argument("bot_id", metavar="BOT")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def slack_policy_show(ctx: click.Context, bot_id: str, as_json: bool) -> None:
    """Print the bot's slack-policy.json (or report it's missing)."""
    from .integrations.slack.policy import load_policy, to_dict_redacted

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
    try:
        policy = load_policy(shared_dir, bot_id)
    except ValueError as exc:
        console.print(f"[red]✗ policy file is malformed:[/] {exc}")
        sys.exit(1)
    if policy is None:
        console.print(f"[yellow]No policy file for {bot_id}.[/] "
                      f"Run [bold]evolve-admin slack-policy init {bot_id}[/].")
        sys.exit(2)
    if as_json:
        # Use the redacted view — unconsumed self-enrollment codes
        # are credentials and must not be re-surfaced after issuance.
        import json as _json
        console.print_json(_json.dumps(to_dict_redacted(policy)))
        return
    console.print(f"[bold]{bot_id}[/] · workspace [cyan]{policy.workspace.team_name or policy.workspace.team_id or '?'}[/]")
    console.print(f"  DM allowlist: {policy.access.dm_allowlist.mode}")
    if policy.access.dm_allowlist.mode == "explicit":
        console.print(f"    users: {', '.join(policy.access.dm_allowlist.user_ids) or '(none)'}")
    console.print(f"  Channels ({len(policy.channels.entries)}):")
    for entry in policy.channels.entries:
        label = f"#{entry.channel_name}" if entry.channel_name else entry.channel_id
        mention = "@-mention only" if entry.require_mention else "listens to all"
        console.print(f"    {entry.channel_id} ({label}) — {mention}")
    console.print(f"  New-channel default: {policy.channels.default_for_new.behavior}")
    console.print(f"  visibleReplies: {policy.messaging.visible_replies_default}")
    if policy.last_reconciled_at:
        console.print(f"  [dim]last reconciled: {policy.last_reconciled_at}[/]")


@slack_policy_group.command("init")
@click.argument("bot_id", metavar="BOT")
@click.option("--force", is_flag=True, default=False,
              help="Synthesize even if doctor reports FAIL findings.")
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_context
def slack_policy_init(
    ctx: click.Context, bot_id: str, force: bool, dry_run: bool,
) -> None:
    """Bootstrap a slack-policy.json from the bot's current openclaw.json.

    Refuses to synthesize from an openclaw.json with FAIL findings unless
    --force is set — otherwise the policy would re-render the bug.
    """
    from .integrations.slack.doctor import run_doctor
    from .integrations.slack.policy import policy_path, save_policy
    from .integrations.slack.writer import synthesize_policy_from_openclaw

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
    home = _bot_home(bot_id, network)

    existing = policy_path(shared_dir, bot_id)
    if existing.exists() and not force:
        console.print(
            f"[yellow]Policy already exists at {existing}.[/] "
            f"Use --force to overwrite."
        )
        sys.exit(2)

    # Gate on doctor findings before we synthesize.
    pre_result = run_doctor(bot_id, bot_home=home)
    fails = pre_result.by_severity("fail")
    if fails and not force:
        console.print(
            f"[red]Refusing to init policy: {len(fails)} FAIL finding(s) "
            f"in openclaw.json would be encoded into the policy.[/]"
        )
        for f in fails:
            console.print(f"  [red]✗ {f.code}[/] {f.title}")
        console.print(
            "[dim]Fix with [bold]evolve-admin slack-doctor "
            f"{bot_id} --fix[/][dim] (for SLK001 rekeying) or hand-edit, "
            "then re-run init. Or pass --force to encode the broken state.[/]"
        )
        sys.exit(1)

    policy, err = synthesize_policy_from_openclaw(bot_id=bot_id, bot_home=home)
    if err or policy is None:
        console.print(f"[red]Could not synthesize policy:[/] {err}")
        sys.exit(1)

    if dry_run:
        from .integrations.slack.policy import to_dict_redacted
        import json as _json
        console.print(f"[dim](dry-run — would write {policy_path(shared_dir, bot_id)})[/]")
        console.print_json(_json.dumps(to_dict_redacted(policy)))
        return
    try:
        path = save_policy(shared_dir, policy)
    except ValueError as exc:
        console.print(f"[red]✗ synthesized policy failed validation:[/] {exc}")
        sys.exit(1)
    console.print(f"[green]✓ wrote {path}[/]")


@slack_policy_group.command("apply")
@click.argument("bot_id", metavar="BOT")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show the diff but don't write openclaw.json.")
@click.option("--force", is_flag=True, default=False,
              help="Apply even if the doctor reports FAIL findings (override the spec §4 gate).")
@click.pass_context
def slack_policy_apply(
    ctx: click.Context, bot_id: str, dry_run: bool, force: bool,
) -> None:
    """Render the policy into the bot's openclaw.json.

    Per spec §4 ("pre-render validation … load-bearing gate"), this
    runs the doctor against the target bot before writing and refuses
    on any FAIL finding. ``--force`` overrides for emergency rollbacks
    where the operator knows better than the doctor (rare).
    """
    from .integrations.slack.doctor import run_doctor
    from .integrations.slack.policy import load_policy
    from .integrations.slack.writer import (
        is_render_up_to_date,
        merge_policy_into_openclaw,
        render_to_openclaw_json,
    )

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
    home = _bot_home(bot_id, network)

    try:
        policy = load_policy(shared_dir, bot_id)
    except ValueError as exc:
        console.print(f"[red]✗ policy is malformed:[/] {exc}")
        sys.exit(1)
    if policy is None:
        console.print(
            f"[yellow]No policy for {bot_id}.[/] "
            f"Run [bold]evolve-admin slack-policy init {bot_id}[/] first."
        )
        sys.exit(2)

    # Doctor gate per spec §4. Skip on --dry-run (operator is just
    # inspecting), and skip if --force (operator is overriding).
    if not dry_run and not force:
        doctor_result = run_doctor(bot_id, bot_home=home, shared_dir=shared_dir)
        fails = doctor_result.by_severity("fail")
        if fails:
            console.print(
                f"[red bold]Refusing to apply: {len(fails)} FAIL finding(s).[/]"
            )
            for f in fails:
                console.print(f"  [red]✗ {f.code}[/] {f.title}")
            console.print(
                "[dim]Fix the issues above or pass [bold]--force[/][dim] to override.[/]"
            )
            sys.exit(1)

    if dry_run:
        target = home / ".openclaw" / "openclaw.json"
        try:
            import json as _json
            existing = _json.loads(target.read_text())
        except FileNotFoundError:
            console.print(f"[red]✗ no openclaw.json at {target}[/]")
            sys.exit(1)
        merged, added, updated, removed = merge_policy_into_openclaw(
            existing=existing, policy=policy,
        )
        if merged == existing:
            console.print(f"[green]✓ {bot_id}: openclaw.json already matches policy.[/]")
            return
        console.print(f"[bold]{bot_id}[/] diff:")
        for cid in added:
            console.print(f"  [green]+ {cid}[/]")
        for cid in updated:
            console.print(f"  [yellow]~ {cid}[/]")
        for cid in removed:
            console.print(f"  [red]- {cid}[/]")
        return

    result = render_to_openclaw_json(bot_id=bot_id, bot_home=home, policy=policy)
    if not result.ok:
        console.print(f"[red]✗ {bot_id}:[/] {result.write_error}")
        for err in result.pre_validate_errors:
            console.print(f"    [red]{err}[/]")
        sys.exit(1)
    if not result.written:
        console.print(f"[green]✓ {bot_id}: already up to date.[/]")
        return
    console.print(f"[green]✓ {bot_id}: openclaw.json updated[/]")
    for cid in result.added_channel_ids:
        console.print(f"    [green]+ {cid}[/]")
    for cid in result.updated_channel_ids:
        console.print(f"    [yellow]~ {cid}[/]")
    for cid in result.removed_channel_ids:
        console.print(f"    [red]- {cid}[/]")


@main.command("slack-directory")
@click.argument("bot_id", metavar="BOT")
@click.option("--refresh", is_flag=True, default=False,
              help="Pull users.list from Slack and rewrite the directory file.")
@click.option("--show", "show_flag", is_flag=True, default=False,
              help="Print the current directory in a readable table.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output raw directory JSON instead of the table.")
@click.pass_context
def slack_directory_cmd(
    ctx: click.Context,
    bot_id: str,
    refresh: bool,
    show_flag: bool,
    as_json: bool,
) -> None:
    """Slack workspace identity directory for one bot.

    The directory maps every workspace user's Slack ID, legacy
    username, display name, real name, and (when scoped) email. It's
    injected into the bot's session prompt at session_start so the
    bot doesn't confuse aliases of the same person.

    Examples:
      evolve-admin slack-directory team_bot_a --refresh
      evolve-admin slack-directory team_bot_a --show
      evolve-admin slack-directory team_bot_a --json
    """
    from .integrations.slack.directory import (
        directory_age_hours,
        directory_path,
        load_directory,
        refresh_workspace_directory,
        render_directory_markdown,
    )
    from .integrations.slack.oc_config import load_openclaw_view

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    bots_cfg = network.get("bots", {})
    if bot_id not in bots_cfg:
        console.print(f"[red]Unknown bot: {bot_id}[/]")
        sys.exit(1)
    shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
    home = _bot_home(bot_id, network)

    if refresh:
        view = load_openclaw_view(bot_id, home)
        if not view.bot_token:
            console.print(
                f"[yellow]⚠ {bot_id}: no Slack bot token found. Configure "
                f"`channels.slack.botToken` in openclaw.json (or "
                f"SLACK_BOT_TOKEN in the workspace .env) before refreshing.[/]"
            )
            sys.exit(2)
        with console.status(f"Pulling users.list for {bot_id}..."):
            result = refresh_workspace_directory(
                bot_id, bot_token=view.bot_token, shared_dir=shared_dir,
            )
        if not result.ok:
            console.print(f"[red]✗ refresh failed:[/] {result.error}")
            sys.exit(1)
        console.print(
            f"[green]✓ wrote {result.saved_path}[/] · "
            f"{result.user_count} user(s) · workspace [cyan]"
            f"{result.team_name or result.team_id or '?'}[/]"
        )
        if not result.users_read_email_scope:
            console.print(
                "[dim]hint: the bot lacks `users:read.email` — the email "
                "column will be empty. Add the scope in the Slack app "
                "dashboard for a richer directory.[/]"
            )

    try:
        directory = load_directory(shared_dir, bot_id)
    except ValueError as exc:
        console.print(f"[red]✗ directory file is malformed:[/] {exc}")
        sys.exit(1)
    if directory is None:
        console.print(
            f"[yellow]No directory file for {bot_id}.[/] "
            f"Run [bold]evolve-admin slack-directory {bot_id} --refresh[/]."
        )
        sys.exit(2)

    if as_json:
        # Re-read the raw file so we don't drop fields the loader
        # didn't surface yet.
        try:
            text = directory_path(shared_dir, bot_id).read_text()
            console.print_json(text)
        except OSError as exc:
            console.print(f"[red]read failed:[/] {exc}")
            sys.exit(1)
        return

    # Default (no flag) and --show both render the same view.
    _render_slack_directory(directory)


def _render_slack_directory(directory: "Any") -> None:
    """Pretty-print the directory as a table."""
    from rich.table import Table
    from .integrations.slack.directory import directory_age_hours, is_stale

    age = directory_age_hours(directory)
    age_str = f"{age:.1f}h ago" if age is not None else "unknown"
    stale_marker = " [red](stale)[/]" if is_stale(directory) else ""
    workspace = directory.team_name or directory.team_id or "?"
    console.print(
        f"\n[bold]{directory.bot_id}[/] · workspace [cyan]{workspace}[/] · "
        f"{directory.user_count} user(s) · refreshed [dim]{age_str}[/]{stale_marker}"
    )
    if not directory.users_read_email_scope:
        console.print(
            "  [dim]users:read.email not granted — email column omitted[/]"
        )
    has_email = directory.users_read_email_scope

    t = Table(show_lines=False, header_style="dim", row_styles=["", "dim"])
    t.add_column("Slack ID")
    t.add_column("name")
    t.add_column("display_name")
    t.add_column("real_name")
    if has_email:
        t.add_column("email")
    t.add_column("role")
    for u in directory.users:
        row = [
            u.id,
            u.name or "—",
            u.display_name or "—",
            u.real_name or "—",
        ]
        if has_email:
            row.append(u.email or "—")
        row.append(u.role_label())
        t.add_row(*row)
    console.print(t)


@main.command("slack-doctor")
@click.argument("bot_id", required=False, default=None, metavar="BOT")
@click.option("--all", "all_bots", is_flag=True, default=False,
              help="Run the doctor on every bot in the pod.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON report (for use by the probe).")
@click.option("--fix", "do_fix", is_flag=True, default=False,
              help="Rewrite name-keyed channel entries to Slack IDs (SLK001).")
@click.pass_context
def slack_doctor(
    ctx: click.Context,
    bot_id: str | None,
    all_bots: bool,
    as_json: bool,
    do_fix: bool,
) -> None:
    """Validate a bot's Slack config against the live workspace.

    Catches the silent-failure modes from
    docs/spec-slack-policy-2026-05-13.md (name-keyed channels under
    allowlist, missing visibleReplies default, bot not a member of a
    listed channel, etc.). Exit code is 0 on no FAIL findings, 1 if any
    FAIL fired.
    """
    from .integrations.slack.doctor import (
        DoctorResult,
        rewrite_openclaw_json_channel_keys,
        run_doctor,
    )

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    bots_cfg = network.get("bots", {})

    if all_bots and bot_id:
        raise click.UsageError("Pass --all OR a single BOT, not both.")
    if not all_bots and not bot_id:
        raise click.UsageError("Specify BOT or pass --all.")

    targets = list(bots_cfg.keys()) if all_bots else [bot_id]
    targets = [t for t in targets if t]
    if not targets:
        console.print("[yellow]No bots to scan.[/]")
        return

    reports: dict[str, DoctorResult] = {}
    for target in targets:
        try:
            home = _bot_home(target, network)
        except Exception as exc:
            console.print(f"[red]✗ {target}: could not resolve home dir ({exc})[/]")
            continue
        result = run_doctor(target, bot_home=home)
        if do_fix:
            _apply_slack_doctor_fixes(target, home, result)
            # Re-run to surface the post-fix state.
            result = run_doctor(target, bot_home=home)
        reports[target] = result

    if as_json:
        import json as _json
        payload = {
            bid: {
                "bot_id": r.bot_id,
                "bot_token_source": r.bot_token_source,
                "findings": [
                    {
                        "code": f.code, "severity": f.severity,
                        "title": f.title, "detail": f.detail,
                        "channel_id": f.channel_id, "user_id": f.user_id,
                        "fixable": f.fixable,
                    }
                    for f in r.findings
                ],
                "fixes_applied": r.fixes_applied,
            }
            for bid, r in reports.items()
        }
        console.print_json(_json.dumps(payload))
    else:
        _render_slack_doctor_reports(reports)

    if any(r.has_fail() for r in reports.values()):
        sys.exit(1)


def _apply_slack_doctor_fixes(
    bot_id: str,
    bot_home: Path,
    result: "Any",
) -> None:
    """Resolve name→ID for SLK001 findings using the doctor's member-list output."""
    from .integrations.slack.doctor import rewrite_openclaw_json_channel_keys
    from .integrations.slack.oc_config import load_openclaw_view

    has_name_keyed = any(
        f.code == "SLK001" and f.fixable for f in result.findings
    )
    if not has_name_keyed:
        return

    # Build name → ID from the bot's actual member channels. Slack
    # channel names lack a leading `#`; some operators write `"foo"`
    # and some `"#foo"`. Normalize both sides.
    name_to_id: dict[str, str] = {}
    for ch in (result.bot_member_channels or []):
        cid = ch.get("id")
        name = ch.get("name")
        if isinstance(cid, str) and isinstance(name, str):
            name_to_id[name] = cid

    if not name_to_id:
        console.print(
            f"[yellow]⚠ {bot_id}: --fix had nothing to map — "
            f"auth.test + users.conversations must succeed first.[/]"
        )
        return

    # Any name-keyed entry whose stripped name matches a member channel
    # is a rewrite candidate. Skip entries whose key is already a member-
    # channel ID — those aren't bug 1.
    view = load_openclaw_view(bot_id, bot_home)
    plan: dict[str, str] = {}
    for entry in view.channel_entries:
        if entry.key in name_to_id.values():
            continue
        stripped = entry.key.lstrip("#")
        if stripped in name_to_id:
            plan[entry.key] = name_to_id[stripped]

    if not plan:
        console.print(
            f"[yellow]⚠ {bot_id}: --fix could not resolve any name-keyed "
            f"entries (channel names in config don't match channels the "
            f"bot is a member of).[/]"
        )
        return

    rewritten, err = rewrite_openclaw_json_channel_keys(
        bot_home=bot_home, view=view, name_to_id=plan,
    )
    if err:
        console.print(f"[red]✗ {bot_id}: --fix failed: {err}[/]")
        return
    for name in rewritten:
        console.print(
            f"  [green]✓[/] {bot_id}: rekeyed channels.\"{name}\" "
            f"→ channels.\"{plan[name]}\""
        )
        result.fixes_applied.append(name)


def _render_slack_doctor_reports(reports: dict) -> None:
    """Pretty-print findings; matches the existing health command style."""
    sev_color = {"fail": "red", "warn": "yellow", "info": "cyan"}
    sev_marker = {"fail": "✗", "warn": "⚠", "info": "ℹ"}
    for bot_id, r in reports.items():
        _print_slack_doctor_header(bot_id, r)
        if not r.findings:
            console.print(f"  [green]✓ no findings[/]")
            continue
        for f in r.findings:
            color = sev_color.get(f.severity, "white")
            marker = sev_marker.get(f.severity, "•")
            console.print(f"  [{color}]{marker} {f.code} ({f.severity})[/] {f.title}")
            for line in f.detail.splitlines():
                console.print(f"      [dim]{line}[/]")
        if r.fixes_applied:
            console.print(f"  [green]applied {len(r.fixes_applied)} fix(es)[/]")


def _print_slack_doctor_header(bot_id: str, r: "Any") -> None:
    """Print the full Slack-config status summary above findings.

    Plex-test focus: the operator should be able to answer "is my bot
    actually doing what I think it's doing?" from this section alone,
    without reading scope names or JSON.
    """
    workspace = ""
    if isinstance(r.auth_test, dict):
        workspace = r.auth_test.get("team") or r.auth_test.get("team_id") or ""
    header = f"[bold]{bot_id}[/]"
    if workspace:
        header += f" · workspace [cyan]{workspace}[/]"
    if r.bot_token_source:
        header += f" · token [dim]{r.bot_token_source}[/]"
    console.print(f"\n{header}")

    # ── Integration state line ────────────────────────────────────────
    bits: list[str] = []
    if r.slack_enabled is False:
        bits.append("[red bold]DISABLED[/] (enabled=false)")
    elif r.slack_enabled is True:
        bits.append("[green]enabled[/]")
    if r.transport_mode:
        bits.append(f"mode={r.transport_mode}")
    if r.group_policy:
        bits.append(f"groupPolicy={r.group_policy}")
    if r.dm_policy:
        bits.append(f"dmPolicy={r.dm_policy}")
    if r.transport_mode == "http" and not r.has_signing_secret:
        bits.append("[yellow]signingSecret missing[/]")
    if r.transport_mode == "socket" and not r.has_app_token:
        bits.append("[yellow]appToken missing[/]")
    if r.streaming_mode:
        # Color the streaming line by hazard — "partial" is the SLK015 trap.
        stream_color = "red" if r.streaming_mode == "partial" else "dim"
        nt = " native" if r.streaming_native_transport else ""
        bits.append(f"[{stream_color}]streaming={r.streaming_mode}{nt}[/]")
    if bits:
        console.print(f"  [dim]Provider:[/] {' · '.join(bits)}")

    # ── Channel state ─────────────────────────────────────────────────
    name_by_id = {
        c.get("id"): c.get("name") for c in r.bot_member_channels
        if isinstance(c.get("id"), str)
    }
    listening_ids = set(r.listening_channel_ids)
    joined_ids = set(name_by_id.keys())
    listening = sorted(listening_ids)
    joined_only = sorted(joined_ids - listening_ids)
    listening_not_joined = sorted(listening_ids - joined_ids)

    def _label(cid: str) -> str:
        name = name_by_id.get(cid)
        return f"#{name} ({cid})" if name else cid

    if listening:
        console.print(f"  [dim]Listening ({len(listening)}):[/]")
        entries_by_id = {e["channel_id"]: e for e in r.listening_channel_entries}
        for cid in listening:
            e = entries_by_id.get(cid, {})
            mention = "@-mention" if e.get("require_mention") else "listens-all"
            marker = "✓" if cid in joined_ids else "[red]✗ (not joined)[/]"
            console.print(f"    {marker} {_label(cid)} [dim][{mention}][/]")
    else:
        console.print("  [dim]Listening (0):[/] [yellow](none configured)[/]")

    if listening_not_joined:
        console.print(
            f"  [red]Listening but NOT joined ({len(listening_not_joined)}):[/] "
            + ", ".join(listening_not_joined)
        )
    if joined_only:
        console.print(f"  [dim]Joined but not in policy ({len(joined_only)}):[/]")
        for cid in joined_only:
            console.print(f"    [yellow]?[/] {_label(cid)}")

    if r.allow_from_user_ids:
        console.print(
            f"  [dim]User allowlist (allowFrom):[/] "
            f"{len(r.allow_from_user_ids)} user(s)"
        )

    # ── OAuth scope feature checklist (the team_bot_a-incremental-scopes win) ─
    if r.feature_bundles:
        _print_feature_bundle_checklist(r)

    if r.other_provider_keys:
        console.print(
            f"  [dim]Other providers in openclaw.json:[/] "
            + ", ".join(r.other_provider_keys)
        )


def _print_feature_bundle_checklist(r: "Any") -> None:
    """Render the feature-by-feature scope checklist.

    Skips bundles that are off AND don't share any scope with the
    bot's actual set — those are bundles the operator clearly hasn't
    set up at all, and listing every off-feature would be noise.
    Shows bundles that are enabled (✓), partially configured
    (✗ — some scopes present, others missing — a real surprise), or
    in the "elevated" category (separate header so the operator
    sees the security-posture posture).
    """
    enabled = [b for b in r.feature_bundles if b.enabled]
    partial = [
        b for b in r.feature_bundles
        if not b.enabled and set(b.bundle.scopes) - set(b.missing)
    ]
    console.print(f"  [dim]Features enabled ({len(enabled)}):[/]")
    for b in enabled:
        marker = "✓" if b.bundle.key not in {"manage_channels", "manage_usergroups", "auto_join", "post_anywhere", "search"} else "[yellow]✓ (elevated)[/]"
        console.print(f"    {marker} {b.bundle.name}")
    if partial:
        console.print(f"  [yellow]Features partially configured (missing scopes):[/]")
        for b in partial:
            console.print(
                f"    [yellow]✗ {b.bundle.name}[/] "
                f"[dim](missing: {', '.join(b.missing)})[/]"
            )
    if r.elevated_scopes_granted:
        console.print(
            f"  [dim]Elevated scopes granted ({len(r.elevated_scopes_granted)}):[/] "
            + ", ".join(r.elevated_scopes_granted)
        )
        console.print(
            f"    [dim]These widen the blast radius if the token leaks. Review "
            f"whether each is required.[/]"
        )


@main.command("doctor-pass")
@click.argument("bot_id", required=False, default=None, metavar="BOT")
@click.option("--all", "all_bots", is_flag=True, default=False,
              help="Run doctor --fix on every bot in the pod.")
@click.option("--timeout", default=600, show_default=True, type=int,
              help="Max seconds to wait per bot.")
@click.pass_context
def doctor_pass(
    ctx: click.Context,
    bot_id: str | None,
    all_bots: bool,
    timeout: int,
) -> None:
    """Run ``openclaw doctor --fix`` on one or every bot.

    Replaces the inline doctor --fix call that used to live in the
    deploy path. Doctor used to run synchronously before
    ``openclaw plugins install`` and started hitting 60s+ timeouts on
    most of the pod during the 2026-05-29/30 deploy --all runs — a
    hang that only manifested inside deploy.py's subprocess wrapper
    and that we could never reproduce manually under the same exact
    invocation (manual runs as the evolve user with identical flags
    consistently completed in 12-15s).

    Doctor's per-bot work (model-ref migrations, cron-payload upgrades,
    orphan-transcript reports, security warnings) is maintenance, not
    a deploy precondition. The one deploy-critical piece — clearing a
    stale plugin install when the manifest schema changed — is still
    handled inline by ``deploy._clear_stale_plugin_install``.

    This command is the on-demand path. A nightly launchd job
    (``ai.openclaw.evolve.doctor-pass.<bot>``) provides the steady-state
    cadence; run this when you've made a change you want doctor to
    reconcile right away.

    Run with sudo (each per-bot invocation sudo's to the bot's macOS
    user).
    """
    from .config import get_bot_user, load_network

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    bots_cfg = network.get("bots", {})

    if all_bots and bot_id:
        raise click.UsageError("Pass --all OR a single BOT, not both.")
    if not all_bots and not bot_id:
        raise click.UsageError("Specify BOT or pass --all.")

    targets = list(bots_cfg.keys()) if all_bots else [bot_id]
    targets = [t for t in targets if t]
    if not targets:
        console.print("[yellow]No bots to scan.[/]")
        return

    overall_rc = 0
    for target in targets:
        try:
            user = get_bot_user(target, network)
        except Exception as exc:
            console.print(f"[red]✗ {target}: could not resolve macOS user ({exc})[/]")
            overall_rc = max(overall_rc, 1)
            continue
        console.print(f"\n[bold]→ doctor --fix: {target}[/] [dim](as {user}, timeout={timeout}s)[/]")
        started = time.monotonic()
        try:
            r = subprocess.run(
                ["sudo", "-H", "-u", user,
                 "/opt/homebrew/bin/openclaw", "doctor", "--fix"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                cwd=f"/Users/{user}",
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            console.print(
                f"  [red]✗ TIMEOUT after {elapsed:.1f}s[/] "
                f"(limit {timeout}s)"
            )
            overall_rc = max(overall_rc, 1)
            continue
        except Exception as exc:
            elapsed = time.monotonic() - started
            console.print(
                f"  [red]✗ raised after {elapsed:.1f}s:[/] "
                f"{type(exc).__name__}: {exc}"
            )
            overall_rc = max(overall_rc, 1)
            continue

        elapsed = time.monotonic() - started
        if r.returncode == 0:
            console.print(f"  [green]✓ doctor finished in {elapsed:.1f}s[/]")
        else:
            console.print(f"  [yellow]⚠ doctor exited rc={r.returncode} ({elapsed:.1f}s)[/]")
            overall_rc = max(overall_rc, r.returncode)
        # Print the captured output indented so the operator can see what
        # doctor did (upgrades, warnings) without a follow-up command.
        if r.stdout:
            for line in r.stdout.splitlines()[-40:]:
                console.print(f"    [dim]{line}[/]")

    sys.exit(overall_rc)


@main.command()
@click.argument("bot_id", required=False, default=None, metavar="BOT")
@click.option("--bot", "bot_opt", default=None, hidden=True, help="Bot to deploy (macOS username)")
@click.option("--all", "all_bots", is_flag=True, default=False, help="Deploy to all network members")
@click.option("--role", default=None, type=click.Choice(["primary", "member"]),
              help="Override role (default: read from network.json)")
@click.option("--port", default=None, type=int, help="Override gateway port")
@click.option("--from-template", "from_template", default=None,
              help="Provision BOT from a template (gallery/bot-templates/<name>). "
                   "Registers the bot if absent, then deploys + installs declared skills + apps.")
@click.option("--template-var", "template_vars", multiple=True,
              metavar="KEY=VALUE",
              help="Template variable assignment (repeatable). Required vars must be set.")
@click.option("--template-vars-file", "template_vars_file", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="JSON file with template variable assignments. Merged before --template-var flags.")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be done without doing it")
@click.option(
    "--allow-audit-criticals", is_flag=True, default=False,
    help="Don't exit non-zero on post-deploy smoke-audit CRITICAL findings. "
         "Use only if you've inspected the findings and accept shipping anyway.",
)
@click.pass_context
def deploy(
    ctx: click.Context,
    bot_id: str | None,
    bot_opt: str | None,
    all_bots: bool,
    role: str | None,
    port: int | None,
    from_template: str | None,
    template_vars: tuple[str, ...],
    template_vars_file: Path | None,
    dry_run: bool,
    allow_audit_criticals: bool,
) -> None:
    """Deploy or update Evolve on one or all bots.

    Runs all 8 steps: reinstall CLI, build plugin, fix permissions,
    install OC plugin, fix shared dir perms, reinstall cron jobs,
    restart gateway, verify.

    Examples:
      evolve-admin deploy admin_bot
      evolve-admin deploy --all
      evolve-admin deploy --from-template morning-briefing briefing-bot --port 19040 \\
          --template-var user_name=Diana --template-var time_zone=America/Los_Angeles
    """
    network_path: Path = ctx.obj["network_path"]
    effective_bot = bot_id or bot_opt

    # ── Template-driven provisioning branch ──────────────────────────────────
    # Short-circuits the "must already be registered" gate by registering
    # the bot first, then dispatching to the standard _full_deploy flow,
    # then applying template-specific post-deploy steps (skills + apps +
    # file renders).
    if from_template:
        if all_bots:
            raise click.UsageError("--from-template is incompatible with --all")
        if not effective_bot:
            raise click.UsageError("--from-template requires a BOT argument")
        _deploy_from_template(
            ctx=ctx,
            bot_id=effective_bot,
            template_name=from_template,
            template_var_flags=template_vars,
            template_vars_file=template_vars_file,
            role=role,
            port=port,
            dry_run=dry_run,
            allow_audit_criticals=allow_audit_criticals,
        )
        return

    if not effective_bot and not all_bots:
        raise click.UsageError("Specify BOT (positional) or --all")

    network = load_network(network_path)
    bots = network.get("bots", {})

    # Pod membership is explicit. `deploy` redeploys EXISTING bots only —
    # it does not register new ones. To add a bot, run `evolve-admin add-bot`
    # (or use the UI's Add Bot flow). The `--role`/`--port` flags update
    # mutable fields on already-registered bots; they don't create entries.
    if effective_bot and effective_bot not in bots:
        console.print(
            f"[red]Error:[/] {effective_bot!r} is not a registered pod member.\n"
            f"  To add it, run: [bold]evolve-admin add-bot {effective_bot} "
            f"--port <PORT> [--user <MAC_USER>][/]\n"
            f"  Or provision from a template: "
            f"[bold]evolve-admin deploy --from-template <name> {effective_bot} --port <PORT>[/]"
        )
        sys.exit(1)

    # Apply --role / --port overrides into network dict for this run
    if effective_bot and (role or port):
        if role:
            bots[effective_bot]["role"] = role
        if port:
            bots[effective_bot]["port"] = port

    if all_bots:
        members = network.get("members", [])
        if not members:
            console.print("[red]No members in network.json — run evolve-admin setup first.[/]")
            sys.exit(1)
        targets = members
    else:
        targets = [effective_bot]

    audit_results: list[tuple[str, SmokeAuditResult]] = []
    for target in targets:
        console.print(f"\n[bold]→ Deploying to {target}[/]")
        audit_results.append((target, _full_deploy(target, network_path, network, dry_run)))

    if not dry_run:
        _exit_on_audit_criticals(audit_results, allow_audit_criticals)


def _exit_on_audit_criticals(
    audit_results: list[tuple[str, SmokeAuditResult]],
    allow_criticals: bool,
) -> None:
    """Exit non-zero if any smoke audit surfaced criticals or failed to run.

    Doesn't roll back the deploy — that horse left the gate. The goal is to
    make sure scripts / CI / the operator's terminal exit code reflect the
    presence of newly-fired criticals so they can't be missed.
    """
    bad: list[tuple[str, SmokeAuditResult]] = [
        (b, r) for b, r in audit_results if r.error or r.critical_findings
    ]
    if not bad:
        return
    summary = ", ".join(
        f"{b}: {'errored' if r.error else f'{len(r.critical_findings)} critical(s)'}"
        for b, r in bad
    )
    if allow_criticals:
        console.print(
            f"\n[yellow]⚠ smoke audit surfaced issues ({summary}) — "
            f"exit overridden by --allow-audit-criticals.[/]"
        )
        return
    console.print(
        f"\n[red bold]Exiting non-zero: smoke audit surfaced issues "
        f"({summary}).[/] "
        f"[dim]Fix the findings and re-run, or pass "
        f"--allow-audit-criticals to ship anyway.[/]"
    )
    sys.exit(2)


def _deploy_from_template(
    *,
    ctx: click.Context,
    bot_id: str,
    template_name: str,
    template_var_flags: tuple[str, ...],
    template_vars_file: Path | None,
    role: str | None,
    port: int | None,
    dry_run: bool,
    allow_audit_criticals: bool = False,
) -> None:
    """Handle ``deploy --from-template`` — register if needed, plan, execute.

    Flow:
      1. Parse template-var inputs (file + flags).
      2. Build the provision plan (pure; no state changes).
      3. Print the plan summary.
      4. If --dry-run, stop here.
      5. Register the bot in network.json if not already present (requires --port).
      6. Run the standard _full_deploy flow (plugin install, gateway, etc.).
      7. Apply template post-deploy: write rendered files into the workspace,
         queue declared applications for install.

    Note on skill installation: today's substrate (OpenClaw) installs plugins
    via the ``openclaw plugins install -l <PLUGIN_INSTALL_DIR>`` command,
    which is already invoked by _full_deploy's Step 4 (install_oc_plugin).
    That command picks up *every* plugin built into the evolve plugin
    install dir — there's no per-plugin invocation. So "queue skills for
    install" today resolves to "ensure the bot's openclaw.json allows the
    queued plugins" plus "verify they're live after deploy". A future
    adapter that supports per-skill install (ClawHub, skill registry) will
    plug in here without changing the template format.
    """
    from .bot_templates import (
        apply_embedded_app,
        build_plan,
        summarize_plan,
        write_file_to_bot_workspace,
    )

    network_path: Path = ctx.obj["network_path"]

    # ── 1. Parse template vars ──────────────────────────────────────────────
    vars_dict: dict[str, Any] = {}
    if template_vars_file:
        try:
            vars_dict.update(json.loads(template_vars_file.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            console.print(f"[red]Error:[/] cannot read {template_vars_file}: {e}")
            sys.exit(1)
    for assignment in template_var_flags:
        if "=" not in assignment:
            raise click.UsageError(
                f"--template-var must be KEY=VALUE, got {assignment!r}"
            )
        k, _, v = assignment.partition("=")
        vars_dict[k.strip()] = v

    # ── 2. Build plan ──────────────────────────────────────────────────────
    network = load_network(network_path)
    bot_user = get_bot_user(bot_id, network) if bot_id in network.get("bots", {}) else bot_id
    result = build_plan(
        template_name=template_name,
        bot_id=bot_id,
        bot_user=bot_user,
        vars=vars_dict,
    )

    # ── 3. Print summary ───────────────────────────────────────────────────
    for line in summarize_plan(result):
        console.print(line)

    if not result.ok:
        console.print("[red]Plan cannot proceed — fix the issues above and retry.[/]")
        sys.exit(1)

    # ── 4. Dry-run stop ────────────────────────────────────────────────────
    if dry_run:
        console.print("\n[dim](dry-run — no state was changed)[/]")
        return

    # ── 5. Register bot if absent ──────────────────────────────────────────
    bots = network.get("bots", {})
    if bot_id not in bots:
        if port is None:
            console.print(
                f"[red]Error:[/] {bot_id!r} is not registered. "
                f"--port is required when --from-template provisions a new bot."
            )
            sys.exit(1)
        from .deploy import add_bot as _add_bot
        try:
            _add_bot(
                bot_id,
                role=role or "member",
                port=port,
                network_path=network_path,
            )
            console.print(f"[green]✓[/] Registered {bot_id} in network.json")
        except ValueError as e:
            console.print(f"[red]Error registering {bot_id}:[/] {e}")
            sys.exit(1)
    else:
        # Already registered — apply --role / --port overrides if given.
        if role:
            bots[bot_id]["role"] = role
        if port:
            bots[bot_id]["port"] = port

    # ── 6. Standard deploy ──────────────────────────────────────────────────
    network = load_network(network_path)
    console.print(f"\n[bold]→ Deploying to {bot_id}[/]")
    template_audit_result = _full_deploy(bot_id, network_path, network, dry_run=False)

    # ── 7. Apply template post-deploy: write files + queue applications ────
    assert result.plan is not None
    plan = result.plan

    # Locate the freshly-deployed bot's workspace.
    from .config import get_bot_workspace
    network = load_network(network_path)
    bot_user_post = get_bot_user(bot_id, network)
    ws = get_bot_workspace(bot_id, user=bot_user_post)
    if ws is None:
        console.print(
            f"[yellow]Warning:[/] cannot locate workspace for {bot_id} — "
            f"template files (AGENTS.md / SOUL.md / exec-approvals.json) "
            f"were not written. Re-run [bold]evolve-admin deploy {bot_id}[/] "
            f"after the bot has been provisioned by OpenClaw."
        )
    else:
        console.print(f"\n[bold]→ Applying template files to {ws}[/]")
        for f in plan.files:
            try:
                write_file_to_bot_workspace(bot_user_post, ws, f.relative_path, f.content)
                console.print(f"  [green]✓[/] wrote {f.relative_path}")
            except subprocess.CalledProcessError as e:
                console.print(
                    f"  [red]✗[/] failed to write {f.relative_path}: "
                    f"{e.stderr or e}"
                )

    # ── 7b. Apply embedded-app blueprints ─────────────────────────────────────
    # V1.1-2: wire apply_embedded_app into the deploy flow for every embedded
    # app plan that has ## FILE: blocks. Each app is applied atomically: if
    # any file write or launchctl load fails, ALL files written for that app
    # are rolled back before the failure is reported. The overall deploy
    # continues (best-effort) — failures are surfaced to the operator,
    # AND a final tally line summarises succeeded/failed apps so half-deploys
    # don't get lost in console scrollback.
    #
    # V1.1-2 fix-up coordination with V1.1-1 fix-up:
    #   * Pass ``bot_id`` and ``shared_dir`` so apply_embedded_app can record
    #     the installed launchd label in the per-bot template-installs manifest
    #     (the SoT consulted by deploy.expected_plist_labels + retire-bot).
    #     Without these, V1.1-1-installed LaunchDaemons would be deleted on
    #     the next evolve-admin deploy by the orphan-sweeper.
    embedded_succeeded: list[str] = []
    embedded_failed: list[tuple[str, str]] = []  # (app_name, error)
    if plan.embedded_app_plans and ws is not None:
        home = Path(f"/Users/{bot_user_post}")
        # shared_dir for the per-bot template-installs manifest. Reads from
        # network.json (set during deploy_shared_dir) and falls back to the
        # platform default if absent.
        shared_dir_val = Path(
            network.get("sharedDir", str(DEFAULT_SHARED_DIR))
        )
        console.print(f"\n[bold]→ Applying embedded-app blueprints to {bot_id}[/]")
        for ep in plan.embedded_app_plans:
            if not ep.files:
                console.print(
                    f"  [dim]- {ep.app_name}: no FILE: blocks — skipping[/]"
                )
                continue
            console.print(f"  → {ep.app_name} ({len(ep.files)} file(s))")
            apply_result = apply_embedded_app(
                ep,
                bot_user=bot_user_post,
                workspace=ws,
                home=home,
                bot_id=bot_id,
                shared_dir=shared_dir_val,
            )
            if apply_result.ok:
                for fpath in apply_result.written:
                    console.print(f"    [green]✓[/] wrote {fpath}")
                for label in apply_result.loaded_labels:
                    console.print(f"    [green]✓[/] loaded launchd: {label}")
                embedded_succeeded.append(ep.app_name)
            else:
                console.print(
                    f"    [red]✗[/] {ep.app_name} failed: {apply_result.error}"
                )
                if apply_result.restored_paths:
                    console.print(
                        f"    (rolled back: {', '.join(apply_result.restored_paths)})"
                    )
                if apply_result.rollback_failures:
                    console.print(
                        f"    [red]WARNING: rollback incomplete for: "
                        f"{', '.join(apply_result.rollback_failures)}[/]"
                    )
                embedded_failed.append((ep.app_name, apply_result.error or "unknown"))

        # End-of-run tally so operators don't miss inline ✗ in scrollback.
        n_ok = len(embedded_succeeded)
        n_fail = len(embedded_failed)
        if n_fail == 0:
            console.print(
                f"  [bold green]Embedded apps: {n_ok} succeeded, "
                f"0 failed.[/]"
            )
        else:
            console.print(
                f"  [bold yellow]Embedded apps: {n_ok} succeeded, "
                f"{n_fail} failed.[/]"
            )
            for app_name, err in embedded_failed:
                console.print(f"    [red]- {app_name}: {err}[/]")

    # Surface application installs for the operator to action.
    # Per spec 6, the template framework queues applications; the actual
    # install pathway is the existing `evolve-admin application install`
    # command. We print the to-do list rather than auto-installing because
    # gallery apps may need bot-specific config (e.g. cron cadence) the
    # template doesn't capture today.
    if plan.applications:
        console.print(f"\n[bold]→ Applications to install on {bot_id}:[/]")
        console.print(
            "  [dim]Not auto-installed in v1; run the commands below after deploy.[/]"
        )
        for app in plan.applications:
            ref = app.app_id or app.embedded_path
            console.print(
                f"  - {app.name} ({app.source}: {ref}) — "
                f"run: [bold]evolve-admin application install {ref} --bot {bot_id} --confirm[/]"
            )

    # Surface skills that need post-deploy adapter or manual setup.
    # These are NOT installed by `_full_deploy` (which only installs the
    # Evolve plugin + whatever's already in the bot's openclaw.json).
    # Without this block the operator might believe the bot is ready
    # when in fact GOG/weather/news still need to be configured.
    adapter_skills = plan.skill_resolution.adapter_required()
    manual_skills = plan.skill_resolution.manual()
    if adapter_skills or manual_skills:
        console.print(
            f"\n[bold yellow]→ {bot_id} still needs skill setup before "
            f"first use:[/]"
        )
        for r in adapter_skills:
            console.print(
                f"  - [bold]{r.spec.id}[/] (dedicated install flow): "
                f"{r.adapter_hint or '—'}"
            )
        for r in manual_skills:
            console.print(
                f"  - [bold]{r.spec.id}[/] (manual): {r.adapter_hint or '—'}"
            )
        console.print(
            "  [dim]Applications that depend on these skills will not "
            "work until the setup is complete.[/]"
        )

    console.print(f"\n[green]✓ Template provisioning complete for {bot_id}.[/]")

    _exit_on_audit_criticals([(bot_id, template_audit_result)], allow_audit_criticals)


# ── add-bot ───────────────────────────────────────────────────────────────────

@main.command("add-bot")
@click.argument("bot_id")
@click.option("--port", required=True, type=int, help="Gateway port for this bot")
@click.option("--user", "user_arg", default=None,
              help="macOS user owning the bot (defaults to BOT_ID). "
                   "Use this when one bot lives on a personal/shared account, "
                   "e.g. --user <macos_account> when one bot lives on a personal/shared account.")
@click.option("--role", default="member", type=click.Choice(["primary", "member"]),
              help="Pod role (default: member)")
@click.option("--multi-user", is_flag=True, default=False,
              help="Mark this bot's macOS account as shared (multi-user) — affects "
                   "security boundaries; see docs/security/.")
@click.option("--backup-repo-url", default="",
              help="Optional per-bot workspace backup git remote URL.")
@click.option(
    "--daily-cap-usd", type=float, default=None,
    help="Explicit per-bot daily $ cap override. Omit to use the graduated "
         "new-bot default ($10/day for the bot's first 7 days, then the pod "
         "default of $5) — a product default resolved in code, no value is "
         "materialized. An explicit number wins over the graduated default "
         "and the pod default; pass 0 to disable the cap. Operators can "
         "adjust later via UI / `action.cost.set_bot_cap`.",
)
@click.option("--no-deploy", is_flag=True, default=False,
              help="Register only; skip the deploy step. Useful for staging "
                   "membership before the bot's host is reachable.")
@click.option("--dry-run", is_flag=True, default=False)
@click.option(
    "--allow-audit-criticals", is_flag=True, default=False,
    help="Don't exit non-zero on post-deploy smoke-audit CRITICAL findings.",
)
@click.pass_context
def add_bot_cmd(
    ctx: click.Context,
    bot_id: str,
    port: int,
    user_arg: str | None,
    role: str,
    multi_user: bool,
    backup_repo_url: str,
    daily_cap_usd: float | None,
    no_deploy: bool,
    dry_run: bool,
    allow_audit_criticals: bool,
) -> None:
    """Register a new bot in the pod and deploy to it.

    Pod membership is explicit. This is the only CLI command that adds a
    bot to network.json — `deploy` and `upgrade` operate on already-
    registered bots only.

    Examples:
      evolve-admin add-bot admin_bot --port 19000
      evolve-admin add-bot <bot_id> --port <port> --user <macos_account>
      evolve-admin add-bot primary_bot --port 19030 --role primary
    """
    from .deploy import add_bot as _add_bot

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    bots = network.get("bots", {})

    # EVO-SEP-S5: refuse reserved `evolve`/`evo` (or a reserved --user alias) before any planning/dry-run output — they are service/assistant accounts, not member bots. deploy.add_bot backstops the real path; this also gives a clean refusal on `--dry-run`. docs/spec-evo-account-separation-2026-05-25.md
    if is_reserved_account(bot_id, network) or (user_arg and is_reserved_account(user_arg)):
        console.print(f"[red]Error:[/] refusing to register {bot_id!r}: `evolve`/`evo` are reserved Evolve service/assistant accounts, not member bots. (EVO-SEP-S5)")
        sys.exit(1)

    if bot_id in bots:
        console.print(
            f"[yellow]{bot_id!r} is already registered.[/] "
            f"Use [bold]evolve-admin deploy {bot_id}[/] to redeploy."
        )
        sys.exit(1)

    if dry_run:
        console.print(f"[dim][dry-run][/] Would register {bot_id} (role={role}, port={port}, user={user_arg or bot_id}, multiUser={multi_user})")
        if not no_deploy:
            console.print(f"[dim][dry-run][/] Would deploy to {bot_id}")
        return

    try:
        # daily_cap_usd: None from the CLI = use deploy.add_bot's graduated
        # new-bot default (resolved in code, age-graded); an explicit number
        # passes through as a per-bot override. ``--daily-cap-usd 0`` disables
        # the cap (matches action_cost semantics).
        add_bot_kwargs: dict[str, Any] = dict(
            role=role,
            port=port,
            user=user_arg,
            multi_user=multi_user,
            backup_repo_url=backup_repo_url,
            network_path=network_path,
        )
        if daily_cap_usd is not None:
            add_bot_kwargs["daily_cap_usd"] = daily_cap_usd
        _add_bot(bot_id, **add_bot_kwargs)
    except ValueError as e:
        console.print(f"[red]Error:[/] {e}")
        sys.exit(1)
    console.print(f"[green]✓[/] Registered {bot_id} in network.json")

    if no_deploy:
        console.print(f"  Skipping deploy (--no-deploy). Run [bold]evolve-admin deploy {bot_id}[/] when the host is ready.")
        return

    network = load_network(network_path)
    console.print(f"\n[bold]→ Deploying to {bot_id}[/]")
    add_bot_audit = _full_deploy(bot_id, network_path, network, dry_run=False)
    _exit_on_audit_criticals([(bot_id, add_bot_audit)], allow_audit_criticals)


# ── apply-actions ─────────────────────────────────────────────────────────────
#
# Operator-facing wrapper around _materialize_scheduled_actions for the
# case where an already-installed app's manifest gains (or has) structured
# scheduled_actions[] entries that need to land on disk as actual
# LaunchDaemons. The 2026-06-04 Atlas Daily Digest incident surfaced
# this gap: gallery-side fixes don't propagate to installed bots, and
# there was no thin CLI to re-materialize the manifest's actions without
# either editing JSON by hand or triggering a full forge improvement run.
#
# See packages/admin/evolve_admin/applications/apply_actions.py for the
# logic + failure semantics.

@main.command("apply-actions")
@click.argument("bot_id")
@click.argument("app_id")
@click.option(
    "--from-gallery", is_flag=True, default=False,
    help="Re-seed scheduled_actions[] from the current gallery package "
         "before materializing. Use after a gallery migration changed "
         "shape (the 2026-06-04 Atlas case); preserves installed_* "
         "stamps where action ids match.",
)
@click.option(
    "--no-files-sync", "no_files_sync", is_flag=True, default=False,
    help="Skip the workspace-file sync pass. By default apply-actions "
         "re-copies any source file whose sha256 has drifted from the "
         "workspace; --no-files-sync disables that and runs scheduled "
         "actions only. Use when an in-flight source change would be "
         "more dangerous than stale code.",
)
@click.option(
    "--force-files-sync", "force_files_sync", is_flag=True, default=False,
    help="Force re-copy of every source file regardless of drift. Use "
         "when you suspect the sha-based drift check is masking a "
         "subtler issue (e.g. corrupt installed file with matching size).",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Print the structured summary as JSON instead of human-readable.",
)
@click.pass_context
def apply_actions_cmd(
    ctx: click.Context,
    bot_id: str,
    app_id: str,
    from_gallery: bool,
    no_files_sync: bool,
    force_files_sync: bool,
    json_output: bool,
) -> None:
    """Materialize an installed app's scheduled_actions[] on a bot and
    re-sync any drifted workspace files from their canonical source.

    The workspace-file sync runs BEFORE action materialization so a
    launchd plist pointing at ``scripts/atlas_digest.py`` runs against
    the freshly-synced file rather than the stale one. Drift is detected
    via sha256 comparison; only mismatched files are re-copied. Clean
    runs are sub-millisecond per file. Spec:
    docs/spec-workspace-file-sync-2026-06-07.md.

    Idempotent: re-running on a fully-installed, in-sync app is a no-op.
    Phase 4.5 skips actions already stamped with ``installed_by:
    "forge:*"``; the sync pass skips files whose source sha matches.

    Invocation: run as root (``sudo evolve-admin apply-actions ...``) or
    as the evolve user (``sudo -u evolve evolve-admin apply-actions ...``).
    Both work — the install helper shells out to ``sudo`` for the actual
    cp/chown/launchctl ops, and the existing ``ai.evolve.*`` NOPASSWD
    grants cover those.

    Examples:
      sudo evolve-admin apply-actions atlas atlas-daily-digest
      sudo evolve-admin apply-actions atlas atlas-daily-digest --force-files-sync
      sudo evolve-admin apply-actions admin_bot morning-briefing --from-gallery
      sudo evolve-admin apply-actions team_bot_a ea-pack --no-files-sync --json

    Exit codes:
      0 — all actions installed or skipped (idempotent)
      1 — bot not registered / manifest not found / gallery sync error /
          workspace_files_source resolves outside the repo
      2 — at least one action failed to install
    """
    import json as _json
    from .applications.apply_actions import (
        apply_actions,
        ApplyActionsError,
    )
    from .applications.workspace_sync import (
        SyncMode,
        WorkspaceFilesSourceError,
    )

    if no_files_sync and force_files_sync:
        console.print(
            "[red]✗ apply-actions: --no-files-sync and --force-files-sync "
            "are mutually exclusive[/]"
        )
        sys.exit(1)

    if no_files_sync:
        files_sync_mode = SyncMode.SKIP.value
    elif force_files_sync:
        files_sync_mode = SyncMode.FORCE.value
    else:
        files_sync_mode = SyncMode.DRIFT_AWARE.value

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path("/Users/Shared/evolve")

    try:
        result = apply_actions(
            bot_id, app_id, shared_dir,
            from_gallery    = from_gallery,
            files_sync_mode = files_sync_mode,
            network         = network,
        )
    except WorkspaceFilesSourceError as exc:
        console.print(f"[red]✗ apply-actions refused (source unsafe):[/] {exc}")
        sys.exit(1)
    except ApplyActionsError as exc:
        console.print(f"[red]✗ apply-actions failed:[/] {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — last-resort for unexpected errors
        console.print(f"[red]✗ unexpected error:[/] {type(exc).__name__}: {exc}")
        sys.exit(1)

    if json_output:
        click.echo(_json.dumps(result.to_dict(), indent=2))
    else:
        sync_note = " (synced from gallery)" if result.synced_from_gallery else ""
        console.print(
            f"[bold]{app_id}[/] on [bold]{bot_id}[/]{sync_note}: "
            f"[green]ok={result.ok_count}[/] "
            f"[yellow]skipped={result.skipped_count}[/] "
            f"[red]failed={result.failed_count}[/]"
        )
        ws = result.workspace_sync or {}
        synced_count    = ws.get("synced_count", 0) or 0
        drifted_paths   = ws.get("drifted_paths") or []
        orphan_paths    = ws.get("orphan_paths") or []
        missing_src     = ws.get("missing_in_source") or []
        sync_errors     = ws.get("errors") or []
        if synced_count:
            console.print(
                f"  [bold]workspace sync:[/] "
                f"[green]synced={synced_count}[/] from {ws.get('source','?')}"
            )
            for p in drifted_paths:
                console.print(f"    [dim]•[/] [green]{p}[/]")
        elif no_files_sync:
            console.print("  [bold]workspace sync:[/] [dim]disabled (--no-files-sync)[/]")
        elif ws.get("skipped_reason"):
            console.print(
                f"  [bold]workspace sync:[/] [dim]skipped — "
                f"{ws.get('skipped_reason')}[/]"
            )
        else:
            console.print("  [bold]workspace sync:[/] [green]clean[/] (no drift)")
        if orphan_paths:
            console.print(
                f"    [yellow]orphans:[/] {len(orphan_paths)} workspace file(s) "
                f"no longer in source"
            )
            for p in orphan_paths:
                console.print(f"      [dim]·[/] {p}")
        if missing_src:
            console.print(
                f"    [yellow]missing in source:[/] {len(missing_src)} file(s) "
                f"declared in manifest.files[] but absent from source"
            )
            for p in missing_src:
                console.print(f"      [dim]·[/] {p}")
        if sync_errors:
            console.print(
                f"    [red]sync errors:[/] {len(sync_errors)} file(s) failed to write"
            )
            for e in sync_errors:
                console.print(f"      [dim]·[/] [red]{e}[/]")
        for entry in result.summary:
            status = entry.get("status", "?")
            colour = {"ok": "green", "skipped": "yellow", "failed": "red"}.get(status, "white")
            console.print(
                f"  [dim]•[/] {entry.get('action_id','?'):20s} "
                f"mech={entry.get('mechanism','?'):24s} "
                f"[{colour}]{status}[/]"
            )
            if entry.get("error"):
                console.print(f"      [red]error:[/] {entry['error']}")
            elif entry.get("reason"):
                console.print(f"      [dim]reason:[/] {entry['reason']}")
            artifact = entry.get("artifact") or entry.get("installed_artifact")
            if artifact and status == "ok":
                console.print(f"      [dim]artifact:[/] {artifact}")

    if result.failed_count > 0:
        sys.exit(2)


# ── reconcile-actions ─────────────────────────────────────────────────────────
#
# Pod-wide drift detector for scheduled_actions[]. Walks every bot,
# compares each installed app's actions against the current gallery,
# classifies the difference, and optionally applies the fix via
# apply_actions(--from-gallery).
#
# The companion-of-apply-actions: where apply-actions is the per-(bot, app)
# scalpel, reconcile-actions is the pod sweep that surfaces what needs
# the scalpel in the first place. Same substrate; different entry point.

@main.command("reconcile-actions")
@click.option(
    "--bot", "bot_filter", default=None,
    help="Restrict to one bot instead of walking the pod.",
)
@click.option(
    "--app", "app_filter", default=None,
    help="Restrict to one app_id across all examined bots.",
)
@click.option(
    "--apply", "apply_flag", is_flag=True, default=False,
    help="Run apply_actions(--from-gallery) against each remediable "
         "drift entry (shape_drift, missing_in_installed). Default is "
         "report-only; this opts into mutation.",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="Print the structured result as JSON instead of human-readable.",
)
@click.pass_context
def reconcile_actions_cmd(
    ctx: click.Context,
    bot_filter: str | None,
    app_filter: str | None,
    apply_flag: bool,
    json_output: bool,
) -> None:
    """Audit and (optionally) fix scheduled_actions[] drift pod-wide.

    Default mode: walk every bot's installed manifests, compare each
    app's scheduled_actions[] to the current gallery package, print a
    classified report. Read-only — no daemon installs happen.

    --apply: in addition to reporting, runs apply_actions(--from-gallery)
    against each remediable drift entry. Stamp preservation keeps the
    audit trail continuous.

    Drift classifications:
      ok                     — installed shape matches gallery
      shape_drift            — same action ids, install block differs
      missing_in_installed   — gallery has action ids not installed yet
      missing_in_gallery     — installed has action ids not in gallery
                               (ambiguous; report only, not auto-applied)
      skipped_no_pkg_id      — custom app, no gallery source to compare
      skipped_side_loaded    — pkg_id not in gallery (e.g. Atlas pre-move)
      skipped_no_daemon      — neither side declares scheduled actions
      error                  — manifest/gallery load failed

    Invocation: same as apply-actions — run as root or as the evolve
    user; the existing ai.evolve.* NOPASSWD grants cover the apply path.

    Examples:
      sudo evolve-admin reconcile-actions
      sudo evolve-admin reconcile-actions --json
      sudo evolve-admin reconcile-actions --apply
      sudo evolve-admin reconcile-actions --bot atlas
      sudo evolve-admin reconcile-actions --app morning-briefing --apply

    Exit codes:
      0 — no drifted entries found (or all --apply runs succeeded)
      1 — drifted entries found (in report-only mode) OR --apply
          had at least one failure
      2 — invocation error (filter typo'd a non-existent bot, etc.)
    """
    import json as _json
    from .applications.reconcile_actions import (
        reconcile_actions,
        CLASS_OK,
        CLASS_SHAPE_DRIFT,
        CLASS_MISSING_IN_INSTALLED,
        CLASS_MISSING_IN_GALLERY,
        CLASS_SKIPPED_NO_PKG_ID,
        CLASS_SKIPPED_SIDE_LOADED,
        CLASS_SKIPPED_NO_DAEMON,
        CLASS_ERROR,
    )

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path("/Users/Shared/evolve")

    result = reconcile_actions(
        shared_dir,
        bot_filter = bot_filter,
        app_filter = app_filter,
        apply      = apply_flag,
        network    = network,
    )

    if json_output:
        click.echo(_json.dumps(result.to_dict(), indent=2))
    else:
        # Human-readable: group by bot, table-style per app.
        # Skip the "no daemon" entries by default to keep the report
        # focused on what's actionable — they're in the --json output
        # for anyone who needs them.
        actionable = [
            r for r in result.reports
            if r.classification != CLASS_SKIPPED_NO_DAEMON
        ]
        if not actionable:
            console.print("[green]✓ pod-wide reconcile clean[/] — "
                          "no drift in any installed scheduled_actions[].")
        else:
            colours = {
                CLASS_OK:                   "green",
                CLASS_SHAPE_DRIFT:          "yellow",
                CLASS_MISSING_IN_INSTALLED: "yellow",
                CLASS_MISSING_IN_GALLERY:   "magenta",
                CLASS_SKIPPED_NO_PKG_ID:    "dim",
                CLASS_SKIPPED_SIDE_LOADED:  "dim",
                CLASS_ERROR:                "red",
            }
            current_bot: str | None = None
            for r in actionable:
                if r.bot_id != current_bot:
                    console.print(f"\n[bold]{r.bot_id}[/]")
                    current_bot = r.bot_id
                colour = colours.get(r.classification, "white")
                console.print(
                    f"  [dim]•[/] {r.app_id:30s} "
                    f"[{colour}]{r.classification}[/]"
                )
                if r.detail:
                    console.print(f"      [dim]{r.detail}[/]")
                if r.applied:
                    if r.apply_error:
                        console.print(f"      [red]apply error:[/] {r.apply_error}")
                    elif r.apply_summary:
                        counts = r.apply_summary.get("counts", {})
                        console.print(
                            f"      [green]applied:[/] "
                            f"ok={counts.get('ok', 0)} "
                            f"skipped={counts.get('skipped', 0)} "
                            f"failed={counts.get('failed', 0)}"
                        )

        summary = result.by_classification
        if summary:
            console.print(
                "\n[dim]totals:[/] " + " ".join(
                    f"{k}={v}" for k, v in sorted(summary.items())
                )
            )
        if result.applied:
            console.print(
                f"[dim]apply outcomes:[/] "
                f"succeeded={result.apply_succeeded_count} "
                f"failed={result.apply_failed_count}"
            )

    # Exit code policy: any drifted entry → non-zero so this CLI can be
    # used in CI / scheduled-audit contexts. If --apply was set and every
    # remediable entry succeeded, exit 0; if some failed, exit 1.
    if result.reports and any(
        r.classification == CLASS_ERROR for r in result.reports
    ):
        # An error from a non-existent --bot filter; surface as
        # invocation-style exit code.
        if bot_filter and len(result.reports) == 1:
            sys.exit(2)
        # Other errors mix with drift; report path will surface them.

    if apply_flag:
        if result.apply_failed_count > 0:
            sys.exit(1)
        sys.exit(0)
    else:
        if result.drifted_count > 0:
            sys.exit(1)
        sys.exit(0)


# ── provision-bot ─────────────────────────────────────────────────────────────
#
# One-shot CLI that does everything `add-bot` does plus the manual steps
# operators currently run by hand (dscl + createhomedir + openclaw onboard).
# Spec: docs/spec-add-bot-wizard-2026-05-28.md §5. Used both as a
# standalone CLI ritual replacement and as the substrate the wizard
# backend (PR β) calls.

@main.command("provision-bot")
@click.argument("bot_id")
@click.option("--user", "user_arg", default=None,
              help="macOS user owning the bot (default: BOT_ID).")
@click.option("--uid", default=None, type=int,
              help="macOS UID (default: next free >= 502).")
@click.option("--port", default=None, type=int,
              help="Gateway port (default: next free in 19000-19100).")
@click.option("--role", default="member", type=click.Choice(["primary", "member"]),
              help="Pod role (default: member).")
@click.option("--display-name", default=None,
              help="Title-case display name (defaults to BOT_ID title-cased).")
@click.option("--anthropic-api-key", default=None,
              help="Anthropic key to pass to openclaw onboard. Skip if you "
                   "want to add the credential later.")
@click.option("--auth-choice", default=None,
              help="LLM provider identifier (see provisioning.AUTH_CHOICE_TO_KEY_FLAG "
                   "for the full list, e.g. anthropic, openai-api-key, ollama). "
                   "Required unless --no-onboard; --anthropic-api-key implies "
                   "'anthropic'. Evolve never presumes a provider.")
@click.option("--gateway-bind", default="loopback",
              type=click.Choice(["loopback", "any"]),
              help="Passed to openclaw onboard --gateway-bind (default: loopback).")
@click.option("--no-onboard", is_flag=True, default=False,
              help="Skip openclaw onboard (rare; only if OC is already configured).")
@click.option("--no-add-bot", is_flag=True, default=False,
              help="Skip network.json registration (rare).")
@click.option("--no-deploy", is_flag=True, default=False,
              help="Skip the deploy step (gateway plist + ACL + workspace).")
@click.option("--allow-existing-user", is_flag=True, default=False,
              help="Accept that --user already exists (shared-account bots).")
@click.option("--multi-user", is_flag=True, default=False,
              help="Mark this bot's macOS account as shared.")
@click.option("--backup-repo-url", default="",
              help="Optional per-bot workspace backup git remote URL.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print the plan + resolved UID/port and exit.")
@click.pass_context
def provision_bot_cmd(
    ctx: click.Context,
    bot_id: str,
    user_arg: str | None,
    uid: int | None,
    port: int | None,
    role: str,
    display_name: str | None,
    anthropic_api_key: str | None,
    auth_choice: str | None,
    gateway_bind: str,
    no_onboard: bool,
    no_add_bot: bool,
    no_deploy: bool,
    allow_existing_user: bool,
    multi_user: bool,
    backup_repo_url: str,
    dry_run: bool,
) -> None:
    """Provision a new bot end-to-end: create user, .openclaw, onboard, deploy.

    Replaces the manual 8-step ritual (dscl + createhomedir +
    .openclaw/ chown + `openclaw onboard --non-interactive` with
    11+ flags + add-bot + deploy) with a single command.

    The Add-a-Bot wizard (PR γ) calls this same code path via the
    `/api/wizard/bot/<id>/provision` endpoint (PR β); the CLI is the
    operator-facing entry point.

    Examples:
      sudo evolve-admin provision-bot atlas --port 19031 \\
          --anthropic-api-key sk-ant-...
      sudo evolve-admin provision-bot team_bot_b --user personal_bot_user \\
          --auth-choice openai-api-key --allow-existing-user --port 19032
    """
    from .provisioning import (
        AUTH_CHOICE_TO_KEY_FLAG,
        provision_bot,
        STATUS_OK, STATUS_FAIL, STATUS_SKIP, STATUS_START,
    )

    network_path: Path = ctx.obj["network_path"]

    # Provider-agnostic principle: no presumed --auth-choice. The one
    # sanctioned inference is the legacy --anthropic-api-key flag, whose
    # name encodes the provider the operator explicitly chose. Otherwise
    # onboard needs an explicit choice — fail here, before any state
    # (macOS user, .openclaw/) is created.
    if not auth_choice and anthropic_api_key:
        auth_choice = "anthropic"
        console.print("[dim]--anthropic-api-key implies --auth-choice anthropic[/]")
    if not auth_choice and not no_onboard:
        console.print(
            "[red]Error: --auth-choice is required (Evolve never presumes an "
            "LLM provider).[/red]\n"
            f"Valid choices: {', '.join(sorted(AUTH_CHOICE_TO_KEY_FLAG))}\n"
            "Or pass --no-onboard to configure openclaw later."
        )
        raise click.Abort()

    # Rich-formatted progress callback. Maps stage names to operator-
    # friendly labels and prints one line per status transition.
    stage_labels = {
        "validate_inputs":     "Validating inputs",
        "create_macos_user":   "Creating macOS user",
        "create_openclaw_dir": "Creating .openclaw/ directory",
        "openclaw_onboard":    "Running openclaw onboard",
        "add_bot_to_network":  "Registering in network.json",
        "deploy_bot":          "Deploying gateway + workspace",
    }

    def emit(stage: str, status: str, detail: str = "") -> None:
        label = stage_labels.get(stage, stage)
        if status == STATUS_START:
            console.print(f"[dim]→[/] {label}…", end="")
        elif status == STATUS_OK:
            tail = f" [dim]({detail})[/]" if detail else ""
            console.print(f" [green]✓[/]{tail}")
        elif status == STATUS_SKIP:
            tail = f" [dim]({detail})[/]" if detail else ""
            console.print(f" [yellow]skip[/]{tail}")
        elif status == STATUS_FAIL:
            console.print(f" [red]✗[/]")
            console.print(f"   [red]{detail}[/]")
        else:
            # "dry_run" or other ad-hoc events
            console.print(f"[dim]{label}: {detail}[/]")

    console.print(f"\n[bold]Provisioning bot:[/] {bot_id}")
    console.print(
        f"[dim]role={role}  user={user_arg or bot_id}  "
        f"port={port if port is not None else 'auto'}  "
        f"uid={uid if uid is not None else 'auto'}[/]\n"
    )

    # provision_bot's kwarg is the provider-agnostic ``provider_api_key``
    # (post-#1895 rename); the CLI flag keeps its operator-facing
    # ``--anthropic-api-key`` name, which implies ``--auth-choice
    # anthropic`` (validated above) so the long-standing one-liner
    # still works.
    result = provision_bot(
        bot_id,
        user=user_arg,
        uid=uid,
        port=port,
        role=role,
        display_name=display_name,
        provider_api_key=anthropic_api_key,
        auth_choice=auth_choice,
        gateway_bind=gateway_bind,
        skip_onboard=no_onboard,
        skip_add_bot=no_add_bot,
        skip_deploy=no_deploy,
        allow_existing_user=allow_existing_user,
        multi_user=multi_user,
        backup_repo_url=backup_repo_url,
        network_path=network_path,
        on_stage=emit,
        dry_run=dry_run,
    )

    console.print()
    if result.success:
        if dry_run:
            console.print("[green]✓ Dry-run complete.[/] No changes applied.")
            return
        console.print(f"[green]✓ Provisioned {bot_id}[/] "
                      f"(user={result.user}, uid={result.uid}, port={result.port})")
    else:
        console.print(
            f"[red]✗ Provisioning failed at stage:[/] {result.failed_stage}"
        )
        if result.error:
            console.print(f"   [red]{result.error}[/]")
        if result.rollback_log:
            console.print("\n[dim]Rolled back:[/]")
            for line in result.rollback_log:
                console.print(f"  [dim]- {line}[/]")
        sys.exit(1)


# ── seed-model-config ────────────────────────────────────────────────────────
#
# Retrofit command for bots provisioned before the seed-model-config stage
# existed in provision_bot. Atlas-style symptom: forge fails with
# "No API key found for provider 'openai'" because the bot's openclaw.json
# has no model.primary, so OC falls back to its bundled OpenAI default.
#
# Idempotent. Picks an LLM provider from the bot's auth-profiles (Anthropic
# preferred), reads the recommended models from model_registry.RECOMMENDED,
# writes them to agents.defaults.models + agents.defaults.model.primary +
# evolve-tiers.json.

@main.command("seed-model-config")
@click.argument("bot_id")
@click.option(
    "--provider", "preferred_provider", default=None,
    help="Force a specific provider (anthropic|openai|google|xai). "
         "Default: pick the first one the bot has an auth-profile for, "
         "with Anthropic preferred.",
)
@click.pass_context
def seed_model_config_cmd(
    ctx: click.Context, bot_id: str, preferred_provider: str | None,
) -> None:
    """Bootstrap a default model catalog for a bot that has none.

    Writes agents.defaults.model.primary + agents.defaults.models +
    tiers from packages/analyzer/model_registry.RECOMMENDED. Skips
    if the bot already has model.primary set (won't clobber tuned
    config).

    Example:
      sudo evolve-admin seed-model-config atlas
      sudo evolve-admin seed-model-config atlas --provider anthropic
    """
    from .provisioning import seed_model_config_if_empty

    network_path: Path = ctx.obj["network_path"]

    console.print(f"\n[bold]Seeding model config for[/] {bot_id}…")
    result = seed_model_config_if_empty(
        bot_id,
        preferred_provider=preferred_provider,
        network_path=network_path,
    )

    if not result.get("ok"):
        console.print(f"[red]✗[/] {result.get('reason')}")
        sys.exit(1)

    if result.get("seeded"):
        console.print(
            f"[green]✓[/] Seeded {result['catalog_count']} models from "
            f"[bold]{result['provider']}[/]"
        )
        console.print(f"  primary: [cyan]{result['primary']}[/]")
        console.print(f"  reason: [dim]{result.get('reason')}[/]")
    else:
        console.print(f"[yellow]skip[/] {result.get('reason')}")


# ── reconcile-catalog ─────────────────────────────────────────────────────────
#
# Retrofit for the team_bot_a-shaped drift discovered 2026-05-28: tiers
# reference google/xai/openai models that aren't in catalog, so OC
# silently drops them at runtime. This command walks the bot's tier
# definitions, adds every referenced model to the catalog, and
# optionally also seeds RECOMMENDED tier1/2/3 entries for credentialed
# providers (--add-recommended).
#
# Companion to `seed-model-config`: seed-model-config bootstraps an
# empty catalog from RECOMMENDED + bot's credentialed providers;
# reconcile-catalog patches drift between an existing catalog and
# existing tier definitions. Both end at the same target shape.
#
# Idempotent. Safe to re-run.

@main.command("reconcile-catalog")
@click.argument("bot_id")
@click.option(
    "--add-recommended/--no-add-recommended", default=False,
    help="Also add RECOMMENDED tier1/2/3 models from "
         "packages/analyzer/model_registry for every provider the bot "
         "has auth-profile credentials for. Default: only add models "
         "already named in tier definitions.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Print what would change without writing.",
)
@click.pass_context
def reconcile_catalog_cmd(
    ctx: click.Context, bot_id: str, add_recommended: bool, dry_run: bool,
) -> None:
    """Fix catalog drift — ensure every model named in any tier is also
    in the catalog (required for OC to actually use it at runtime).

    Examples:
      sudo evolve-admin reconcile-catalog team_bot_a
      sudo evolve-admin reconcile-catalog team_bot_a --add-recommended
      sudo evolve-admin reconcile-catalog team_bot_a --dry-run
    """
    from .model_catalog import reconcile_catalog
    from runtime.agent_runtime import get_runtime  # type: ignore
    runtime = get_runtime()

    cfg = runtime.full_config_get(bot_id)
    if cfg is None:
        console.print(f"[red]✗[/] could not read openclaw.json for {bot_id}")
        sys.exit(1)
    current_catalog = cfg.get("catalog") or []
    tiers = cfg.get("tiers") or {}

    from .config import load_network
    network = load_network(ctx.obj["network_path"])

    credentialed: set[str] = set()
    if add_recommended:
        # Read the bot's auth-profile providers; same approach as
        # provisioning._read_auth_profile_providers.
        from .provisioning import _read_auth_profile_providers
        from .deploy import get_bot_user
        user = get_bot_user(bot_id, network)
        credentialed = set(_read_auth_profile_providers(user))

    # Compute merged (defaults ← pod ← bot) role resolutions so reconcile also
    # adds role-resolved DEFAULT models (max → claude-fable-5) the bot's tiers
    # doc never names — the headline role_resolves_outside_catalog drift. Same
    # source of truth as find_catalog_drift. Best-effort.
    resolved_role_models: dict = {}
    try:
        from primary_bot import resolve_roles_with_provenance  # type: ignore
        roles_view = resolve_roles_with_provenance(
            network, bot_id, credentialed_providers=credentialed,
        )
        resolved_role_models = {
            role: (info or {}).get("resolvedModel")
            for role, info in (roles_view or {}).items()
            if (info or {}).get("resolvedModel")
        }
    except Exception as exc:
        console.print(
            f"[yellow]⚠[/] could not resolve roles for {bot_id} "
            f"(reconciling tiers-only): {exc}"
        )
        resolved_role_models = {}

    result = reconcile_catalog(
        current_catalog, tiers,
        credentialed_providers=credentialed,
        add_recommended_for_credentialed=add_recommended,
        resolved_role_models=resolved_role_models,
    )

    if result.unchanged:
        console.print(f"[green]✓[/] {bot_id}'s catalog is already in sync with tiers.")
        if add_recommended:
            console.print(f"  [dim]All RECOMMENDED models for credentialed providers are present.[/]")
        return

    console.print(f"\n[bold]Reconciling catalog for[/] {bot_id}")
    if result.added_from_tiers:
        console.print(f"\n  [bold]Adding {len(result.added_from_tiers)} tier-referenced model(s)[/] (currently in tiers but not catalog):")
        for m in result.added_from_tiers:
            console.print(f"    [cyan]+ {m}[/]")
    if result.added_from_recommended:
        console.print(f"\n  [bold]Adding {len(result.added_from_recommended)} RECOMMENDED model(s)[/] for credentialed providers:")
        for m in result.added_from_recommended:
            console.print(f"    [cyan]+ {m}[/]")
    if result.added_from_roles:
        console.print(f"\n  [bold]Adding {len(result.added_from_roles)} role-resolved default model(s)[/] (resolved by a role but not in catalog — e.g. max → claude-fable-5):")
        for m in result.added_from_roles:
            console.print(f"    [cyan]+ {m}[/]")

    if dry_run:
        console.print(f"\n[yellow]Dry run — no changes written.[/]")
        return

    write_result = runtime.full_config_set(bot_id, {"catalog": result.new_catalog})
    if write_result is None:
        console.print(f"\n[red]✗ write failed — check server logs.[/]")
        sys.exit(1)

    console.print(
        f"\n[green]✓[/] Wrote catalog ({len(result.new_catalog)} models). "
        f"Gateway restart required for changes to take effect."
    )


# ── handover ──────────────────────────────────────────────────────────────────

@main.command("handover")
@click.argument("bot_id")
@click.option(
    "--expires-in", "expires_in_days", default=7, show_default=True, type=int,
    help="Days until the link expires (default: 7).",
)
@click.option(
    "--for", "audience", default="personal_bot_user",
    type=click.Choice(["personal_bot_user", "team_bot_member"]),
    help="Who the bot is being handed to (v1 treats both the same; recorded for audit).",
)
@click.option(
    "--message", "-m", "message", default="",
    help="Custom greeting line shown on the landing page (e.g. \"Hi Diana — your assistant is ready.\").",
)
@click.option(
    "--rotate", is_flag=True, default=False,
    help="Replace any existing unclaimed token for this bot.",
)
@click.pass_context
def handover_cmd(
    ctx: click.Context,
    bot_id: str,
    expires_in_days: int,
    audience: str,
    message: str,
    rotate: bool,
) -> None:
    """Generate a one-tap onboarding link to hand a bot off to its end user.

    Use this when you installed a bot for someone else. The new user taps
    the link, picks how the assistant should address them and the voice
    they prefer, and they're done — no terminal, no openclaw.json,
    nothing technical.

    Examples:
      evolve-admin handover diana_personal -m "Hi Diana — your assistant is ready."
      evolve-admin handover board_assistant --expires-in 3
      evolve-admin handover diana_personal --rotate    # replace existing link
    """
    from .handover import create_token, build_handover_url, DEFAULT_EXPIRES_IN_DAYS

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    bots = network.get("bots", {}) or {}
    if bot_id not in bots:
        console.print(
            f"[red]Error:[/] {bot_id!r} isn't registered in this pod. "
            f"Use [bold]evolve-admin add-bot[/] first."
        )
        sys.exit(1)

    shared_dir = Path(network.get("sharedDir") or DEFAULT_SHARED_DIR)
    rec, created = create_token(
        shared_dir,
        bot_id=bot_id,
        audience=audience,
        message=message,
        expires_in_days=expires_in_days or DEFAULT_EXPIRES_IN_DAYS,
        rotate=rotate,
    )
    url = build_handover_url(network, rec["token"])

    if created:
        verb = "Created" if not rotate else "Rotated → new"
    else:
        verb = "Existing"

    console.print(
        Panel.fit(
            f"[bold]{verb} handover link for {bot_id}[/]\n\n"
            f"  [cyan]{url}[/]\n\n"
            f"  [dim]Expires:[/] {rec['expires_at']}\n"
            f"  [dim]Audience:[/] {rec.get('audience', 'personal_bot_user')}\n"
            + (f"  [dim]Message:[/] {rec['message']}\n" if rec.get('message') else "")
            + "\n"
            f"  Text the link to the new user — they tap it to finish onboarding.\n"
            f"  You can pause this bot any time with: [bold]evolve-admin remove-evolve {bot_id}[/]\n"
            f"  (or pause everything with [bold]evolve-admin pause-all[/])",
            title="Handover",
            border_style="green" if created else "yellow",
        )
    )
    if not created:
        console.print(
            "[dim]An unclaimed link already exists for this bot. "
            "Re-run with [bold]--rotate[/] to replace it.[/]"
        )


# ── ensure-pod-perms ──────────────────────────────────────────────────────────

@main.command("ensure-pod-perms")
@click.option("--check-only", is_flag=True, default=False,
              help="Report drift without applying. Output mirrors what would be applied.")
@click.option("--bot", "bot_id", default=None,
              help="Run only this bot's per-bot checks (default: all bots in network.json).")
@click.pass_context
def ensure_pod_perms_cmd(ctx: click.Context, check_only: bool, bot_id: str | None) -> None:
    """Idempotently enforce pod-side permission contract.

    Codifies the four perm layers we hand-applied during the 2026-04-25
    apply.py-zombie incident:

      1. /Users/<bot>/.openclaw/ ACL has the canonical allow-set entries
         (admin user, evolve service user, configured security bot —
         derived by deploy.pod_acl_users() from system + network state)
      2. /Users/Shared/evolve/apply_processed-<bot>.lock pre-exists, owned by <bot>
      3. /opt/homebrew/Cellar/python@3.14/ is readable + executable for all users
      4. /Users/Shared/evolve/proposals/ + standard subdirs are mode 1777
      Plus: apply daemon plist exists per-bot with the canonical UserName.

    With --check-only, reports drift without applying. Output is grouped by
    section so the report can be cross-checked against tools/etr-pod-doctor.
    """
    network_path: Path = ctx.obj["network_path"]

    if check_only is False and os.geteuid() != 0:
        # Most fixes need sudo (chmod +a, chown, mkdir under /opt). The
        # in-process ones will silently fail without root. Refuse early.
        console.print("[red]This command needs sudo to apply fixes:[/]")
        console.print(f"  sudo evolve-admin ensure-pod-perms{' --bot ' + bot_id if bot_id else ''}")
        console.print("  (or run with --check-only to report drift without applying)")
        sys.exit(1)

    result = ensure_pod_perms(
        bot_id=bot_id,
        network_path=network_path,
        check_only=check_only,
    )

    # Group checks by (category, target) so each section header prints once.
    from collections import OrderedDict
    sections: "OrderedDict[tuple[str, str], list]" = OrderedDict()
    for c in result.checks:
        key = (c.category, c.target)
        sections.setdefault(key, []).append(c)

    drift_count = 0
    for (category, target), checks in sections.items():
        console.print(f"\n[bold]== {category}: {target} ==[/]")
        for c in checks:
            if c.ok:
                console.print(f"  [green]✓[/] {c.detail}")
            else:
                drift_count += 1
                arrow = "→ would " if check_only else "→ "
                fix_text = f" {arrow}{c.fix_description}" if c.fix_description else ""
                console.print(f"  [red]✗[/] {c.detail}{fix_text}")

    console.print()
    if check_only:
        if drift_count == 0:
            console.print("[green]No drift detected.[/]")
        else:
            console.print(
                f"[yellow]{drift_count} drift item(s).[/] "
                f"Run without --check-only to apply."
            )
            sys.exit(1)
    else:
        if result.applied:
            console.print(f"[green]Applied {len(result.applied)} change(s).[/]")
        else:
            console.print("[green]0 changes — pod is already in canonical state.[/]")
        if result.errors:
            console.print(f"[red]{len(result.errors)} error(s):[/]")
            for err in result.errors:
                console.print(f"  [red]✗[/] {err}")
            sys.exit(2)


# ── audit-acls ────────────────────────────────────────────────────────────────

@main.command("audit-acls")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit machine-readable JSON instead of the text report.")
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Repair drifted invariants (chmod, chown, chmod +a). "
                   "Requires sudo for most fixes. Off by default — the auditor "
                   "is read-only without this flag.")
@click.pass_context
def audit_acls_cmd(ctx: click.Context, as_json: bool, do_apply: bool) -> None:
    """Audit pod-wide ACL + permission invariants against documented state.

    Independent diagnostic that walks the documented invariants for
    ``/Users/Shared/evolve/`` and every bot's ``.openclaw/`` workspace and
    reports drift between actual macOS file state (mode bits, sticky bit,
    owner, ACL entries) and the canonical rule table in
    ``evolve_admin.tools.audit_pod_acls``.

    This is intentionally a separate source of truth from
    ``ensure-pod-perms``. The motivating incident was the sticky bit on
    ``proposals/pending/`` silently blocking ``evo`` from os.replace-ing
    proposal files — the ACL allowed write but sticky kicked in on the
    implicit unlink. The auditor's rule table says 0o0775 (no sticky)
    plus an inherited evo write ACL.

    Exit codes:
      0 — no drift
      1 — drift detected (and not auto-fixed via --apply)
      2 — some paths unreadable

    Examples::

        evolve-admin audit-acls                  # report only
        evolve-admin audit-acls --json           # for Signal ingestion
        sudo evolve-admin audit-acls --apply     # report + fix
    """
    from .tools.audit_pod_acls import main_cli as _audit_main
    network_path: Path = ctx.obj["network_path"]
    exit_code = _audit_main(
        as_json=as_json,
        apply=do_apply,
        network_path=network_path,
    )
    if exit_code != 0:
        sys.exit(exit_code)


# ── provision-evo-account (Phase E.2.a) ──────────────────────────────────────

@main.command("provision-evo-account")
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Report what would be done without making changes.",
)
def provision_evo_account_cmd(dry_run: bool) -> None:
    """Provision the `evo` macOS account empty (Phase E.2.a).

    Phase E of the evo/admin separation creates a dedicated, non-privileged
    `evo` macOS user that evo's gateway will eventually run as. This
    command is the first half of E.2 — it creates the account and the
    empty `/Users/evo/.openclaw/` tree but does NOT change any LaunchDaemon
    plist or migrate state. The actual runtime cutover (E.2.b) happens
    after the admin daemon endpoints land in Phase E.3.

    Idempotent: safe to re-run. On systems where the account already
    exists this is a no-op (or refresh-only for the chown/ACL steps).

    Spec: docs/spec-evo-account-separation-2026-05-25.md §"Phase E.2.a".

    Examples:
      sudo evolve-admin provision-evo-account
      sudo evolve-admin provision-evo-account --dry-run
    """
    from .setup_wizard import _provision_evo_account, _user_exists

    if dry_run:
        already = _user_exists("evo")
        console.print(
            "[dim][dry-run][/] " +
            ("'evo' account already exists; would refresh chown + ACL only."
             if already else
             "Would create 'evo' macOS account + empty /Users/evo/.openclaw/ tree, "
             "then grant the evolve user ACL read access.")
        )
        return

    if os.geteuid() != 0:
        console.print("[red]This command needs sudo:[/]")
        console.print("  sudo evolve-admin provision-evo-account")
        sys.exit(1)

    ok = _provision_evo_account()
    if not ok:
        console.print("[red]✗ Provisioning failed — see error output above.[/]")
        sys.exit(2)
    console.print("[green]✓ Phase E.2.a complete:[/] /Users/evo/.openclaw/ ready.")
    console.print(
        "  Nothing runs there yet. The cutover to make evo's gateway run as "
        "the new account is Phase E.2.b — wait for that to land after Phase E.3."
    )


# ── remove (deprecated alias for detach-bot) ──────────────────────────────────

@main.command()
@click.option("--bot", "bot_id", required=True, help="Bot to disconnect from Evolve")
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--shared-dir", "shared_dir_arg", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help="Override shared dir (default: from network.json).")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="Skip the interactive 'are you sure' prompt.")
@click.pass_context
def remove(
    ctx: click.Context,
    bot_id: str,
    dry_run: bool,
    shared_dir_arg: Path | None,
    skip_confirm: bool,
) -> None:
    """Deprecated alias for `evolve-admin detach-bot`.

    The legacy ``remove`` command called ``deploy.remove_bot``, which
    only unloaded the long-defunct ``ai.openclaw.evolve.measure.<bot>``
    plist and stripped the network.json entry — silently leaving 7+
    per-bot Evolve daemons (apply, test, cost-converter, audit-runner,
    audit-runner-t3, doctor-pass, backup) running forever. PR #1903
    fixed the UI and MCP paths; this finishes the CLI by routing
    through ``detach-bot`` (which uses ``retire.remove_evolve_plugin``
    over the canonical ``per_bot_evolve_plist_labels`` source of truth).

    Use ``detach-bot`` (stop Evolve, keep bot), ``retire-bot``
    (graceful archive + remove), or ``delete-bot`` (irreversible)
    directly going forward.
    """
    console.print(
        "[yellow]`evolve-admin remove` is deprecated; "
        "use `evolve-admin detach-bot` (or retire-bot / delete-bot) "
        "instead.[/]"
    )
    ctx.invoke(
        detach_bot_cmd,
        bot_id=bot_id, dry_run=dry_run,
        shared_dir_arg=shared_dir_arg, skip_confirm=skip_confirm,
    )


# ── retire-bot ────────────────────────────────────────────────────────────────

@main.command("retire-bot")
@click.argument("bot_id")
@click.option("--dry-run", is_flag=True, default=False,
              help="Plan only — generate closure summary in memory, "
                   "report what would happen, touch nothing on disk.")
@click.option("--shared-dir", "shared_dir_arg", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help="Override shared dir (default: from network.json).")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="Skip the interactive 'are you sure' prompt.")
@click.pass_context
def retire_bot_cmd(
    ctx: click.Context,
    bot_id: str,
    dry_run: bool,
    shared_dir_arg: Path | None,
    skip_confirm: bool,
) -> None:
    """Gracefully retire a bot.

    Generates a Markdown closure summary, archives the bot's data,
    cleanly stops its LaunchDaemons, removes it from the network, and
    notifies the pod's primary integration. Reversible: the macOS
    user account is not deleted, and the archive contains everything
    needed to revive the bot later.

    Pass `--dry-run` to preview without touching the filesystem.
    """
    from .retire import retire_bot, lifecycle_cli_refusal, DEFAULT_SHARED_DIR

    network_path: Path = ctx.obj["network_path"]
    shared_dir = shared_dir_arg or DEFAULT_SHARED_DIR

    if (msg := lifecycle_cli_refusal(bot_id, network_path)):
        console.print(f"[red]{msg}[/]")
        sys.exit(1)

    # Real retirement edits root-owned /Library/LaunchDaemons/ and shells
    # out to `sudo /bin/launchctl bootout` / `sudo /bin/rm`. Without root
    # those subprocesses fail, but the unsudo'd launchctl probe also
    # returns nonzero from a non-root caller — which the verification
    # step would silently treat as "service gone" and report a green
    # checkmark while the daemons keep running. Bail loudly up front.
    # Mirrors the pattern at cli.py:326, 710, 912, 1265.
    if not dry_run and os.geteuid() != 0:
        console.print("[red]This command must be run with sudo:[/]")
        console.print(f"  sudo evolve-admin retire-bot {bot_id}")
        console.print("  [dim](Use --dry-run to preview the plan without root.)[/]")
        sys.exit(1)

    if not dry_run and not skip_confirm:
        console.print(
            f"[yellow]This will retire bot[/] [bold]{bot_id}[/][yellow]:"
            " archive its data, stop its services, and remove it from the"
            " network. The macOS user account stays in place.[/]"
        )
        if not click.confirm("Proceed?", default=False):
            console.print("[dim]Aborted.[/]")
            return

    # Pre-flight inventory preview — shows the operator what artifacts
    # archive would touch and which need manual off-host cleanup. Cheap
    # (pure read-only) and always useful before the destructive op.
    _print_lifecycle_preview(bot_id, network_path, action="archive")

    with console.status(
        f"[bold]Retiring {bot_id}{' (dry-run)' if dry_run else ''}...[/]"
    ):
        result = retire_bot(
            bot_id,
            network_path=network_path,
            shared_dir=shared_dir,
            dry_run=dry_run,
        )

    for step in result.steps:
        console.print(f"  [dim]{step}[/]")

    if result.archive_path is not None:
        console.print(
            f"\n[bold]Archive:[/] {result.archive_path}"
            + ("  [dim](dry-run; not created)[/]" if dry_run else "")
        )
    if result.summary_path is not None:
        console.print(f"[bold]Closure summary:[/] {result.summary_path}")
    if result.plists_stopped:
        console.print(
            f"[green]Plists stopped:[/] {', '.join(result.plists_stopped)}"
        )
    if result.plists_failed:
        console.print(f"[red]Plists FAILED to stop:[/] {', '.join(result.plists_failed)}")
    if result.notification_outcome:
        console.print(
            f"[dim]Notification outcome: {result.notification_outcome}[/]"
        )

    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/]")
        for err in result.errors:
            console.print(f"  [red]✗[/] {err}")
        sys.exit(1)

    if result.success:
        if dry_run:
            console.print(f"\n[green]✓[/] dry-run complete for {bot_id}")
        else:
            console.print(f"\n[green]✓ {bot_id} retired cleanly[/]")
            # Notify MCP bridge to reload its bot registry
            try:
                from .mcp_service import reload_registry as _mcp_reload, status as _mcp_status
                if _mcp_status().get("running"):
                    ok, msg = _mcp_reload()
                    console.print(f"  [dim]MCP bridge: {msg}[/]")
            except Exception:
                pass


# ── remove-evolve ─────────────────────────────────────────────────────────────

@main.command("remove-evolve")
@click.argument("bot_id")
@click.option("--dry-run", is_flag=True, default=False,
              help="Plan only — report what would happen, touch nothing.")
@click.option("--shared-dir", "shared_dir_arg", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help="Override shared dir (default: from network.json).")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="Skip the interactive 'are you sure' prompt.")
@click.pass_context
def remove_evolve_cmd(
    ctx: click.Context,
    bot_id: str,
    dry_run: bool,
    shared_dir_arg: Path | None,
    skip_confirm: bool,
) -> None:
    """Disable Evolve on a bot while leaving the bot itself running.

    Stops the per-bot evolve plists (apply, test, cost-converter — set
    sourced from ``deploy.per_bot_evolve_plist_labels``) AND strips the
    evolve plugin entries from the bot's ``openclaw.json`` so the
    runtime stops loading the plugin on each turn. Leaves the bot's
    OpenClaw gateway running and marks the bot ``evolve_disabled`` in
    network.json. Smaller-scope counterpart to ``retire-bot`` — use
    when you want a bot to keep operating but want Evolve to stop
    observing or proposing changes to it.
    """
    from .retire import remove_evolve_plugin, lifecycle_cli_refusal, DEFAULT_SHARED_DIR

    network_path: Path = ctx.obj["network_path"]
    shared_dir = shared_dir_arg or DEFAULT_SHARED_DIR

    if (msg := lifecycle_cli_refusal(bot_id, network_path)):
        console.print(f"[red]{msg}[/]")
        sys.exit(1)

    # Same root-check rationale as retire-bot: launchctl bootout is
    # sudo-only, and the post-condition probe gives false negatives under
    # non-root, which would silently report success while leaving the
    # evolve daemons running. Mirrors cli.py:326, 710, 912, 1265.
    if not dry_run and os.geteuid() != 0:
        console.print("[red]This command must be run with sudo:[/]")
        console.print(f"  sudo evolve-admin remove-evolve {bot_id}")
        console.print("  [dim](Use --dry-run to preview the plan without root.)[/]")
        sys.exit(1)

    if not dry_run and not skip_confirm:
        console.print(
            f"[yellow]This will disable Evolve on[/] [bold]{bot_id}[/][yellow]:"
            " stop the measure + apply daemons, mark the bot evolve_disabled."
            " The OpenClaw gateway keeps running.[/]"
        )
        if not click.confirm("Proceed?", default=False):
            console.print("[dim]Aborted.[/]")
            return

    # Pre-flight inventory preview — see retire_bot_cmd for rationale.
    _print_lifecycle_preview(bot_id, network_path, action="detach")

    result = remove_evolve_plugin(
        bot_id,
        network_path=network_path,
        shared_dir=shared_dir,
        dry_run=dry_run,
    )

    for step in result.steps:
        console.print(f"  [dim]{step}[/]")
    if result.plists_stopped:
        console.print(
            f"[green]Plists stopped:[/] {', '.join(result.plists_stopped)}"
        )
    if result.plists_failed:
        console.print(f"[red]Plists FAILED to stop:[/] {', '.join(result.plists_failed)}")

    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/]")
        for err in result.errors:
            console.print(f"  [red]✗[/] {err}")
        sys.exit(1)
    if dry_run:
        console.print(f"\n[green]✓[/] dry-run complete for {bot_id}")
    else:
        console.print(f"\n[green]✓ Evolve disabled on {bot_id}[/]")


# ── detach-bot (alias for remove-evolve) + lifecycle group ────────────────────


@main.command("detach-bot")
@click.argument("bot_id")
@click.option("--dry-run", is_flag=True, default=False,
              help="Plan only — report what would happen, touch nothing.")
@click.option("--shared-dir", "shared_dir_arg", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help="Override shared dir (default: from network.json).")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="Skip the interactive 'are you sure' prompt.")
@click.pass_context
def detach_bot_cmd(ctx, bot_id, dry_run, shared_dir_arg, skip_confirm):
    """Detach a bot from Evolve — keep it running as an OpenClaw bot.

    Alias for ``remove-evolve``, kept so the three lifecycle commands
    cluster in --help: ``detach-bot`` / ``retire-bot`` / (future
    ``delete-bot``). Same behavior — see ``remove-evolve --help``.
    """
    # Delegate to remove-evolve so behavior stays in lockstep.
    ctx.invoke(
        remove_evolve_cmd,
        bot_id=bot_id, dry_run=dry_run,
        shared_dir_arg=shared_dir_arg, skip_confirm=skip_confirm,
    )


# ── delete-bot (irreversible full removal) ────────────────────────────────────


@main.command("delete-bot")
@click.argument("bot_id")
@click.option("--dry-run", is_flag=True, default=False,
              help="Plan only — generate closure summary in memory, "
                   "report what would happen, touch nothing on disk.")
@click.option("--shared-dir", "shared_dir_arg", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help="Override shared dir (default: from network.json).")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="Skip the interactive 'are you sure' prompt + DELETE "
                   "typed confirmation. Intended for tests / automation.")
@click.pass_context
def delete_bot_cmd(
    ctx: click.Context,
    bot_id: str,
    dry_run: bool,
    shared_dir_arg: Path | None,
    skip_confirm: bool,
) -> None:
    """Irreversibly delete a bot.

    Runs the full `retire-bot` flow (archives data, stops services,
    removes from network) AND then deletes the bot's macOS user
    account + ``/Users/<bot>/`` home directory. The archive remains
    in ``{shared_dir}/retired/`` for forensics but the bot cannot
    be revived — the macOS account is gone.

    Safety: if the bot's macOS user name does not match the bot_id
    (the piggyback case — bot opts into running under an existing
    operator user account), the macOS user is preserved even when
    --yes is passed. Deleting someone's actual home dir
    by accident is the worst-case outcome this command protects
    against. The bot is still retired and removed from the network.

    Pass ``--dry-run`` to preview without touching the filesystem.
    """
    from .retire import delete_bot, lifecycle_cli_refusal, DEFAULT_SHARED_DIR

    network_path: Path = ctx.obj["network_path"]
    shared_dir = shared_dir_arg or DEFAULT_SHARED_DIR

    if (msg := lifecycle_cli_refusal(bot_id, network_path)):
        console.print(f"[red]{msg}[/]")
        sys.exit(1)

    # Same root-check rationale as retire-bot. Without root we cannot
    # launchctl bootout / rm plists / dscl delete, and the unsudo'd
    # probes silently report success, which would leave the operator
    # thinking the bot is gone while live daemons keep running.
    if not dry_run and os.geteuid() != 0:
        console.print("[red]This command must be run with sudo:[/]")
        console.print(f"  sudo evolve-admin delete-bot {bot_id}")
        console.print("  [dim](Use --dry-run to preview the plan without root.)[/]")
        sys.exit(1)

    if not dry_run and not skip_confirm:
        console.print(
            f"[bold red]IRREVERSIBLE:[/] this will delete bot "
            f"[bold]{bot_id}[/], stop its daemons, remove it from the "
            f"network, AND delete the macOS user account + "
            f"[bold]/Users/{bot_id}/[/]. An archive remains in "
            f"{shared_dir}/retired/ but the bot cannot be revived."
        )
        typed = click.prompt(
            'Type "DELETE" to confirm', default="", show_default=False,
        )
        if typed.strip() != "DELETE":
            console.print("[dim]Aborted — DELETE not typed exactly.[/]")
            return

    # Pre-flight inventory preview — shows what archive + user delete
    # would touch. Reuses the same helper as retire-bot.
    _print_lifecycle_preview(bot_id, network_path, action="delete")

    with console.status(
        f"[bold]Deleting {bot_id}{' (dry-run)' if dry_run else ''}...[/]"
    ):
        result = delete_bot(
            bot_id,
            network_path=network_path,
            shared_dir=shared_dir,
            dry_run=dry_run,
        )

    for step in result.steps:
        console.print(f"  [dim]{step}[/]")

    if result.archive_path is not None:
        console.print(
            f"\n[bold]Archive (still on disk):[/] {result.archive_path}"
            + ("  [dim](dry-run; not created)[/]" if dry_run else "")
        )
    if result.plists_stopped:
        console.print(
            f"[green]Plists stopped:[/] {', '.join(result.plists_stopped)}"
        )
    if result.plists_failed:
        console.print(f"[red]Plists FAILED to stop:[/] {', '.join(result.plists_failed)}")

    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/]")
        for err in result.errors:
            console.print(f"  [red]✗[/] {err}")
        sys.exit(1)

    if result.success:
        if dry_run:
            console.print(f"\n[green]✓[/] dry-run complete for {bot_id}")
        else:
            console.print(f"\n[green]✓ {bot_id} deleted[/]")
            try:
                from .mcp_service import reload_registry as _mcp_reload, status as _mcp_status
                if _mcp_status().get("running"):
                    ok, msg = _mcp_reload()
                    console.print(f"  [dim]MCP bridge: {msg}[/]")
            except Exception:
                pass


def _print_lifecycle_preview(bot_id: str, network_path: Path, action: str) -> None:
    """One-shot pre-flight inventory preview printed before detach / archive run.

    Surfaces the count of items each action will touch and the off-host
    cleanup checklist (if any) so the operator sees the impact before
    the destructive op runs. Failure to load the inventory is silent —
    the lifecycle command continues. ``action`` is "detach" or "archive".
    """
    try:
        from .lifecycle import compile_bot_inventory, LifecycleAction
        from .config import load_network
        network = load_network(network_path)
        inv = compile_bot_inventory(bot_id, network=network)
    except Exception as e:
        console.print(f"[dim](inventory preview unavailable: {e})[/]")
        return

    s = inv.summary
    target_count = s.get(f"removed_by_{action}", 0)
    manual = inv.manual_cleanup()
    console.print(
        f"\n[bold]Pre-flight inventory[/]  [dim]({s.get('total_items', 0)} "
        f"items total for {bot_id})[/]"
    )
    console.print(
        f"  [bold]{action}[/] would touch [bold]{target_count}[/] item(s); "
        f"{len(manual)} need manual off-host cleanup"
    )
    if manual:
        console.print(
            "  [yellow]Manual cleanup required (after Evolve finishes):[/]"
        )
        for it in manual:
            console.print(f"    · {it.name}")
        console.print(
            "  [dim](run `evolve-admin lifecycle inventory "
            f"{bot_id}` for full detail)[/]"
        )


@main.group("lifecycle")
def lifecycle_group():
    """Bot lifecycle commands — discover and retire bots.

    Three first-class retirement paths (cluster around the existing
    detach-bot / retire-bot commands):

      detach   — strip Evolve from a bot but leave it running
                 (alias: ``evolve-admin detach-bot <bot>``,
                 implementation: ``evolve-admin remove-evolve``)
      archive  — graceful full retirement; reversible via archive restore
                 (``evolve-admin retire-bot <bot>``)
      delete   — irreversible full removal incl. macOS user + home dir
                 (``evolve-admin delete-bot <bot>``)

    Use ``evolve-admin lifecycle inventory <bot>`` to discover everything
    that belongs to a bot before deciding which path to take.
    """


@lifecycle_group.command("inventory")
@click.argument("bot_id")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the full inventory as JSON (for tooling).")
@click.option("--action", type=click.Choice(["detach", "archive", "delete"]),
              default=None,
              help="Filter to items removed by the named lifecycle action.")
@click.pass_context
def lifecycle_inventory_cmd(ctx, bot_id, as_json, action):
    """Discover everything that belongs to BOT — read-only.

    Walks per-bot launchd plists, openclaw.json (plugins, channels,
    exec policy), openclaw cron jobs, workspace credentials, backup
    repo URL, SSH deploy keys, signals + proposals tagged with the
    bot, and config_intents. Classifies each item by which lifecycle
    action removes it. No filesystem changes — safe to run anytime.
    """
    import json as _json
    from .lifecycle import compile_bot_inventory, LifecycleAction
    from .config import load_network

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    inv = compile_bot_inventory(bot_id, network=network)

    if as_json:
        click.echo(_json.dumps(inv.to_dict(), indent=2))
        return

    # Filter for --action if requested.
    filter_action: LifecycleAction | None = (
        LifecycleAction(action) if action else None
    )
    items = (
        inv.items_for(filter_action) if filter_action else inv.items
    )

    # Pretty render.
    primary_marker = "  [PRIMARY]" if inv.is_primary else ""
    console.print(
        f"\n[bold]Bot:[/] {inv.bot_id}  "
        f"[dim](macOS user: {inv.macos_user}){primary_marker}[/]"
    )
    s = inv.summary
    console.print(
        f"[dim]{s.get('total_items', 0)} total items · "
        f"detach removes {s.get('removed_by_detach', 0)} · "
        f"archive removes {s.get('removed_by_archive', 0)} · "
        f"delete removes {s.get('removed_by_delete', 0)} · "
        f"{s.get('manual_cleanup_items', 0)} need manual off-host cleanup[/]"
    )
    if filter_action:
        console.print(
            f"[yellow]Filtered to items removed by:[/] [bold]{filter_action.value}[/]"
        )

    # Group by category for readable rendering.
    from collections import defaultdict
    by_cat: dict[str, list] = defaultdict(list)
    for it in items:
        by_cat[it.category.value].append(it)

    for cat in sorted(by_cat.keys()):
        console.print(f"\n[bold]{cat}[/]")
        for it in by_cat[cat]:
            removed_by = ",".join(
                a.value for a in sorted(it.removed_by, key=lambda x: x.value)
            ) or "—"
            marker = " [yellow][manual][/]" if it.manual_action else ""
            console.print(f"  · {it.name}{marker}")
            if it.detail:
                console.print(f"      [dim]{it.detail}[/]")
            console.print(f"      [dim]removed_by: {removed_by}[/]")
            if it.manual_action:
                console.print(f"      [yellow]{it.manual_action}[/]")

    # Final manual-cleanup recap.
    manual = inv.manual_cleanup()
    if manual:
        console.print(
            f"\n[bold yellow]Off-host cleanup checklist[/] "
            f"[dim]({len(manual)} item(s) Evolve cannot automate)[/]"
        )
        for it in manual:
            console.print(f"  · [yellow]{it.name}[/] — {it.manual_action}")


# ── setup-shared ──────────────────────────────────────────────────────────────

@main.command("refresh-sudoers")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print the would-be sudoers file to stdout instead of installing it.")
@click.option("--no-pull", is_flag=True, default=False,
              help="Skip the git pull step. Use when testing local template edits.")
def refresh_sudoers(dry_run: bool, no_pull: bool) -> None:
    """Regenerate /etc/sudoers.d/evolve from the current template.

    By default, first does `git pull --ff-only origin main` on the repo
    that evolve-admin was installed from — so template changes from PRs
    merged to main are picked up automatically. Pass --no-pull to opt
    out when testing local edits.

    The git pull guard exists because the common footgun with this
    command is running it without first pulling: the template on disk is
    stale, the regenerated sudoers is stale, and subsequent deploys
    silently fail with "sudo: a password is required" at whatever step
    the template got out of sync with the code. Making the pull the
    default eliminates that category of mistake.

    Runs visudo -c on the rendered file before installing. Refuses to
    install if the file fails syntax check.
    """
    from .setup_wizard import _write_evolve_sudoers, _render_evolve_sudoers
    if dry_run:
        rendered = _render_evolve_sudoers()
        if rendered is None:
            console.print("[red]✗ Could not render sudoers template — see setup_wizard._find_openclaw_path()[/]")
            sys.exit(1)
        console.print(rendered)
        return

    # Pull repo before rendering so a stale checkout doesn't silently
    # install an outdated template. Skipped with --no-pull for local
    # template-edit testing.
    if not no_pull:
        pull_ok, pull_msg = _pull_repo_for_sudoers_refresh()
        if not pull_ok:
            console.print(f"[red]✗ git pull failed — aborting so we don't install a stale sudoers file[/]")
            console.print(f"  [dim]{pull_msg}[/]")
            console.print(f"  [dim]Re-run with --no-pull to skip the pull step (only safe when your local template is the intended one).[/]")
            sys.exit(1)
        console.print(f"[dim]{pull_msg}[/]")

    ok = _write_evolve_sudoers(initiated_by="cli")
    if not ok:
        console.print("[red]✗ refresh-sudoers failed (visudo check or install errored) — see log[/]")
        sys.exit(1)
    console.print("[green]✓ /etc/sudoers.d/evolve refreshed[/]")


@main.command("repo-pull")
@click.option("--repo", default=None,
              help="Repo path to pull (default: platform-keyed deploy checkout — "
                   "/Users/Shared/evolve-repo on macOS, /var/lib/evolve/repo on Linux)")
@click.option("--remote", default="origin")
@click.option("--branch", default="main")
@click.option("--quiet", is_flag=True, default=False,
              help="Suppress no-op output (still prints advances + errors). "
                   "Used by the LaunchDaemon to keep the log readable.")
@click.option("--setup-key", is_flag=True, default=False,
              help="Run only the deploy-key bootstrap (generate evolve user's "
                   "ed25519 key + SSH config + print operator instructions for "
                   "adding the public key to GitHub). Skips the actual pull. "
                   "Useful for re-printing the public key on demand or "
                   "recovering from a botched manual setup.")
@click.option("--repo-url", default="",
              help="GitHub web URL for the deploy-key instructions deep link. "
                   "When empty, auto-resolves from network.json pod.repo_url or "
                   "`git remote get-url origin` on the deploy checkout.")
@click.option("--hooks-from", default="",
              help="Run ONLY the post-advance hook suite for an externally-"
                   "performed code move (release-manager promote/rollback): "
                   "the sha the checkout moved from. Requires --hooks-to.")
@click.option("--hooks-to", default="",
              help="Companion to --hooks-from: the sha the checkout now sits at.")
def repo_pull(repo: str, remote: str, branch: str, quiet: bool,
              setup_key: bool, repo_url: str,
              hooks_from: str, hooks_to: str) -> None:
    """Fast-forward the deployed evolve-repo to origin/main.

    Wired as ai.evolve.evolve.repo-puller LaunchDaemon (every 15min)
    so the deployed repo stays current with origin/main without
    operator intervention. Without this, every PR merge requires a
    manual `git pull` on the deploy box to propagate the new code to the
    daemons that load from /Users/Shared/evolve-repo.

    Safe by design: --ff-only refuses non-fast-forward (won't
    silently overwrite local commits). On non-fast-forward, surfaces
    a clear hint so the operator notices instead of the failure
    being one line of git noise.

    Manual invocation: `evolve-admin repo-pull` (drop --quiet to see
    no-op confirmations too).

    Deploy-key bootstrap: `sudo evolve-admin repo-pull --setup-key`
    re-runs just the SSH key generation + GitHub instructions
    (without performing a pull). Useful when the auto-bootstrap at
    install time was missed or you need to re-print the public key.
    """
    from . import repo_puller as _rp
    from . import release_manager as _relmgr

    # Resolve the deploy-checkout path from the platform profile when the
    # caller (the repo-puller daemon invokes `repo-pull --quiet` with no
    # --repo) didn't pin one. _rp.DEFAULT_REPO is /Users/Shared/evolve-repo
    # on macOS, /var/lib/evolve/repo on Linux — without this the daemon
    # pulled the macOS literal on a Linux pod ("repo path does not exist").
    if not repo:
        repo = str(_rp.DEFAULT_REPO)

    if hooks_from or hooks_to:
        # Hooks-only mode: the release manager moved the checkout and
        # re-invokes us in a fresh subprocess so EVOLVE_VERSION reflects
        # the post-move code (spec-state-store-and-deploy-resilience §2.4).
        if not (hooks_from and hooks_to):
            print("ERROR: --hooks-from and --hooks-to must be given together",
                  file=sys.stderr)
            sys.exit(2)
        hooks_result = _rp.run_hooks_only(Path(repo), hooks_from, hooks_to)
        out = _rp.format_for_log(hooks_result, quiet=quiet)
        if out:
            print(out)
        sys.exit(0 if hooks_result.success else 1)

    if setup_key:
        # Run the bootstrap, print operator instructions, exit. No pull.
        # Must be invoked as root (writes to /Users/evolve/.ssh/).
        if os.geteuid() != 0:
            print("ERROR: --setup-key must be run as root "
                  "(writes to /Users/evolve/.ssh/). Re-run with `sudo`.",
                  file=sys.stderr)
            sys.exit(1)
        dk = _rp.ensure_deploy_key()
        if not dk.success:
            print(f"deploy-key bootstrap failed: {dk.error}", file=sys.stderr)
            sys.exit(1)
        for step in dk.steps:
            print(f"[deploy-key] {step}")
        print(_rp.format_deploy_key_instructions(dk, repo_url=repo_url))
        sys.exit(0 if dk.auth_test_ok else 0)   # success either way; auth fail is an
                                                # operator-action-pending state, not an error

    # Mark this process as the puller daemon so any downstream code
    # (notably _rp.install_launchd called from the auto-install hook
    # when the pulled diff touches deploy.py) can skip a bootout of the
    # puller's own service. A self-bootout SIGTERMs this process before
    # the follow-up bootstrap can re-register the plist, leaving the
    # daemon silently unloaded. See repo_puller.PULLER_PROCESS_ENV.
    os.environ[_rp.PULLER_PROCESS_ENV] = str(os.getpid())

    # Mode dispatch (spec-state-store-and-deploy-resilience §2.8):
    # pod.release.mode == "canary" routes the tick through the gated
    # release pipeline (candidate → Gate 1 → canary soak → promote);
    # "direct" (the default) is the legacy pull-origin/main behavior.
    # EVOLVE_RELEASE_MODE env overrides for a 30-second disable.
    try:
        _net_for_release = load_network(DEFAULT_NETWORK_CONFIG)
    except Exception as _net_err:
        # Fail CLOSED when this pod has release state: resolving to the
        # default ("direct") on a transient network.json read error would
        # do a full ungated pull of origin/main, which the next canary
        # tick then yanks back via pointer repair — a restart whipsaw.
        try:
            _has_release_state = _relmgr.release_state_path(
                _rp.DEFAULT_SHARED_DIR).exists()
        except Exception:
            _has_release_state = False
        if _has_release_state:
            print(f"[release] network.json unreadable ({_net_err}); "
                  f"release state exists — refusing an ungated direct pull",
                  file=sys.stderr)
            sys.exit(1)
        _net_for_release = {}
    _release_cfg = _relmgr.resolve_release_config(_net_for_release)

    if _release_cfg.mode == "canary":
        shared_dir = Path(_net_for_release.get("sharedDir", str(_rp.DEFAULT_SHARED_DIR))) \
            if isinstance(_net_for_release, dict) else _rp.DEFAULT_SHARED_DIR
        rt = _relmgr.release_tick(
            Path(repo), shared_dir,
            remote=remote, branch=branch, cfg=_release_cfg,
        )
        for line in rt.steps:
            print(f"[release] {line}")
        # The per-tick pod maintenance the legacy pull() provided
        # (config validation + lagging-bot redeploy) must keep running
        # even though the fleet checkout only moves at promote.
        maint = _rp.run_tick_maintenance(Path(repo), shared_dir)
        maint_out = _rp.format_for_log(maint, quiet=True)
        if maint_out:
            print(maint_out)
        sys.exit(0 if rt.success else 1)

    result = _rp.tick(
        repo=Path(repo),
        remote=remote,
        branch=branch,
    )
    output = _rp.format_tick_for_log(result, quiet=quiet)
    if output:
        print(output)
    sys.exit(0 if result.pull.success else 1)


# ── release: gated-deploy pointer management (7.2) ───────────────────────────
#
# The `evolve-admin release` command group lives in release_cli.py (cli.py is
# line-count capped); register it onto the top-level group here. Spec:
# docs/spec-state-store-and-deploy-resilience-2026-06-10.md Part 2.
from .release_cli import release_group as _release_group  # noqa: E402
main.add_command(_release_group)


@main.command("digest-flush")
@click.option("--hourly", is_flag=True, default=False,
              help="Self-gating hourly tick — flush only when local time "
                   "matches the configured digest window (digest_hour_local "
                   "for daily; Monday at digest_hour_local for weekly). "
                   "Designed for the LaunchDaemon entrypoint.")
@click.option("--frequency", default=None, type=click.Choice(["daily", "weekly"]),
              help="One-shot flush at the given frequency (manual mode). "
                   "Bypasses the digest-hour gate.")
def digest_flush(hourly: bool, frequency: str | None) -> None:
    """Drain the alert digest queue.

    Wired as ai.evolve.evolve.digest-flush LaunchDaemon (every hour at
    :00 with ``--hourly``). The daemon ticks 24 times a day; exactly
    one of those ticks matches ``digest_hour_local`` and actually flushes.
    Weekly digests additionally gate on weekday == Monday.

    Manual invocation::

        evolve-admin digest-flush --frequency daily     # force-flush daily queue
        evolve-admin digest-flush --frequency weekly    # force-flush weekly queue

    Both ``--hourly`` and ``--frequency`` cover the same module CLI
    (``python3 -m evolve_admin.alerts.digest_dispatcher``) but routed
    through the stable evolve-admin binary so PYTHONPATH for the admin
    package is correct (the daemon's PYTHONPATH points at analyzer/).
    """
    from .alerts.digest_dispatcher import _cli as _digest_cli
    from .config import DEFAULT_SHARED_DIR

    argv: list[str] = ["--shared-dir", str(DEFAULT_SHARED_DIR)]
    if hourly:
        argv.append("--hourly")
    elif frequency:
        argv += ["--frequency", frequency]
    else:
        raise click.UsageError("either --hourly or --frequency is required")

    sys.exit(_digest_cli(argv))


@main.command("cve-scan-finalize")
def cve_scan_finalize() -> None:
    """Drain today's CVE candidate JSON and dispatch the security alert.

    Wired as ai.evolve.evolve.security-cve-scan-finalize LaunchDaemon
    (daily at 09:10 America/Los_Angeles, ten minutes after the LLM
    discovery cron). Idempotent — running it twice in a day with a
    log entry already on disk is a no-op.

    Manual invocation:
        sudo -u evolve evolve-admin cve-scan-finalize

    See packages/analyzer/evolve_apps/security-cve-scan/finalize.py
    for the discipline applied (installed-version filter, baseline
    mute, idempotency, message render per
    docs/operator-message-style.md).
    """
    import importlib.util
    finalize_path = (
        Path(__file__).parent.parent.parent
        / "analyzer" / "evolve_apps" / "security-cve-scan" / "finalize.py"
    )
    if not finalize_path.exists():
        console.print(f"[red]Finalizer not found at {finalize_path}[/]")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("cve_scan_finalize", finalize_path)
    if spec is None or spec.loader is None:
        console.print("[red]Could not load finalize.py module spec[/]")
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.exit(mod._cli([]))


@main.command("audit-scheduler-tick")
@click.option("--quiet", is_flag=True, default=False,
              help="Suppress no-op output. Used by the LaunchDaemon.")
def audit_scheduler_tick(quiet: bool) -> None:
    """Run one pass of the audit scheduler.

    Wired as ai.evolve.evolve.audit-scheduler LaunchDaemon (hourly).
    Fires the pod-wide infra audit when its cadence is due and drains
    every bot's per-bot audit-outbox into the Signal store via
    applications.audit_poller.

    Renamed from app-test-tick on 2026-06-08 when the app-test surface
    was killed. The audit-poller half always ran independently; this
    is now its sole responsibility. See
    docs/decision-app-tests-2026-06-08.md.

    Manual invocation: ``evolve-admin audit-scheduler-tick`` (drop --quiet
    to see the full per-tick summary).
    """
    from .applications import audit_scheduler as _sched
    from .config import load_network, DEFAULT_SHARED_DIR

    network = load_network()
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    result = _sched.tick(shared_dir, network=network)

    summary = result.as_dict()
    nothing_ran = (
        summary["audit_files_processed"] == 0
        and summary["infra_audit_files_processed"] == 0
        and summary.get("fit_review_files_processed", 0) == 0
        and not summary["errors"]
    )
    if quiet and nothing_ran:
        sys.exit(0)
    print(f"[audit-scheduler-tick] {summary}")
    sys.exit(0 if not summary["errors"] else 1)


# Backward-compat alias — old LaunchDaemon plist still in operator's
# /Library/LaunchDaemons fires `evolve-admin app-test-tick` until
# install_launchd swaps it out. Forward to the new command.
@main.command("app-test-tick", hidden=True)
@click.option("--quiet", is_flag=True, default=False)
@click.pass_context
def _app_test_tick_legacy(ctx: click.Context, quiet: bool) -> None:
    """DEPRECATED 2026-06-08 — forwards to audit-scheduler-tick.

    The legacy plist label (ai.evolve.evolve.app-test-scheduler) was
    renamed; this shim is kept for one deploy so the old daemon's last
    run before bootout doesn't error.
    """
    ctx.invoke(audit_scheduler_tick, quiet=quiet)


@main.command("pairing-sweep")
@click.option("--quiet", is_flag=True, default=False,
              help="Suppress no-op output. Used by the LaunchDaemon.")
def pairing_sweep(quiet: bool) -> None:
    """Run one pass of the pairing auto-approver across every bot.

    Wired as the ``ai.evolve.evolve.pairing-sweep`` LaunchDaemon
    (StartInterval=30s). Honors three auto-approval triggers
    per spec-user-roster-and-roles-2026-06-07: pod-admin claims,
    primary-owner claims, and per-channel newcomer_mode=auto_admit.
    Blocked identities are never auto-approved regardless of trigger.

    The same sweep also runs inline at the start of every GET to the
    Users page (via ``_auto_approve_inline``). Periodic + inline is
    belt-and-suspenders: the periodic catches the case where no admin
    is on the page; the inline keeps the page-render state fresh.

    Manual invocation: ``evolve-admin pairing-sweep`` (drop --quiet to
    see the per-bot summary even when nothing happened).
    """
    from .config import load_network
    from .pairing import auto_approver as _aa
    from .web import routes_bot_users as _rbu

    network = load_network()
    results = _aa.run_sweep_all_bots(network, rbu_module=_rbu)

    total_approved = sum(len(r.approved) for r in results)
    total_blocked = sum(len(r.skipped_blocked) for r in results)
    total_errors = sum(len(r.errors) for r in results)
    nothing_happened = (
        total_approved == 0 and total_blocked == 0 and total_errors == 0)
    if quiet and nothing_happened:
        sys.exit(0)

    for r in results:
        if not (r.approved or r.skipped_blocked or r.errors):
            continue
        print(f"[pairing-sweep] {r.bot_id}: "
              f"approved={len(r.approved)} "
              f"blocked-skipped={len(r.skipped_blocked)} "
              f"errors={len(r.errors)}")
        for a in r.approved:
            print(f"  + {a['channel']}/{a['id']} — {a['reason']}")
        for b in r.skipped_blocked:
            print(f"  ✗ {b['channel']}/{b['id']} — blocked")
        for e in r.errors:
            print(f"  ! {e}")
    sys.exit(0 if total_errors == 0 else 1)


def _pull_repo_for_sudoers_refresh() -> tuple[bool, str]:
    """Fast-forward the evolve-admin repo to origin/main.

    Discovers the repo via evolve_admin.__file__ (four parents up lands
    at the repo root on both editable installs and the mini's layout).
    Returns (ok, user-readable-message).

    Refuses to pull if:
      - The discovered path isn't a git repo (pip install without -e)
      - The current branch isn't main (avoid pulling upstream into a
        feature branch)
      - The working tree is dirty (avoid merge conflicts masking a bad
        state with a successful-looking pull)
      - Fetch fails (network / auth)
      - The merge isn't a clean fast-forward

    Each refusal yields a specific message so the user knows what to do.
    """
    # Walk up from the installed evolve_admin package looking for .git.
    # On the mini (editable install): evolve_admin/ is under
    # /Users/Shared/evolve-repo/packages/admin/ — four steps up hits the
    # repo root. Non-editable installs under site-packages will never
    # find a .git — that's an explicit failure mode we detect below.
    import evolve_admin as _mod
    start = Path(_mod.__file__).resolve().parent
    repo: Path | None = None
    candidate = start
    for _ in range(8):
        if (candidate / ".git").exists():
            repo = candidate
            break
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    if repo is None:
        return False, (
            f"could not locate a .git/ directory walking up from {start}. "
            f"If evolve-admin was pip-installed non-editably, re-install with "
            f"`pip install -e <repo>/packages/admin`."
        )

    # Run as the evolve user, not as root. refresh-sudoers is invoked under
    # `sudo evolve-admin ...`, so this python process is root — but root
    # doesn't have GitHub deploy-key access (the ed25519 key lives under
    # /Users/evolve/.ssh/ and is owned by the evolve user, used by the
    # repo-puller daemon every 15 min). A `git fetch` as root therefore
    # tries publickey auth with no key in scope and fails with
    # "Permission denied (publickey)". Running git as evolve picks up
    # /Users/evolve/.ssh/config + the deploy key naturally, matching what
    # the repo-puller daemon does. Reads (rev-parse, status) are routed
    # the same way for consistency and to sidestep git's safe.directory
    # ownership warning on a repo owned by the evolve user.
    def _git(*args: str, timeout: int = 30) -> tuple[int, str, str]:
        r = subprocess.run(  # sudo-grant: root-only — `sudo evolve-admin` CLI (operator root), dropping TO evolve
            ["sudo", "-u", "evolve", "git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    rc, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return False, f"git rev-parse failed at {repo}"
    if branch != "main":
        return False, (
            f"current branch is '{branch}', not main. "
            f"Checkout main first, or re-run with --no-pull if this is intentional."
        )

    rc, status, _ = _git("status", "--porcelain")
    if rc != 0:
        return False, f"git status failed at {repo}"

    # Paths that normal operation legitimately rewrites. A deploy runs
    # `tsc` which regenerates packages/plugin/dist/* on every call, so
    # any mini that's ever deployed will have a "dirty" tree relative
    # to the committed dist/. Same for node_modules if npm was run.
    # package-lock.json drifts when `npm install` runs against a
    # different npm version than the one that produced the committed
    # lockfile. The egg-info/ prefix is a fallback for pods that haven't
    # pulled the commit that untracks it — pip install -e . rewrites it.
    # Tolerating these lets refresh-sudoers work on a working mini
    # without forcing the operator to manually `git checkout --` the
    # build outputs every time.
    #
    # NOTE: we don't just `git checkout --` the paths because that
    # would overwrite a tsc build the operator is actively testing.
    # Tolerating-but-reporting is safer: we pull over the top, and if
    # the pull conflicts with the local dist/ state, git merge --ff-only
    # will fail loudly downstream.
    def _is_ignorable(porcelain_line: str) -> bool:
        # Porcelain format: "XY path" where X/Y are status flags
        # (space, M, A, D, etc). Path starts at offset 3.
        if len(porcelain_line) < 4:
            return False
        path = porcelain_line[3:].strip()
        ignorable_prefixes = (
            "packages/plugin/dist/",
            "packages/plugin/node_modules/",
            "packages/admin/evolve_admin.egg-info/",
        )
        ignorable_exact = (
            "packages/plugin/package-lock.json",
        )
        return path.startswith(ignorable_prefixes) or path in ignorable_exact

    if status:
        dirty_lines = status.splitlines()
        non_ignorable = [ln for ln in dirty_lines if not _is_ignorable(ln)]
        if non_ignorable:
            return False, (
                f"working tree has uncommitted changes at {repo} "
                f"(beyond known build artifacts):\n    "
                + "\n    ".join(non_ignorable)
                + "\n  Commit, stash, or discard first; or --no-pull to skip."
            )
        # Only build-artifact drift. Log it so the operator sees we
        # tolerated something, then continue.
        ignored_count = len(dirty_lines)
        _ok_ignore = f"(tolerating {ignored_count} expected build-artifact changes)"
    else:
        _ok_ignore = ""

    rc, _, stderr = _git("fetch", "--quiet", "origin", "main", timeout=60)
    if rc != 0:
        return False, f"git fetch failed: {stderr}"

    # Canary release mode (spec-state-store-and-deploy-resilience §2.10):
    # the fleet checkout follows the RELEASE POINTER, not origin tip —
    # pulling origin/main here would bypass every gate. Sync to the
    # pointer instead (almost always a no-op: the release tick already
    # repairs drift) and never past it.
    try:
        from . import release_manager as _relmgr
        _net = load_network(DEFAULT_NETWORK_CONFIG)
        _rcfg = _relmgr.resolve_release_config(_net)
        if _rcfg.mode == "canary":
            _shared = Path(_net.get("sharedDir", "/Users/Shared/evolve"))
            _state = _relmgr.load_release_state(_shared)
            if _state is None:
                # Canary mode enabled but no tick has initialized the
                # pointer yet. Falling through to the legacy pull would
                # be a gate bypass in that window — skip instead; the
                # next puller tick initializes and reconciles.
                return True, (
                    "✓ canary mode: no release state yet; skipping pull "
                    "(next puller tick initializes the pointer)"
                )
            rc, head_now, _ = _git("rev-parse", "HEAD")
            if rc == 0 and head_now == _state.stable["sha"]:
                return True, (
                    f"✓ canary mode: fleet already at the release pointer "
                    f"({head_now[:12]}); origin tip is gated by the canary "
                    f"pipeline, not pulled here"
                )
            # Drifted. Do NOT reset from here: this path tolerates local
            # build-artifact drift (plugin dist/) that a reset --hard
            # would clobber, and pointer repair is the release tick's
            # job (with its quarantine sweep). Refuse, with the fix path.
            return False, (
                f"fleet HEAD {head_now[:12]} != release pointer "
                f"{_state.stable['sha'][:12]}; the next `evolve-admin "
                f"repo-pull` tick repairs this (or run `evolve-admin "
                f"release status`). Re-run with --no-pull to install the "
                f"on-disk sudoers template as-is."
            )
    except Exception as e:
        return False, (
            f"canary-mode pointer sync errored ({type(e).__name__}: {e}); "
            f"re-run with --no-pull if the on-disk template is the intended one"
        )

    rc, head_before, _ = _git("rev-parse", "--short", "HEAD")
    rc, merge_out, stderr = _git("merge", "--ff-only", "origin/main")
    if rc != 0:
        # Common cause when we reach here: the ignorable paths DID
        # conflict with upstream. Surface clearly.
        return False, (
            f"git merge --ff-only failed: {stderr.splitlines()[0] if stderr else 'unknown'}. "
            f"If the conflict is in packages/plugin/dist/, run "
            f"`git checkout -- packages/plugin/dist/` then retry."
        )
    rc, head_after, _ = _git("rev-parse", "--short", "HEAD")
    if _ok_ignore:
        _ok_ignore = " " + _ok_ignore

    if head_before == head_after:
        return True, f"✓ git pull: already up to date (HEAD: {head_after}){_ok_ignore}"
    return True, f"✓ git pull: fast-forwarded {head_before} → {head_after}{_ok_ignore}"


@main.command("setup-shared")
@click.option("--dir", "shared_dir", default=str(DEFAULT_SHARED_DIR), type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, default=False)
def setup_shared(shared_dir: Path, dry_run: bool) -> None:
    """Create the shared directory with correct permissions."""
    result = deploy_shared_dir(shared_dir, dry_run=dry_run)
    for step in result.steps:
        console.print(f"  [dim]{step}[/]")
    if result.errors:
        for err in result.errors:
            console.print(f"[red]✗ {err}[/]")
        sys.exit(1)
    console.print(f"[green]✓ Shared dir ready: {shared_dir}[/]")


# ── health ────────────────────────────────────────────────────────────────────

@main.command("health")
@click.option("--fix", is_flag=True, default=False, help="Apply non-privileged fixes automatically")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output results as JSON")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show passing checks too")
@click.pass_context
def health(ctx: click.Context, fix: bool, as_json: bool, verbose: bool) -> None:
    """Scan OpenClaw instances, permissions, and services; flag issues and suggest fixes.

    Exit 0 = all pass (or warnings only), exit 2 = at least one failure.
    """
    from .health import run_health_check, print_report, apply_all_fixes

    network_path: Path = ctx.obj["network_path"]

    with console.status("Scanning pod..."):
        report = run_health_check(network_path=network_path)

    if fix:
        fix_results = apply_all_fixes(report)
        for fr in fix_results:
            color = "green" if fr.ok else "yellow"
            console.print(f"  [{color}]{'✓' if fr.ok else '⚠'}[/] {fr.name}: {fr.message}")

    if as_json:
        import json as _json
        console.print_json(_json.dumps(report.as_dict(), indent=2))
    else:
        print_report(report, verbose=verbose)

    if not report.ok:
        sys.exit(2)


# ── migrate-generator-records ─────────────────────────────────────────────────

@main.command("migrate-generator-records")
@click.option("--apply", "apply_changes", is_flag=True, default=False,
              help="Actually rewrite the records (dry-run by default).")
@click.option("--shared-dir", default="/Users/Shared/evolve",
              help="Pod shared dir containing generators/ records.")
@click.pass_context
def migrate_generator_records(
    ctx: click.Context, apply_changes: bool, shared_dir: str
) -> None:
    """Sync generator records' charter_fingerprints with the deployed charters.

    Charters are immutable at runtime: when the YAML changes, the registry
    refuses to load the generator until its stored record is updated. This
    command surfaces the mismatches and (with --apply) rewrites them.

    Default is dry-run — print what would change, do nothing.

    Use after merging a charter change. Common case: a Security Warden
    invariant or scanner update lands; on next runner cycle the L3 detector
    AND the new logic both fail to load until you run this.
    """
    import json as _json
    from pathlib import Path as _Path

    # In-tree charters live alongside the analyzer package source.
    analyzer_dir = _Path(__file__).parent.parent.parent / "analyzer"

    try:
        from registry.charter_loader import compute_charter_fingerprint
    except ImportError as e:
        console.print(f"[red]✗ analyzer registry not importable: {e}[/]")
        sys.exit(2)

    shared = _Path(shared_dir)
    records_dir = shared / "generators"
    code_dir = analyzer_dir / "generators"

    if not records_dir.exists():
        console.print(f"[yellow]no records dir at {records_dir}[/] (nothing to migrate)")
        return

    mismatches: list[tuple[str, str, str, _Path, _Path]] = []
    for entry in sorted(code_dir.iterdir()):
        if not entry.is_dir():
            continue
        charter_path = entry / "charter.yaml"
        if not charter_path.exists():
            charter_path = entry / "charter.yml"
            if not charter_path.exists():
                continue
        record_path = records_dir / f"{entry.name}.json"
        if not record_path.exists():
            continue
        try:
            content = charter_path.read_text(encoding="utf-8")
            new_fingerprint = compute_charter_fingerprint(content)
            record_data = _json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as e:
            console.print(f"[red]✗ {entry.name}: cannot read ({e})[/]")
            continue
        old = record_data.get("charter_fingerprint", "")
        if old != new_fingerprint:
            mismatches.append((entry.name, old, new_fingerprint, charter_path, record_path))

    if not mismatches:
        console.print("[green]✓ all generator records match their charter fingerprints[/]")
        return

    console.print(f"[yellow]{len(mismatches)} generator record(s) out of date:[/]")
    for gid, old, new, _, _ in mismatches:
        console.print(f"  - {gid}: {old[:12]}... → {new[:12]}...")

    if not apply_changes:
        console.print()
        console.print("[dim]dry-run — re-run with --apply to rewrite the records[/]")
        return

    for gid, _, new, _, record_path in mismatches:
        try:
            record_data = _json.loads(record_path.read_text(encoding="utf-8"))
            record_data["charter_fingerprint"] = new
            tmp = record_path.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(record_data, indent=2), encoding="utf-8")
            tmp.replace(record_path)
            console.print(f"  [green]✓[/] {gid}")
        except (OSError, _json.JSONDecodeError) as e:
            console.print(f"  [red]✗[/] {gid}: {e}")
            sys.exit(2)

    console.print(f"[green]✓ migrated {len(mismatches)} record(s)[/]")


# ── migrate-model-roles ───────────────────────────────────────────────────────
# Command body lives in migrate_model_roles.register_cli (beside the migration
# it drives); this attaches it to the top-level group.
from . import migrate_model_roles as _migrate_model_roles  # noqa: E402

_migrate_model_roles.register_cli(main)


# ── warden-suppress / warden-suppressions ────────────────────────────────────

@main.group("warden")
def warden() -> None:
    """Security Warden management subcommands."""


@warden.command("suppressions")
@click.option("--shared-dir", default="/Users/Shared/evolve",
              help="Pod shared dir (defaults to /Users/Shared/evolve).")
def warden_list_suppressions(shared_dir: str) -> None:
    """List Security Warden's do_not_reflag suppressions."""
    from pathlib import Path as _Path

    from generators.security_warden import do_not_reflag as _dnr
    entries = _dnr.list_suppressions(_Path(shared_dir))
    if not entries:
        console.print("[dim]no suppressions[/]")
        return
    for e in entries:
        bot_id = e.get("bot_id", "?")
        patterns = ",".join(e.get("pattern_set", []))
        added = e.get("added_at", "")
        source = e.get("source", "?")
        reason = e.get("reason", "")
        console.print(f"  {bot_id} [{source}] {patterns}  {added}  {reason}")


@warden.command("suppress")
@click.option("--bot", "bot_id", required=True, help="Bot id this suppression applies to.")
@click.option("--patterns", required=True,
              help="Comma-separated pattern ids (e.g. 'ignore_previous_instructions,dan_jailbreak').")
@click.option("--reason", default="manual", help="Reason text for the audit log.")
@click.option("--shared-dir", default="/Users/Shared/evolve")
def warden_add_suppression(
    bot_id: str, patterns: str, reason: str, shared_dir: str
) -> None:
    """Manually suppress a Security Warden injection signature.

    Example:
      evolve-admin warden suppress --bot team_bot_a \\
        --patterns ignore_previous_instructions,system_tag_breakout \\
        --reason "user demos jailbreaks for security training"
    """
    from pathlib import Path as _Path

    from generators.security_warden import do_not_reflag as _dnr
    pattern_list = [p.strip() for p in patterns.split(",") if p.strip()]
    if not pattern_list:
        console.print("[red]✗ --patterns must contain at least one pattern id[/]")
        sys.exit(2)
    added = _dnr.add_suppression(
        _Path(shared_dir), bot_id, pattern_list, reason=reason
    )
    if added:
        console.print(
            f"[green]✓[/] suppressed {bot_id}: {sorted(pattern_list)}"
        )
    else:
        console.print(
            f"[yellow]⚠[/] {bot_id}: {sorted(pattern_list)} was already suppressed"
        )


@warden.command("unsuppress")
@click.option("--bot", "bot_id", required=True)
@click.option("--patterns", required=True)
@click.option("--shared-dir", default="/Users/Shared/evolve")
def warden_remove_suppression(bot_id: str, patterns: str, shared_dir: str) -> None:
    """Remove a Security Warden suppression so the patterns flag again."""
    from pathlib import Path as _Path

    from generators.security_warden import do_not_reflag as _dnr
    pattern_list = [p.strip() for p in patterns.split(",") if p.strip()]
    removed = _dnr.remove_suppression(_Path(shared_dir), bot_id, pattern_list)
    if removed:
        console.print(f"[green]✓[/] removed suppression for {bot_id}: {sorted(pattern_list)}")
    else:
        console.print(f"[yellow]⚠[/] no matching suppression found")


# ── scan-secrets ──────────────────────────────────────────────────────────────

@main.command("scan-secrets")
@click.option("--bot", "bot_id", default=None,
              help="Single bot to scan (defaults to all members of the network)")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output findings as JSON")
@click.pass_context
def scan_secrets(ctx: click.Context, bot_id: str | None, as_json: bool) -> None:
    """Scan bot workspace files for hardcoded credentials.

    Wraps the same audit_workspace_secrets() the 15-min security audit
    runs. Use this for ad-hoc verification after a wizard run, after a
    bot is provisioned, or before declaring a remediation complete.

    Exit 0 when no findings (or warnings only). Exit 2 when at least
    one CRITICAL finding (a credential pattern matched).
    """
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    members = network.get("members", []) if isinstance(network, dict) else []
    targets = [bot_id] if bot_id else list(members)
    if not targets:
        console.print("[red]No bots to scan — pass --bot or populate network.json members.[/]")
        sys.exit(2)

    try:
        from audit import audit_workspace_secrets
    except ImportError as e:
        console.print(f"[red]audit module not importable: {e}[/]")
        sys.exit(2)

    shared_dir = Path(network.get("shared_dir") or DEFAULT_SHARED_DIR)
    all_findings: list[Any] = []
    for bid in targets:
        all_findings.extend(audit_workspace_secrets(bid, shared_dir))

    crits = [f for f in all_findings if f.level == "critical"]

    if as_json:
        import json as _json
        console.print_json(_json.dumps([
            {"level": f.level, "category": f.category, "bot_id": f.bot_id,
             "message": f.message, "detail": f.detail}
            for f in all_findings
        ], indent=2))
    else:
        if not crits:
            console.print(f"[green]✓ No credentials found in workspaces ({len(targets)} bot(s) scanned).[/]")
        else:
            console.print(f"[red]✗ {len(crits)} credential finding(s) across {len(targets)} bot(s):[/]")
            for f in crits:
                console.print(f"  [red]🔴[/] {f.message}")
            console.print(f"\n  [dim]Remediation: rotate the credential, then move it to "
                          f"auth-profiles.json or openclaw.json → integrations.[/]")

    if crits:
        sys.exit(2)


# ── config ────────────────────────────────────────────────────────────────────

@main.group()
def config() -> None:
    """Manage network configuration."""


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Print current network config as JSON."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    console.print_json(json.dumps(network, indent=2))


@config.command("set-primary")
@click.argument("bot_id")
@click.pass_context
def config_set_primary(ctx: click.Context, bot_id: str) -> None:
    """[DEPRECATED] Set the primary bot.

    Retired 2026-05-20. Every Evolve install now ships with a
    dedicated ``evo`` primary bot; the operator-facing chooser was
    removed from the wizard and the Settings UI. This CLI command
    still works for one release so existing automation / scripts
    don't break — but it surfaces a deprecation warning. Plan to
    remove it after the next deploy.

    If you genuinely need to migrate primary to a different bot
    (rare — usually means something deeper is broken), the
    network.json field is still ``primary`` + the bot record's
    ``role: "primary"``. Edit those directly and redeploy with
    ``sudo evolve-admin deploy``.
    """
    console.print(
        "[yellow]⚠ `evolve-admin config set-primary` is deprecated "
        "(2026-05-20).[/]\n"
        "  Every Evolve install now uses a dedicated `evo` primary "
        "bot; flipping primary at runtime is no longer a supported\n"
        "  pattern. This command still writes the change for "
        "backward compatibility but will be removed in a future "
        "release.\n"
        "  If you hit a case where this is genuinely needed, please "
        "open an issue describing why."
    )
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    network["primary"] = bot_id
    bots = network.setdefault("bots", {})
    if bot_id in bots:
        bots[bot_id]["role"] = "primary"
    save_network(network, network_path)
    console.print(f"[green]✓ Primary set to {bot_id}[/]")


@config.command("set-alert")
@click.option("--chat-id", required=True, help="Telegram chat ID")
@click.option("--channel", default="telegram", type=click.Choice(["telegram", "slack"]))
@click.pass_context
def config_set_alert(ctx: click.Context, chat_id: str, channel: str) -> None:
    """Configure the Telegram alert target."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    network["alerts"] = {"channel": channel, "chatId": chat_id}
    save_network(network, network_path)
    console.print(f"[green]✓ Alerts → {channel}:{chat_id}[/]")


# ── serve ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--port", default=5050, show_default=True, help="Port to listen on")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address (127.0.0.1 for local only, 0.0.0.0 for all interfaces including Tailscale)")
@click.option("--open", "open_browser", is_flag=True, default=False, help="Open browser after start")
@click.pass_context
def serve(ctx: click.Context, port: int, host: str, open_browser: bool) -> None:
    """Start the admin web UI. Reads only from shared dir — safe to run as any user with access to /Users/Shared/evolve."""
    network_path: Path = ctx.obj["network_path"]
    console.print(f"[bold]Starting Evolve Admin UI[/] on http://{host}:{port}/")
    console.print("[dim]Reads only from shared dir — safe to run as any user[/]")
    console.print("[dim]Ctrl+C to stop[/]")

    if open_browser:
        import subprocess as _sp
        import threading
        def _open_browser():
            import sys as _sys
            open_host = "127.0.0.1" if host == "0.0.0.0" else host
            if _sys.platform == "darwin":
                # Use macOS `open` command — avoids AppleScript errors from webbrowser module
                _sp.run(["open", f"http://{open_host}:{port}/"], check=False,
                        stderr=_sp.DEVNULL, stdout=_sp.DEVNULL)
            else:
                import webbrowser
                webbrowser.open(f"http://{open_host}:{port}/")
        threading.Timer(1.2, _open_browser).start()

    from .fd_limits import raise_nofile_limit
    raise_nofile_limit()  # EMFILE storm defense (2026-07-28 incident) — launchd soft default is 256
    from .telemetry import setup_access_logging
    access_log = setup_access_logging()
    if access_log is not None:
        console.print(f"[dim]  access log: {access_log} (rotating, 10MB × 5)[/]")

    from .web.server import create_app
    app = create_app(network_path)

    # Phase E.3.1 — second WSGI binding on a unix socket for the
    # evo↔admin-daemon channel. Spec:
    # docs/spec-evo-account-separation-2026-05-25.md §3.
    # The same Flask app serves both bindings; routes that should
    # only be reachable from trusted peers gate via @require_trusted_peer
    # in web.peer_auth.
    #
    # Best-effort: if the socket setup fails (permissions, stale lock,
    # libc unavailable for getpeereid, etc.) we log and continue with
    # the TCP binding only. The admin UI keeps working; evo's tools
    # that depend on the unix socket will fall back to direct fs reads
    # while we're still pre-E.2.b cutover.
    try:
        from .web.unix_socket_server import resolve_admin_socket_path
        from .web.unix_socket_server import start_in_background as _start_unix_server
        _socket_path = resolve_admin_socket_path(network_path)
        _unix_thread, _unix_server = _start_unix_server(app, _socket_path)
        console.print(f"[dim]  unix-socket binding active at {_socket_path}[/]")
    except Exception as _unix_exc:  # noqa: BLE001
        console.print(
            f"[yellow]  unix-socket binding skipped (non-fatal): "
            f"{type(_unix_exc).__name__}: {_unix_exc}[/]"
        )

    app.run(host=host, port=port, debug=False, threaded=True)


# ── connect: laptop-side SSH tunnel to the admin host ─────────────────────────

@main.command()
@click.option("--host", "host_opt", default=None,
              help="SSH host of the Evolve admin machine "
                   "(default: from network.json `adminBaseUrl`)")
@click.option("--user", "user_opt", default=None,
              help="SSH username on the admin host "
                   "(default: from network.json `admin_user`)")
@click.option("--remote-port", "remote_port", default=5050, show_default=True, type=int,
              help="Admin UI port on the remote machine")
@click.option("--local-port", "local_port", default=5050, show_default=True, type=int,
              help="Local port to forward the admin UI onto")
@click.option("--ssh-key", "ssh_key", default="~/.ssh/id_ed25519", show_default=True,
              help="SSH private key path on this machine")
@click.option("--once", is_flag=True, default=False,
              help="Run a one-shot foreground tunnel (Ctrl+C to stop) "
                   "instead of installing the persistent launchd agent")
@click.option("--uninstall", is_flag=True, default=False,
              help="Remove the persistent tunnel launchd agent and exit")
@click.option("--status", "show_status", is_flag=True, default=False,
              help="Print the tunnel agent's status and exit")
@click.option("--open/--no-open", "open_browser", default=True, show_default=True,
              help="Open the admin UI in your browser once the tunnel is up")
def connect(
    host_opt: str | None,
    user_opt: str | None,
    remote_port: int,
    local_port: int,
    ssh_key: str,
    once: bool,
    uninstall: bool,
    show_status: bool,
    open_browser: bool,
) -> None:
    """Open an SSH tunnel from this laptop to the Evolve admin UI.

    By default, installs a persistent launchd agent so the tunnel reconnects
    automatically on reboot or network drop, then opens the admin UI in your
    browser.

    \b
    Examples:
      # First run on your laptop — install + open browser
      evolve-admin connect --host mini
    \b
      # One-shot foreground tunnel (Ctrl+C to stop)
      evolve-admin connect --host mini --once
    \b
      # Check whether the tunnel agent is loaded
      evolve-admin connect --status
    \b
      # Stop the agent reconnecting
      evolve-admin connect --uninstall
    """
    from .tunnel import (
        install_persistent_tunnel,
        uninstall_persistent_tunnel,
        tunnel_status,
        run_one_shot_tunnel,
        TunnelConfig,
    )

    if show_status:
        info = tunnel_status(local_port=local_port)
        state = "🟢 up" if (info["loaded"] and info["listening_on_local"]) else \
                "🟡 loaded but not listening" if info["loaded"] else \
                "🔵 not installed"
        console.print(Panel.fit(
            f"  State:        {state}\n"
            f"  Installed:    {'yes' if info['installed'] else 'no'}\n"
            f"  launchd PID:  {info['pid'] or '—'}\n"
            f"  Local port:   {local_port} {'(in use)' if info['listening_on_local'] else '(free)'}\n"
            f"  Plist:        {info['plist_path']}\n"
            f"  Log:          {info['log_path']}",
            title="Evolve tunnel — status",
        ))
        return

    if uninstall:
        result = uninstall_persistent_tunnel()
        if result["removed"]:
            console.print(f"[green]✅[/] Removed {result['plist_path']}")
        else:
            console.print(f"[dim]No tunnel agent at {result['plist_path']} — nothing to remove[/]")
        return

    cfg: TunnelConfig = {
        "remote_port": remote_port,
        "local_port": local_port,
        "ssh_key": ssh_key,
    }
    if host_opt:
        cfg["remote_host"] = host_opt
    if user_opt:
        cfg["remote_user"] = user_opt

    if once:
        try:
            console.print(
                f"[bold]Opening one-shot tunnel[/] — Ctrl+C to stop.\n"
                f"  Local: http://localhost:{local_port}/"
            )
            rc = run_one_shot_tunnel(cfg)
            sys.exit(rc)
        except KeyboardInterrupt:
            sys.exit(0)
        except RuntimeError as e:
            console.print(f"[red]✗[/] {e}")
            sys.exit(1)

    try:
        info = install_persistent_tunnel(cfg)
    except RuntimeError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    bin_label = "autossh" if info["used_autossh"] else "ssh"
    key_note = "" if info["ssh_key_present"] else \
        "\n  [yellow]⚠️[/]  SSH key not found at the configured path; the agent " \
        "will fail to reconnect headlessly. Run `ssh-copy-id` to add your key."

    console.print(Panel.fit(
        f"[green]✅ Tunnel installed[/]\n\n"
        f"  Local:        http://localhost:{info['local_port']}/\n"
        f"  Forwards to:  {info['remote_user']+'@' if info['remote_user'] else ''}"
        f"{info['remote_host']}:{info['remote_port']}\n"
        f"  Reconnect:    via launchd ({bin_label})\n"
        f"  Plist:        {info['plist_path']}\n"
        f"  Log:          {info['log_path']}"
        f"{key_note}\n\n"
        f"[dim]Stop reconnecting: evolve-admin connect --uninstall[/]",
        title="Evolve — admin UI tunnel",
    ))

    if open_browser:
        import subprocess as _sp
        import sys as _sys
        url = f"http://localhost:{info['local_port']}/"
        if _sys.platform == "darwin":
            _sp.run(["open", url], check=False,
                    stderr=_sp.DEVNULL, stdout=_sp.DEVNULL)
        else:
            import webbrowser
            webbrowser.open(url)


@main.command()
def restart() -> None:
    """Restart the running admin server (launchd or direct process)."""
    from .service import restart as _restart
    ok, msg = _restart()
    if ok:
        console.print("[green]✓ Restarted[/]")
    else:
        console.print(f"[red]✗ {msg}[/]")
        sys.exit(1)


@main.command("gen-evo-glossary")
@click.option(
    "--check", is_flag=True,
    help="Verify the committed GLOSSARY.md matches what generation from "
         "the yaml would produce. Exits 1 on drift. Use in CI.",
)
@click.option(
    "--output", type=click.Path(), default=None,
    help="Output path. Default: packages/analyzer/evolve_bot/GLOSSARY.md "
         "(the committed default; pre-override).",
)
@click.option(
    "--apply-overrides", is_flag=True,
    help="Apply network.json::evo_glossary_overrides during render. "
         "Off by default (the committed GLOSSARY.md is the pre-override "
         "baseline that CI checks against). Used at deploy time.",
)
@click.pass_context
def gen_evo_glossary_cmd(
    ctx: click.Context, check: bool, output: str | None, apply_overrides: bool,
) -> None:
    """Generate / verify evo's pod glossary markdown.

    Single source of truth: packages/analyzer/evolve_bot/glossary.yaml.
    The deploy step regenerates the glossary with pod overrides
    applied; this CLI is for build-time regeneration + CI drift
    checks.

    Spec §3.7 brittleness mitigation — see
    docs/spec-evo-oc-native-2026-05-19.md.

    Examples:

    \b
      evolve-admin gen-evo-glossary                  # write the committed default
      evolve-admin gen-evo-glossary --check          # CI drift check
      evolve-admin gen-evo-glossary --apply-overrides  # write with pod overrides
    """
    from .evo import glossary as _glossary

    g = _glossary.load()
    if apply_overrides:
        try:
            network_path: Path = ctx.obj["network_path"]
            net = json.loads(network_path.read_text())
            g = _glossary.apply_overrides(g, net.get("evo_glossary_overrides") or {})
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]overrides not applied: {exc}[/]")

    rendered = _glossary.render_markdown(g)

    out_path = Path(output) if output else _glossary.DEFAULT_GLOSSARY_OUTPUT
    if check:
        if not out_path.exists():
            console.print(f"[red]missing: {out_path}[/]")
            sys.exit(1)
        existing = out_path.read_text()
        if existing != rendered:
            console.print(
                f"[red]drift: {out_path} doesn't match what generation "
                f"from glossary.yaml would produce[/]"
            )
            console.print(
                "  Re-run `evolve-admin gen-evo-glossary` to regenerate, "
                "or update glossary.yaml so the generated output is what's "
                "currently on disk."
            )
            sys.exit(1)
        console.print(f"[green]✓[/] {out_path} matches glossary.yaml")
        return

    out_path.write_text(rendered)
    console.print(
        f"[green]✓[/] wrote {out_path} ({len(rendered)} chars) "
        f"— {len(g.chips)} chips, {len(g.producers)} producers, "
        f"{len(g.generators)} generators"
    )


@main.command("sync-evo-tools")
@click.option(
    "--restart/--no-restart", default=True,
    help="Restart evo's gateway after sync (default: yes). Disable to "
         "batch with another restart, e.g. after a deploy.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would change without writing openclaw.json.",
)
@click.pass_context
def sync_evo_tools_cmd(ctx: click.Context, restart: bool, dry_run: bool) -> None:
    """Sync the evo tool registry into evo's openclaw.json.

    Idempotent. Writes mcp.servers.evo_tools so OC spawns the stdio MCP
    bridge (python3 -m evolve_admin.evo.tools) on session start. Use
    after adding new tools to the registry to make them visible to evo
    without running a full deploy.

    Requires sudo — writing evo's openclaw.json goes through
    /tmp staging + sudo /bin/cp.

    Examples:

    \b
      sudo evolve-admin sync-evo-tools           # write + restart evo's gateway
      sudo evolve-admin sync-evo-tools --no-restart   # write only
      sudo evolve-admin sync-evo-tools --dry-run      # plan only
    """
    if not dry_run and os.geteuid() != 0:
        console.print("[red]sync-evo-tools must be run with sudo (writes evo's openclaw.json)[/]")
        sys.exit(1)

    from .config import load_network
    from .evo.tools.deploy_integration import (
        EVO_TOOLS_SERVER_ID,
        build_server_block,
        ensure_evo_tools_mcp_server,
        _primary_bot_id,
        _bot_user_for,
        _read_bot_oc_config,
    )

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    primary = _primary_bot_id(network)
    if primary is None:
        console.print("[red]No primary bot resolved from network.json — nothing to sync.[/]")
        sys.exit(1)

    if dry_run:
        bot_user = _bot_user_for(primary, network)
        shared_dir = network.get("sharedDir") or "/Users/Shared/evolve"
        desired = build_server_block(
            venv_python="/Users/Shared/evolve-venv/bin/python3",
            shared_dir=shared_dir,
            network_path=str(network_path),
        )
        oc, err = _read_bot_oc_config(primary, bot_user)
        if oc is None:
            console.print(f"[red]read failed: {err}[/]")
            sys.exit(1)
        current = ((oc.get("mcp") or {}).get("servers") or {}).get(EVO_TOOLS_SERVER_ID)
        if current == desired:
            console.print(f"  [green]✓[/] {primary}: mcp.servers.{EVO_TOOLS_SERVER_ID} already current")
        else:
            verb = "create" if current is None else "update"
            console.print(f"  Would {verb} mcp.servers.{EVO_TOOLS_SERVER_ID} on {primary}:")
            import json as _json
            console.print("    " + _json.dumps(desired, indent=2).replace("\n", "\n    "))
        return

    changed, status = ensure_evo_tools_mcp_server(
        primary, network, network_path=network_path,
    )
    if status.startswith("error:"):
        console.print(f"  [red]✗[/] {primary}: {status}")
        sys.exit(1)
    icon = "✓" if changed else "·"
    color = "green" if changed else "dim"
    console.print(f"  [{color}]{icon}[/] {primary}: mcp.servers.{EVO_TOOLS_SERVER_ID} {status}")

    if restart and changed:
        from .deploy import restart_gateway
        console.print(f"Restarting {primary}'s gateway to pick up new tool surface…")
        try:
            restart_gateway(primary)
            console.print(f"  [green]✓[/] {primary} gateway restarted")
        except Exception as exc:
            console.print(f"  [red]✗[/] restart failed: {exc}")
            sys.exit(1)


@main.command("restart-gateways")
@click.argument("bots", nargs=-1, metavar="[BOT ...]")
@click.pass_context
def restart_gateways(ctx: click.Context, bots: tuple[str, ...]) -> None:
    """Restart all (or specific) OpenClaw gateways.

    No args: restart every gateway in network.json ``members``; pass names to scope.

    Requires sudo — each restart kills any orphaned port process then does
    ``launchctl kickstart -k`` on the system daemon.

    Examples:

    \b
      sudo evolve-admin restart-gateways           # all bots
      sudo evolve-admin restart-gateways team_bot_a        # just team_bot_a
      sudo evolve-admin restart-gateways team_bot_a admin_bot  # team_bot_a and admin_bot
    """
    if os.geteuid() != 0:
        console.print("[red]restart-gateways must be run with sudo[/]")
        sys.exit(1)

    from .config import load_network
    from .deploy import restart_all_gateways

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)

    # Roster = network membership, NOT a literal "evolve" append — that phantom
    # crash-looped a re-rendered gateway (EVO-GATEWAY-RESIDUE-RERENDER). #3279
    _members = network.get("members")
    _members = _members if isinstance(_members, list) and _members else None
    all_bots: list[str] = [m for m in (_members or network.get("bots", {})) if m]

    targets = list(bots) if bots else all_bots

    unknown = [b for b in targets if b not in all_bots]
    if unknown:
        console.print(f"[red]Unknown bot(s): {', '.join(unknown)}[/]")
        console.print(f"  Known: {', '.join(all_bots)}")
        sys.exit(1)

    console.print(f"Restarting {len(targets)} gateway(s): {', '.join(targets)}")
    results = restart_all_gateways(targets)
    any_err = False
    for bot_id, outcome in results.items():
        if outcome == "ok":
            console.print(f"  [green]✓[/] {bot_id}")
        else:
            console.print(f"  [red]✗[/] {bot_id}: {outcome}")
            any_err = True
    if any_err:
        sys.exit(1)


# ── recovery: panic-button + per-bot rollback ────────────────────────────────
#
# Sprint pillar B2.e. Two capabilities, both reversible, neither destructive:
#
#   pause-all / resume-all    — disable / re-enable every bot gateway pod-wide
#   rollback / list-rollbacks / reverse-rollback
#                             — revert a bot's openclaw.json to a daily-backup
#                               commit; every rollback is itself reversible.
#
# All operations write audit records under {shared_dir}/recovery/ so an
# operator can always reconstruct "who did what when".


def _recovery_shared_dir(network: dict) -> Path:
    """Resolve the shared-dir for recovery state from network config.

    Falls back to /Users/Shared/evolve (the production default) when
    network.json doesn't declare a sharedDir override."""
    raw = (network or {}).get("sharedDir") or "/Users/Shared/evolve"
    return Path(raw)


@main.command("pause-all")
@click.option("--reason", default="operator pause", help="Free-form reason recorded in the audit log.")
@click.option("--initiated-by", default="cli", help="Who initiated this pause (defaults to 'cli'; set to a user id if known).")
@click.option("--dry-run", is_flag=True, help="Plan only — print what would happen; touch nothing.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON result instead of human-readable output.")
@click.pass_context
def pause_all_cmd(ctx: click.Context, reason: str, initiated_by: str, dry_run: bool, as_json: bool) -> None:
    """Disable all bot gateways pod-wide (panic button).

    Writes a pause-state flag at {shared_dir}/recovery/pause-state.json,
    then bootouts every bot gateway via launchctl. Reversible via
    ``resume-all``. Bot data/state is preserved — only the running
    gateway daemons are stopped.

    Typically completes in well under 30 seconds for a 7-bot pod.
    """
    from . import recovery
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _recovery_shared_dir(network)
    result = recovery.pause_all(
        reason=reason, initiated_by=initiated_by,
        shared_dir=shared_dir, network=network, dry_run=dry_run,
    )
    _print_recovery_pause_result(result, as_json=as_json)
    if not result.ok:
        sys.exit(1)


@main.command("resume-all")
@click.option("--initiated-by", default="cli")
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def resume_all_cmd(ctx: click.Context, initiated_by: str, dry_run: bool, as_json: bool) -> None:
    """Re-enable all bot gateways (reverse of ``pause-all``)."""
    from . import recovery
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _recovery_shared_dir(network)
    result = recovery.resume_all(
        initiated_by=initiated_by, shared_dir=shared_dir,
        network=network, dry_run=dry_run,
    )
    _print_recovery_pause_result(result, as_json=as_json)
    if not result.ok:
        sys.exit(1)


@main.command("recovery-status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def recovery_status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Print pause-state + recent rollback history."""
    from . import recovery
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _recovery_shared_dir(network)
    status_dict = recovery.recovery_status(shared_dir=shared_dir, network=network)
    if as_json:
        print(json.dumps(status_dict, indent=2, sort_keys=True))
        return
    paused = status_dict.get("paused")
    if paused:
        ps = status_dict.get("pause_state") or {}
        console.print(f"[red]Pod is PAUSED[/] — since {ps.get('paused_at', '?')} "
                      f"(reason: {ps.get('reason', '?')}, by: {ps.get('initiated_by', '?')})")
    else:
        console.print("[green]Pod is running[/] (not paused).")
    rolls = status_dict.get("recent_rollbacks") or []
    if rolls:
        console.print(f"\nRecent rollbacks ({len(rolls)}):")
        for r in rolls[:10]:
            tag = "[green]OK[/]" if r.get("ok") else "[red]FAIL[/]"
            console.print(f"  {tag}  {r.get('started_at', '?')}  {r.get('bot_id', '?')}  → {r.get('target_commit', '?')[:8]}  ({r.get('rollback_id', '?')})")
    else:
        console.print("\nNo rollback history.")


@main.command("rollback")
@click.argument("bot_id")
@click.argument("target")
@click.option("--initiated-by", default="cli")
@click.option("--skip-restart", is_flag=True, help="Skip the gateway kickstart after writing (debugging only).")
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def rollback_cmd(
    ctx: click.Context,
    bot_id: str,
    target: str,
    initiated_by: str,
    skip_restart: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Revert a bot's openclaw.json to a previous state.

    TARGET is either a YYYY-MM-DD date (picks the most recent backup
    commit on or before end-of-day UTC of that date) or a 7-40 char
    commit SHA from the bot's workspace git history.

    The pre-rollback config is snapshotted to
    ``{shared_dir}/recovery/rollbacks/<rollback_id>.json`` so the
    rollback itself is reversible via ``reverse-rollback``.

    Examples:

    \b
      sudo evolve-admin rollback team_bot_a 2026-05-09
      sudo evolve-admin rollback admin_bot a1b2c3d
      sudo evolve-admin rollback team_bot_a 2026-05-09 --dry-run
    """
    from . import recovery
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _recovery_shared_dir(network)
    result = recovery.rollback_bot(
        bot_id=bot_id, target=target,
        network=network, shared_dir=shared_dir,
        initiated_by=initiated_by,
        skip_restart=skip_restart,
        dry_run=dry_run,
    )
    _print_rollback_result(result, as_json=as_json)
    if not result.ok:
        sys.exit(1)


@main.command("list-rollback-points")
@click.argument("bot_id")
@click.option("--limit", default=14, type=int, help="Max commits to list (default 14).")
@click.option("--json", "as_json", is_flag=True)
@click.option("--all", "include_all", is_flag=True, help="Include non-backup commits as well.")
@click.pass_context
def list_rollback_points_cmd(ctx: click.Context, bot_id: str, limit: int, as_json: bool, include_all: bool) -> None:
    """List candidate rollback dates/commits for a bot."""
    from . import recovery
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    points = recovery.list_rollback_points(
        bot_id, network=network, limit=limit, only_backup=not include_all,
    )
    if as_json:
        print(json.dumps([p.to_dict() for p in points], indent=2))
        return
    if not points:
        console.print(f"[yellow]No backup commits found for {bot_id}.[/]")
        console.print("  This usually means backup.py hasn't run for this bot yet,")
        console.print("  or backupRepoUrl isn't configured in network.json.")
        return
    table = Table(title=f"Rollback points for {bot_id}", show_lines=False)
    table.add_column("Commit", style="cyan", no_wrap=True)
    table.add_column("Date (local)")
    table.add_column("Has config", justify="center")
    table.add_column("Subject")
    for p in points:
        has = "✓" if p.has_openclaw_json else "—"
        table.add_row(p.commit_sha[:8], p.commit_date_local, has, p.summary[:80])
    console.print(table)


@main.command("list-rollbacks")
@click.option("--bot", "bot_id", default=None, help="Only show rollbacks for this bot.")
@click.option("--limit", default=20, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_rollbacks_cmd(ctx: click.Context, bot_id: str | None, limit: int, as_json: bool) -> None:
    """List past rollback operations (audit history)."""
    from . import recovery
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _recovery_shared_dir(network)
    entries = recovery.list_rollback_history(
        shared_dir=shared_dir, bot_id=bot_id, limit=limit,
    )
    if as_json:
        print(json.dumps(entries, indent=2))
        return
    if not entries:
        console.print("[dim]No rollback history.[/]")
        return
    table = Table(title="Rollback history", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Bot")
    table.add_column("Target", no_wrap=True)
    table.add_column("Started")
    table.add_column("OK", justify="center")
    table.add_column("Reversed")
    for e in entries:
        ok = "[green]✓[/]" if e.get("ok") else "[red]✗[/]"
        rev = e.get("reversed_by_rollback_id", "") or "—"
        table.add_row(
            e.get("rollback_id", "")[:12],
            e.get("bot_id", ""),
            (e.get("target_commit") or "")[:12],
            e.get("started_at", "")[:19],
            ok,
            rev[:12],
        )
    console.print(table)


@main.command("reverse-rollback")
@click.argument("rollback_id")
@click.option("--initiated-by", default="cli")
@click.option("--skip-restart", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reverse_rollback_cmd(
    ctx: click.Context,
    rollback_id: str,
    initiated_by: str,
    skip_restart: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Undo a previous rollback by restoring its pre-rollback snapshot."""
    from . import recovery
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _recovery_shared_dir(network)
    result = recovery.reverse_rollback(
        rollback_id, network=network, shared_dir=shared_dir,
        initiated_by=initiated_by, skip_restart=skip_restart, dry_run=dry_run,
    )
    _print_rollback_result(result, as_json=as_json)
    if not result.ok:
        sys.exit(1)


# ── Circuit breakers (Phase 2: state store + manual trip) ───────────────────
#
# See docs/spec-circuit-breakers-2026-05-21.md. Phase 2 lands the manual
# trip primitive backed by breakers.store. No enforcement yet — writing a
# breaker file marks intent; Phase 3 wires up heal.py + the TS plugin to
# act on it.


def _breakers_store():
    """Lazy import of breakers.store."""
    from breakers import store as _store  # noqa: WPS433
    return _store


@main.group("breaker")
def breaker_group() -> None:
    """Manage per-bot and pod-wide circuit breakers.

    Two breaker types:

      cost — blocks background activity (heartbeats, crons, auto agents).
             User messaging through normal channels still works.
      full — full halt; bot gateway is taken down. Equivalent to per-bot
             pause-all. Reset requires admin context.

    Use scope=pod to operate pod-wide. See `breaker trip --help` for the
    duration format ("1h", "24h", "7d", "indefinite").
    """


def _format_breaker_row(rec, *, now=None) -> str:
    from datetime import datetime as _dt, timezone as _tz
    store = _breakers_store()
    state = "EXPIRED" if store.is_expired(rec, now=now) else "tripped"
    color = "[yellow]" if state == "EXPIRED" else "[red]"
    expires = rec.expires_at or "indefinite"
    return (
        f"  {color}{rec.bot_id}/{rec.type}[/]  {state}  "
        f"since {rec.tripped_at}  expires {expires}  "
        f"by {rec.initiated_by}  reason={rec.reason!r}"
    )


def _breakers_shared_dir(ctx: click.Context) -> Path:
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    return _recovery_shared_dir(network)


@breaker_group.command("trip")
@click.argument("scope")
@click.argument("breaker_type", type=click.Choice(["cost", "full"]))
@click.option("--duration", "-d", default="24h",
              help='Trip duration: "1h"/"4h"/"24h"/"7d"/"30m" or "indefinite". '
                   "Default 24h.")
@click.option("--reason", "-r", default="manual trip", help="Why this trip.")
@click.option("--initiated-by", default="cli",
              help='Audit-log actor ID (e.g. "admin:pod_admin"). Defaults to "cli".')
@click.option("--motivating-signal", multiple=True,
              help="Signal ID this trip responds to (repeatable).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_trip_cmd(
    ctx: click.Context, scope: str, breaker_type: str, duration: str,
    reason: str, initiated_by: str,
    motivating_signal: tuple[str, ...], as_json: bool,
) -> None:
    """Trip a breaker. SCOPE is a bot id or "pod" (pod-wide)."""
    store = _breakers_store()
    try:
        dur = store.parse_duration(duration)
    except ValueError as e:
        console.print(f"[red]Bad --duration: {e}[/]")
        sys.exit(2)

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _breakers_shared_dir(ctx)
    try:
        rec = store.trip(
            shared_dir=shared_dir, scope=scope, breaker_type=breaker_type,
            duration=dur, initiated_by=initiated_by, reason=reason,
            motivating_signals=list(motivating_signal),
        )
    except ValueError as e:
        console.print(f"[red]Trip rejected: {e}[/]")
        sys.exit(2)

    # Phase 3a: synchronously enforce. For type=full this boots out the
    # affected gateway(s); for type=cost (partial Phase 3b) this stashes
    # and removes agents.defaults.heartbeat.every from openclaw.json
    # and kickstarts the gateway. The stash lives in
    # ``{shared_dir}/breakers/<bot>/heartbeat-stash.json`` and is
    # restored on the matching reset.
    from . import breakers_enforce
    try:
        enforce_result = breakers_enforce.enforce_trip(
            scope=scope, breaker_type=breaker_type, network=network,
            shared_dir=shared_dir,
            reason=rec.reason, expires_at_iso=rec.expires_at,
        )
    except ValueError as e:
        # Unknown bot — file was written but we can't enforce. Surface
        # the inconsistency to the operator; don't silently swallow.
        console.print(f"[red]State written, enforce FAILED: {e}[/]")
        console.print("  [yellow]The breaker file exists but the gateway "
                      "was not bootout. Reset the breaker, or fix the "
                      "scope.[/]")
        sys.exit(3)

    if as_json:
        print(json.dumps({
            "trip": rec.to_json(),
            "enforce": enforce_result.to_dict(),
        }, indent=2, sort_keys=True))
        if not enforce_result.ok:
            sys.exit(1)
        return

    console.print(f"[red]●[/] tripped: {scope}/{breaker_type} "
                  f"(trip_id={rec.trip_id[:8]}, "
                  f"expires={rec.expires_at or 'indefinite'})")
    console.print(f"  reason: {rec.reason}")

    if enforce_result.no_op:
        console.print(
            f"  [dim]{enforce_result.no_op_reason}.[/]"
        )
    else:
        _print_enforce_result(enforce_result)
    if not enforce_result.ok:
        sys.exit(1)


@breaker_group.command("reset")
@click.argument("scope")
@click.argument("breaker_type", type=click.Choice(["cost", "full"]))
@click.option("--reason", "-r", default="manual reset")
@click.option("--initiated-by", default="cli")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_reset_cmd(
    ctx: click.Context, scope: str, breaker_type: str, reason: str,
    initiated_by: str, as_json: bool,
) -> None:
    """Reset (clear) a breaker. No-op if not tripped."""
    store = _breakers_store()
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _breakers_shared_dir(ctx)
    try:
        prior = store.reset(
            shared_dir=shared_dir, scope=scope, breaker_type=breaker_type,
            initiated_by=initiated_by, reason=reason,
        )
    except ValueError as e:
        console.print(f"[red]Reset rejected: {e}[/]")
        sys.exit(2)

    # Phase 3a: synchronously bootstrap the affected gateway(s) if this
    # was an L2 reset; or restore agents.defaults.heartbeat.every from
    # the stash if it was an L1 cost reset.
    enforce_result = None
    if prior is not None:
        from . import breakers_enforce
        try:
            enforce_result = breakers_enforce.enforce_reset(
                scope=scope, breaker_type=breaker_type, network=network,
                shared_dir=shared_dir,
            )
        except ValueError as e:
            console.print(f"[red]State cleared, restart FAILED: {e}[/]")
            console.print("  [yellow]The breaker file is deleted but the "
                          "gateway may still be down. Run 'evolve-admin "
                          "restart-gateways' or fix the scope.[/]")
            sys.exit(3)

    if as_json:
        print(json.dumps({
            "reset": prior.to_json() if prior else None,
            "enforce": enforce_result.to_dict() if enforce_result else None,
        }, indent=2, sort_keys=True))
        if enforce_result and not enforce_result.ok:
            sys.exit(1)
        return
    if prior is None:
        console.print(f"[green]✓[/] {scope}/{breaker_type} not tripped — nothing to reset")
        return
    console.print(f"[green]●[/] reset: {scope}/{breaker_type} "
                  f"(was trip_id={prior.trip_id[:8]})")
    if enforce_result and enforce_result.no_op:
        console.print(f"  [dim]{enforce_result.no_op_reason}.[/]")
    elif enforce_result:
        _print_enforce_result(enforce_result)
    if enforce_result and not enforce_result.ok:
        sys.exit(1)


@breaker_group.command("extend")
@click.argument("scope")
@click.argument("breaker_type", type=click.Choice(["cost", "full"]))
@click.option("--by", "by_duration", required=True,
              help='Extend by this much: "1h", "24h", "7d", etc.')
@click.option("--initiated-by", default="cli")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_extend_cmd(
    ctx: click.Context, scope: str, breaker_type: str, by_duration: str,
    initiated_by: str, as_json: bool,
) -> None:
    """Push a tripped breaker's expiry further out."""
    store = _breakers_store()
    try:
        delta = store.parse_duration(by_duration)
    except ValueError as e:
        console.print(f"[red]Bad --by: {e}[/]")
        sys.exit(2)
    if delta is None:
        console.print("[red]--by cannot be 'indefinite'; reset and re-trip instead.[/]")
        sys.exit(2)

    shared_dir = _breakers_shared_dir(ctx)
    try:
        rec = store.extend(
            shared_dir=shared_dir, scope=scope, breaker_type=breaker_type,
            additional=delta, initiated_by=initiated_by,
        )
    except ValueError as e:
        console.print(f"[red]Extend rejected: {e}[/]")
        sys.exit(2)

    if as_json:
        print(json.dumps(
            {"extended": rec.to_json() if rec else None}, indent=2, sort_keys=True,
        ))
        return
    if rec is None:
        console.print(f"[yellow]No active trip on {scope}/{breaker_type}.[/]")
        sys.exit(1)
    else:
        console.print(f"[green]●[/] extended {scope}/{breaker_type} → "
                      f"expires {rec.expires_at}")


@breaker_group.command("status")
@click.option("--all", "show_all", is_flag=True,
              help="Include expired (not-yet-reaped) trips.")
@click.option("--audit-days", type=int, default=7,
              help="Show audit-log entries from the last N days (0 = skip).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_status_cmd(
    ctx: click.Context, show_all: bool, audit_days: int, as_json: bool,
) -> None:
    """Print currently-tripped breakers + recent audit history."""
    store = _breakers_store()
    shared_dir = _breakers_shared_dir(ctx)
    trips = store.list_all(shared_dir) if show_all else store.list_active(shared_dir)
    audit = store.read_audit_log(shared_dir, days=audit_days) if audit_days > 0 else []

    if as_json:
        print(json.dumps({
            "trips": [r.to_json() for r in trips],
            "audit": audit,
        }, indent=2, sort_keys=True))
        return

    if not trips:
        console.print("[green]No active circuit breakers.[/]")
    else:
        console.print(f"[bold]{len(trips)} active breaker(s):[/]")
        for r in trips:
            console.print(_format_breaker_row(r))

    if audit:
        console.print(f"\n[bold]Recent audit ({len(audit)} entries, last {audit_days}d):[/]")
        for entry in audit[:20]:
            action = entry.get("action", "?")
            ts = entry.get("timestamp", "?")
            scope = entry.get("scope", "?")
            etype = entry.get("type", "?")
            by = entry.get("initiated_by", "?")
            console.print(f"  {ts}  {action:7s}  {scope}/{etype}  by {by}")


def _print_enforce_result(result) -> None:
    """Pretty-print a breakers_enforce.EnforceResult."""
    if result.no_op:
        console.print(f"  [dim]{result.no_op_reason}.[/]")
        return
    header = "[green]✓[/]" if result.ok else "[red]✗[/]"
    suffix = " (DRY-RUN)" if result.dry_run else ""
    elapsed = f"{result.elapsed_ms} ms" if result.elapsed_ms is not None else "?"
    console.print(f"  {header} {result.action} {result.breaker_type}"
                  f"{suffix} — {elapsed}, {len(result.per_bot)} bot(s)")
    for r in result.per_bot:
        sym = "[green]✓[/]" if r.ok else "[red]✗[/]"
        tag = " (skipped: " + r.skip_reason + ")" if r.skipped else ""
        line = f"    {sym} {r.bot_id}  rc={r.rc}{tag}"
        if r.stderr:
            line += f"   stderr={r.stderr!r}"
        console.print(line)


def _print_recovery_pause_result(result, as_json: bool) -> None:
    """Pretty-print a PauseResult dataclass."""
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    header = "[green]✓[/]" if result.ok else "[red]✗[/]"
    label = result.action
    suffix = " (DRY-RUN)" if result.dry_run else ""
    console.print(f"{header} {label}{suffix} — {result.elapsed_ms} ms, "
                  f"{len(result.per_bot)} bot(s)")
    for r in result.per_bot:
        sym = "[green]✓[/]" if r.ok else "[red]✗[/]"
        tag = " (skipped: " + r.skip_reason + ")" if r.skipped else ""
        line = f"  {sym} {r.bot_id}  rc={r.rc}  {r.elapsed_ms}ms{tag}"
        if r.stderr:
            line += f"   stderr={r.stderr!r}"
        console.print(line)


def _print_rollback_result(result, as_json: bool) -> None:
    """Pretty-print a RollbackResult dataclass."""
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    header = "[green]✓[/]" if result.ok else "[red]✗[/]"
    suffix = " (DRY-RUN)" if result.dry_run else ""
    console.print(f"{header} rollback{suffix} {result.bot_id} → {result.target_commit[:8] if result.target_commit else '?'}")
    console.print(f"    id: {result.rollback_id}")
    console.print(f"    msg: {result.message}")
    if result.gateway_restart_ok is not None:
        rs = "[green]ok[/]" if result.gateway_restart_ok else "[red]fail[/]"
        console.print(f"    gateway-restart: {rs} ({result.gateway_restart_msg})")
    if result.pre_rollback_config_path:
        console.print(f"    record: {result.pre_rollback_config_path}")


# ── application ──────────────────────────────────────────────────────────────

_MANIFEST_PROMPT = """\
You are generating an application manifest for an OpenClaw AI assistant.
This manifest is used for quality assurance, regression testing, and Better Engine adaptation.

Application detected:
- Name: {name}
- Description: {description}
- Evidence files found in workspace: {evidence_files}
- Bot purpose (from SOUL.md preview): {soul_preview}

Generate a complete application manifest with these sections:

1. IDENTITY
   - purpose: One clear sentence: "This application exists to [do X] for [user] so that [outcome]."
   - scope_includes: 3-5 specific things this application handles
   - scope_excludes: 2-3 things it explicitly does NOT handle
   - user: who this is for

2. SUCCESS_CRITERIA
   - observable_outcomes: 3-4 specific, observable things that indicate it's working
     (specific behaviors you can verify in a conversation or file, not vague goals)
   - failure_signals: 3-4 specific things that would indicate it's broken
   - quality_bar: object with "minimum" and "excellent" string fields

3. CONSTRAINTS
   - privacy: any data that must never leave this application
   - safety: any hard rules that always apply
   - dependencies: files or systems this relies on
   - boundaries: what this explicitly doesn't handle

4. EXAMPLE_TRIGGERS
   - 3-5 example user messages that invoke this application

5. TEST_CASES
   - 2-3 basic test cases, each with: id, name, trigger, expected_behavior, pass_criteria,
     last_run (null), last_result (null), last_notes (null)

Return ONLY valid JSON matching this schema (no explanation, no markdown):
{{
  "identity": {{
    "purpose": "...",
    "scope_includes": ["..."],
    "scope_excludes": ["..."],
    "user": "..."
  }},
  "success_criteria": {{
    "observable_outcomes": ["..."],
    "failure_signals": ["..."],
    "quality_bar": {{"minimum": "...", "excellent": "..."}}
  }},
  "constraints": {{
    "privacy": ["..."],
    "safety": ["..."],
    "dependencies": ["..."],
    "boundaries": ["..."]
  }},
  "example_triggers": ["..."],
  "test_cases": [
    {{
      "id": "tc-1",
      "name": "...",
      "trigger": "...",
      "expected_behavior": "...",
      "pass_criteria": "...",
      "last_run": null,
      "last_result": null,
      "last_notes": null
    }}
  ]
}}
"""


def _generate_full_manifest_with_llm(d: Any, bot_id: str, workspace: "Path | None" = None) -> dict:
    """
    Call the tier3 model to generate a full 4-section RSI manifest.
    Returns a dict with identity/success_criteria/constraints/example_triggers/test_cases,
    or empty dict on failure.
    """
    import json as _json, subprocess as _sp
    from pathlib import Path as _Path

    from .applications.reviewer import _resolve_tier3

    soul_preview = "(SOUL.md not available)"
    if workspace:
        soul_path = _Path(workspace) / "SOUL.md"
        try:
            soul_preview = soul_path.read_text()[:500]
        except OSError:
            pass

    evidence_list = ", ".join(d.evidence_files[:8]) if d.evidence_files else "none"
    prompt = _MANIFEST_PROMPT.format(
        name=d.name,
        description=d.description or d.name,
        evidence_files=evidence_list,
        soul_preview=soul_preview,
    )

    try:
        result = _sp.run(
            ["openclaw", "run", "--model", _resolve_tier3(),
             "--max-turns", "1", "--message", prompt],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            return {}
        text = result.stdout.strip()
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start == -1 or json_end <= json_start:
            return {}
        data = _json.loads(text[json_start:json_end])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _build_manifest_dict(d: Any, bot_id: str, enriched: dict, now: str) -> dict:
    """Build a full 4-section manifest dict from a DetectedApplication and LLM enrichment."""
    identity = enriched.get("identity") or {
        "purpose": f"This application exists to help {bot_id} with {d.description or d.name}.",
        "scope_includes": d.suggested_goals[:5] if d.suggested_goals else [],
        "scope_excludes": [],
        "user": bot_id,
    }
    success_criteria = enriched.get("success_criteria") or {
        "observable_outcomes": [],
        "failure_signals": d.suggested_tests[:4] if d.suggested_tests else [],
        "quality_bar": {"minimum": "", "excellent": ""},
    }
    constraints = enriched.get("constraints") or {
        "privacy": d.suggested_privacy if d.suggested_privacy else [],
        "safety": [],
        "dependencies": d.evidence_files[:8],
        "boundaries": [],
    }
    return {
        "id": d.id,
        "name": d.name,
        "bot_id": bot_id,
        "source": d.source,
        "confidence": d.confidence,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        # 4-section RSI fields
        "identity": identity,
        "success_criteria": success_criteria,
        "constraints": constraints,
        "example_triggers": enriched.get("example_triggers", []),
        "test_cases": enriched.get("test_cases", []),
        "satisfaction": {"score": None, "notes": None, "rated_at": None},
        "improvement_history": [],
        "open_questions": [],
        "known_issues": [],
        "evidence_files": d.evidence_files,
        "tags": [],
        "schema_version": 3,
        # Legacy flat fields kept for backward compat
        "description": d.description or d.name,
        "satisfaction_score": None,
        "satisfaction_notes": None,
        "last_tested": None,
        "last_test_result": None,
    }


def _apply_enrichment(existing: dict, enriched: dict, d: Any, bot_id: str, now: str) -> None:
    """Merge 4-section LLM enrichment into an existing manifest dict (in-place)."""
    if enriched.get("identity"):
        existing["identity"] = enriched["identity"]
    else:
        existing.setdefault("identity", {
            "purpose": existing.get("purpose", ""),
            "scope_includes": existing.get("goals", [])[:5],
            "scope_excludes": [],
            "user": bot_id,
        })
    if enriched.get("success_criteria"):
        existing["success_criteria"] = enriched["success_criteria"]
    else:
        existing.setdefault("success_criteria", {
            "observable_outcomes": [],
            "failure_signals": d.suggested_tests[:4] if d.suggested_tests else [],
            "quality_bar": {"minimum": "", "excellent": ""},
        })
    if enriched.get("constraints"):
        existing["constraints"] = enriched["constraints"]
    else:
        existing.setdefault("constraints", {
            "privacy": existing.get("privacy_constraints", []),
            "safety": [],
            "dependencies": existing.get("evidence_files", d.evidence_files)[:8],
            "boundaries": [],
        })
    if enriched.get("example_triggers"):
        existing["example_triggers"] = enriched["example_triggers"]
    if enriched.get("test_cases"):
        existing["test_cases"] = enriched["test_cases"]
    existing.setdefault("satisfaction", {"score": None, "notes": None, "rated_at": None})
    existing.setdefault("improvement_history", [])
    existing.setdefault("open_questions", [])
    existing.setdefault("known_issues", [])
    existing["schema_version"] = 3
    existing["updated_at"] = now


@main.group()
def application() -> None:
    """Manage application manifests."""


@application.command("migrate-to-workspace")
@click.option("--apply", is_flag=True, default=False,
              help="Actually move files (default: dry run)")
@click.option(
    "--keep-shared", is_flag=True, default=False,
    help="Leave the shared-side files in place after copying (default: delete)",
)
@click.pass_context
def application_migrate_to_workspace(
    ctx: click.Context, apply: bool, keep_shared: bool,
) -> None:
    """One-time migration: move manifests from /Users/Shared/evolve/applications/
    to each bot's workspace/manifests/.

    Per the architectural change in PR #(this one), manifests are per-bot
    state and live at /Users/<bot>/.openclaw/workspace/manifests/<app>.json.
    The legacy shared-side location was a partial duplicate that drifted
    out of sync (team_bot_c's case: 10 manifests bot-side, 0 shared-side).

    For each bot dir under /Users/Shared/evolve/applications/:
      - Walks every *.json manifest (skips .scan-status and _history).
      - If the bot-side counterpart is absent OR older: copies shared
        → bot-side via /tmp staging + sudo cp (chowns to bot user).
      - If the bot-side counterpart is newer: leaves both, logs a
        warning so the operator can manually resolve.
      - Unless --keep-shared: deletes the shared-side file after copy.

    Idempotent. Safe to re-run.
    """
    import json as _json
    import shutil as _shutil
    import subprocess as _sub
    import tempfile as _temp

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    shared_apps = shared_dir / "applications"

    if not shared_apps.exists():
        console.print(f"[green]No shared-side manifests at {shared_apps} — nothing to migrate.[/]")
        return

    from .config import get_bot_user, get_bot_workspace

    total_copied = 0
    total_skipped = 0
    total_conflicts = 0
    total_deleted = 0

    for bot_dir in sorted(shared_apps.iterdir()):
        if not bot_dir.is_dir():
            continue
        bot_id = bot_dir.name
        bot_user = get_bot_user(bot_id, network)
        bot_workspace = get_bot_workspace(bot_id, user=bot_user)
        if bot_workspace is None:
            console.print(f"[yellow]Skipping {bot_id}: no workspace resolvable[/]")
            continue
        bot_manifests = bot_workspace / "manifests"

        console.print(f"\n[bold]{bot_id}[/] → {bot_manifests}")

        for src in sorted(bot_dir.glob("*.json")):
            if src.name.startswith(".") or "_history" in src.name:
                continue
            dst = bot_manifests / src.name

            try:
                src_mtime = src.stat().st_mtime
            except OSError:
                src_mtime = 0.0
            try:
                dst_mtime = dst.stat().st_mtime if dst.exists() else 0.0
            except OSError:
                dst_mtime = 0.0

            if dst.exists() and dst_mtime > src_mtime:
                console.print(f"  [dim]skip[/] {src.name}: bot-side newer")
                total_conflicts += 1
                continue
            if dst.exists() and dst_mtime == src_mtime:
                console.print(f"  [dim]skip[/] {src.name}: identical mtime")
                total_skipped += 1
                if apply and not keep_shared:
                    try:
                        src.unlink()
                        total_deleted += 1
                    except OSError as e:
                        console.print(f"    [yellow]could not delete shared: {e}[/]")
                continue

            action = "[cyan]copy[/]" if not dst.exists() else "[yellow]overwrite[/]"
            console.print(f"  {action} {src.name}")
            if not apply:
                total_copied += 1
                continue

            # Stage to /tmp then sudo cp + chown to the bot user.
            content = src.read_bytes()
            fd, tmp_path = _temp.mkstemp(
                dir="/tmp", prefix="evolve-manifest-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                # mkdir of the manifests/ dir via sudo if it doesn't exist
                if not bot_manifests.exists():
                    _sub.run(
                        ["sudo", "/bin/mkdir", "-p", str(bot_manifests)],
                        capture_output=True, text=True, timeout=10,
                    )
                    _sub.run(
                        ["sudo", get_profile().chown, bot_user, str(bot_manifests)],
                        capture_output=True, text=True, timeout=10,
                    )
                r = _sub.run(
                    ["sudo", "/bin/cp", tmp_path, str(dst)],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode != 0:
                    console.print(f"    [red]cp failed: {r.stderr.strip()[:200]}[/]")
                    continue
                _sub.run(
                    ["sudo", get_profile().chown, bot_user, str(dst)],
                    capture_output=True, text=True, timeout=10,
                )
                total_copied += 1
                if not keep_shared:
                    try:
                        src.unlink()
                        total_deleted += 1
                    except OSError as e:
                        console.print(f"    [yellow]could not delete shared: {e}[/]")
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    label = "would" if not apply else "did"
    console.print(
        f"\n[bold]Summary:[/] {label} copy {total_copied} · "
        f"skipped {total_skipped} (identical) · "
        f"conflicts {total_conflicts} (bot-side newer) · "
        f"deleted shared {total_deleted}"
    )
    if not apply:
        console.print("[dim]Re-run with --apply to actually move files.[/]")


@application.command("regenerate-apps-md")
@click.argument("bot_id", required=False)
@click.option("--all", "all_bots", is_flag=True, default=False,
              help="Regenerate for every bot in network.json")
@click.pass_context
def application_regenerate_apps_md(
    ctx: click.Context, bot_id: str | None, all_bots: bool,
) -> None:
    """Regenerate INSTALLED_APPS.md for a bot (or every bot with --all).

    The file lives at /Users/<bot>/.openclaw/workspace/INSTALLED_APPS.md
    and lists each active manifest in plain language so the bot's LLM
    knows what apps it has and how to invoke them. Normally regenerated
    automatically by forge approval and scanner Phase 5; this command
    is the manual escape hatch.
    """
    from .applications.app_registry import regenerate_installed_apps_md

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    if all_bots and bot_id:
        console.print("[red]Pass either a bot_id or --all, not both[/]")
        sys.exit(1)
    if not all_bots and not bot_id:
        console.print("[red]Pass a bot_id or use --all[/]")
        sys.exit(1)

    targets: list[str]
    if all_bots:
        targets = list((network.get("bots") or {}).keys())
    else:
        targets = [bot_id]

    for bid in targets:
        path = regenerate_installed_apps_md(bid, shared_dir)
        if path is None:
            console.print(f"[yellow]{bid}: skipped (workspace unresolvable)[/]")
        else:
            console.print(f"[green]{bid}: wrote {path}[/]")


@application.command("audit")
@click.argument("bot_id")
@click.argument("app_id", required=False)
@click.option("--all", "all_apps", is_flag=True, default=False,
              help="Audit every eligible app on the bot")
@click.option("--ignore-accepted", "full_audit", is_flag=True, default=False,
              help="Full audit — re-evaluate accepted findings too")
@click.option("--no-kick", is_flag=True, default=False,
              help="Queue the inbox request but don't kick the runner now "
                   "(waits for next hourly tick)")
@click.pass_context
def application_audit(
    ctx: click.Context, bot_id: str, app_id: str | None,
    all_apps: bool, full_audit: bool, no_kick: bool,
) -> None:
    """Queue a Tier-3 semantic audit for one or all apps on a bot.

    Examples:
      evolve-admin application audit team_bot_a journal
      evolve-admin application audit team_bot_a --all
      evolve-admin application audit team_bot_a journal --ignore-accepted

    The request is queued on the bot's audit_inbox and the runner is
    kicked immediately. Results land in the bot's audit_outbox and the
    admin's audit_poller (next scheduler tick) ingests them as Proposals.

    Cadence is ignored for manual requests — the named apps audit now
    regardless of when they last ran.
    """
    from .applications.audit_dispatch import request_audit
    from .config import get_bot_user

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)

    bot_user = get_bot_user(bot_id, network)
    if not bot_user:
        console.print(f"[red]Unknown bot: {bot_id}[/]")
        sys.exit(1)

    if all_apps and app_id:
        console.print("[red]Pass either app_id or --all, not both[/]")
        sys.exit(1)
    apps_arg = None if all_apps else ([app_id] if app_id else None)
    if apps_arg is None and not all_apps:
        console.print("[red]Pass app_id or --all[/]")
        sys.exit(1)

    result = request_audit(
        bot_id=bot_id,
        bot_user=bot_user,
        apps=apps_arg,
        full_audit=full_audit,
        requested_by=f"cli:{os.environ.get('USER', '?')}",
        kick=not no_kick,
    )
    if not result.ok:
        console.print(f"[red]audit dispatch failed: {result.error}[/]")
        sys.exit(1)
    console.print(
        f"[green]Queued audit request {result.request_id} on {bot_id} "
        f"({'all eligible apps' if all_apps else app_id})"
        f"{' [FULL]' if full_audit else ''}"
        f"{' (kicked)' if result.kicked else ' (queued; will run on next tick)'}[/]"
    )


@application.command("audit-accept")
@click.argument("bot_id")
@click.argument("app_id")
@click.argument("signature")
@click.option("--rationale", default="", help="Why this finding is being accepted")
@click.pass_context
def application_audit_accept(
    ctx: click.Context, bot_id: str, app_id: str, signature: str, rationale: str,
) -> None:
    """Mark an audit finding as accepted so future audits don't re-raise it.

    Operators reading a Proposal copy the signature from the proposal's
    payload and run this command. The accepted entry lives on the
    manifest under audit_accepted[]. Use the --ignore-accepted flag on
    `application audit` to force re-evaluation.
    """
    from .applications.audit_dispatch import mark_finding_accepted
    from .config import get_bot_user

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    bot_user = get_bot_user(bot_id, network)
    if not bot_user:
        console.print(f"[red]Unknown bot: {bot_id}[/]")
        sys.exit(1)

    ok, err = mark_finding_accepted(
        bot_id=bot_id,
        bot_user=bot_user,
        app_id=app_id,
        signature=signature,
        accepted_by=f"cli:{os.environ.get('USER', '?')}",
        rationale=rationale,
    )
    if ok:
        console.print(f"[green]Marked {signature[:24]}… accepted on {bot_id}/{app_id}[/]")
    else:
        console.print(f"[red]Accept failed: {err}[/]")
        sys.exit(1)


@application.command("scan")
@click.argument("bot_id")
@click.option("--no-llm", is_flag=True, default=False, help="Skip LLM draft generation")
@click.option("--min-confidence", default=0.5, type=float, show_default=True)
@click.option("--auto-approve", is_flag=True, default=False, help="Approve all detected capabilities without interactive review")
@click.option("--no-repair", is_flag=True, default=False,
              help="Skip Phase 4.5 mechanical-repair pass (CLI backfill, hint-word floor, test exemption, INSTALLED_APPS.md registration)")
@click.option("--dedup-existing", is_flag=True, default=False, help="One-time cleanup: walk caps_dir for duplicate manifests left over from previous broken scans, merge each pair (older created_at wins), archive losers to _history/. No discovery is performed.")
@click.pass_context
def application_scan(ctx: click.Context, bot_id: str, no_llm: bool, min_confidence: float, auto_approve: bool, no_repair: bool, dedup_existing: bool) -> None:
    """Scan a bot's workspace and review detected capabilities."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    from .config import get_bot_workspace
    ws = get_bot_workspace(bot_id)
    if ws is None:
        console.print(f"[red]Cannot locate workspace for {bot_id}[/]")
        sys.exit(1)

    from .applications.scanner import (
        scan_workspace_pipeline,
        scan_workspace,
        dedup_existing_manifests,
    )
    from .applications.reviewer import review_manifest

    if dedup_existing:
        from .applications.manifest import applications_dir as _apps_dir
        caps_dir = _apps_dir(shared_dir, bot_id)
        console.print(f"[dim]Dedup-existing pass on: {caps_dir}[/]")
        result = dedup_existing_manifests(caps_dir, shared_dir)
        if result["merged"]:
            console.print(
                f"[green]Merged {result['merged']} duplicate pair(s).[/] "
                f"Archived: {', '.join(result['archived'])}"
            )
        else:
            console.print("[dim]No duplicate manifests found.[/]")
        return

    console.print(f"Scanning workspace: {ws}")

    if auto_approve:
        # Non-interactive path: full pipeline with phase status reporting.
        # Used by the web server; manifests are saved atomically per phase.
        import json as _json
        console.print("[dim]Running full discovery pipeline (phases 1-4)...[/]")
        try:
            network_config = load_network(network_path)
            manifests = scan_workspace_pipeline(
                workspace=ws,
                bot_id=bot_id,
                shared_dir=shared_dir,
                config=network_config,
                use_llm=not no_llm,
                min_confidence=min_confidence,
                repair=not no_repair,
            )
            approved = [m.get("name", m.get("id", "?")) for m in manifests]
        except Exception as exc:
            console.print(f"[red]Pipeline error: {exc}[/]")
            approved = []
    else:
        # Interactive path: discovery only, then prompt for each detected app.
        detected = scan_workspace(ws, min_confidence=min_confidence,
                                  use_llm=not no_llm, bot_id=bot_id)
        if not detected:
            console.print("[yellow]No capabilities detected.[/]")
            return
        console.print(f"\nDetected {len(detected)} capability(-ies):")
        for d in detected:
            console.print(f"  [dim]{d.confidence*100:.0f}%[/] {d.name} — {d.evidence_summary[:60]}")
        console.print("\nStarting interactive review...")
        approved = []
        for d in detected:
            result = review_manifest(d, bot_id, shared_dir, use_llm=not no_llm)
            if result:
                approved.append(result.name)

    if approved:
        console.print(f"\n[green]✓ Approved {len(approved)} manifest(s):[/] {', '.join(approved)}")
    else:
        console.print("\n[dim]No manifests approved.[/]")


@application.command("list")
@click.argument("bot_id")
@click.pass_context
def application_list(ctx: click.Context, bot_id: str) -> None:
    """List application manifests for a bot."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    from .applications.manifest import list_manifests
    manifests = list_manifests(shared_dir, bot_id)
    if not manifests:
        console.print(f"[dim]No manifests for {bot_id}.[/]")
        return

    from rich.table import Table
    t = Table(show_header=True, header_style="bold blue")
    t.add_column("Capability")
    t.add_column("Status")
    t.add_column("Score")
    t.add_column("Goals")
    t.add_column("Tests")
    t.add_column("Issues")
    for m in manifests:
        score = f"{'★' * (m.satisfaction_score or 0)}" if m.satisfaction_score else "—"
        status_color = "green" if m.status == "approved" else "yellow"
        t.add_row(
            m.name, f"[{status_color}]{m.status}[/]",
            score, str(len(m.goals)), str(len(m.tests)),
            str(len(m.known_issues)) if m.known_issues else "0",
        )
    console.print(t)


@application.command("enrich")
@click.argument("bot_id")
@click.option("--force", is_flag=True, default=False, help="Re-generate even if manifest already has 4-section fields")
@click.pass_context
def application_enrich(ctx: click.Context, bot_id: str, force: bool) -> None:
    """Enrich existing thin manifests with full 4-section RSI schema via LLM."""
    import json as _json
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    from .config import get_bot_workspace
    ws = get_bot_workspace(bot_id)

    caps_dir = shared_dir / "applications" / bot_id
    if not caps_dir.exists():
        console.print(f"[yellow]No manifests found for {bot_id}[/]")
        return

    manifest_files = [f for f in sorted(caps_dir.glob("*.json")) if not f.name.startswith("_")]
    if not manifest_files:
        console.print(f"[yellow]No manifests found for {bot_id}[/]")
        return

    console.print(f"Enriching {len(manifest_files)} manifest(s) for {bot_id}…")
    now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    enriched_count = 0

    for mf in manifest_files:
        try:
            data = _json.loads(mf.read_text())
        except Exception:
            console.print(f"  [red]✗ Cannot read {mf.name}[/]")
            continue

        if data.get("identity") and not force:
            console.print(f"  [dim]Skipping {data.get('name', mf.stem)} — already enriched (use --force to re-generate)[/]")
            continue

        from .applications.scanner import DetectedApplication
        d = DetectedApplication(
            id=data.get("id", mf.stem),
            name=data.get("name", mf.stem),
            description=data.get("description", ""),
            confidence=data.get("confidence", 0.8),
            evidence_files=data.get("evidence_files", []),
            evidence_summary="",
            suggested_goals=data.get("goals", []),
            suggested_tests=[tc.get("trigger", "") for tc in data.get("test_cases", []) if isinstance(tc, dict)],
            suggested_privacy=data.get("privacy_constraints", []),
            source=data.get("source", "detected"),
        )

        console.print(f"  [dim]Enriching: {d.name}…[/]")
        enriched = _generate_full_manifest_with_llm(d, bot_id, ws)
        _apply_enrichment(data, enriched, d, bot_id, now)

        tmp = mf.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data, indent=2))
        os.replace(tmp, mf)
        enriched_count += 1
        console.print(f"  [green]✓[/] {d.name}")

    console.print(f"\n[green]Enriched {enriched_count} manifest(s).[/]")


@application.command("new")
@click.argument("bot_id")
@click.pass_context
def application_new(ctx: click.Context, bot_id: str) -> None:
    """Define a new application from scratch (build wizard)."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    from .applications.manifest import ApplicationManifest, save_manifest, now_iso
    from .applications.reviewer import _ask, _ask_multiline, _confirm, _header, _c

    _header("New Application")
    print()
    print("  Define a new application before building it.")
    print("  This creates a manifest that drives RSI measurement.")

    cap_name = _ask("Application name (e.g. 'Task Management')")
    cap_id = cap_name.lower().replace(" ", "-").replace("/", "-")
    description = _ask("Brief description (1-2 sentences)")

    print()
    print("  What should this application accomplish?")
    goals = _ask_multiline("Goals")

    print()
    print("  What would make it clearly broken if you didn't notice?")
    failure_signals = _ask_multiline("Failure signals (signs it's broken)")

    print()
    print("  What data must never leave this application?")
    privacy = _ask_multiline("Privacy constraints")

    manifest = ApplicationManifest(
        id=cap_id, name=cap_name, bot_id=bot_id,
        description=description, goals=goals,
        success_criteria={"failure_signals": failure_signals},
        privacy_constraints=privacy,
        status="draft", created_at=now_iso(),
    )

    if _confirm("Save manifest?", default=True):
        manifest.status = "active"
        manifest.approved_at = now_iso()
        path = save_manifest(manifest, shared_dir)
        console.print(f"[green]✓ Manifest saved: {path}[/]")


@application.command("usage")
@click.option("--bot", "bot_id", default=None, help="Bot ID")
@click.option("--days", default=7, show_default=True, help="Days of history to aggregate")
@click.option("--min-sessions", default=2, show_default=True, help="Minimum sessions to include")
@click.pass_context
def application_usage(ctx: click.Context, bot_id: str | None, days: int, min_sessions: int) -> None:
    """Show per-application usage stats (sessions, corrections, efficiency flags, unresolved)."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    if not bot_id:
        members = network.get("members", [])
        bot_id = network.get("primary") or (members[0] if members else None)
    if not bot_id:
        console.print("[red]--bot required[/]")
        return

    try:
        from analyze import load_metrics_range
    except ImportError as e:
        console.print(f"[red]analyze.py not found:[/] {e}")
        return

    history = load_metrics_range(str(shared_dir), bot_id, days)
    if not history:
        console.print(f"[yellow]No metrics found for {bot_id} in past {days} days.[/]")
        return

    # Aggregate application stats across all days
    cap_totals: dict[str, dict] = {}
    for day_metrics in history:
        for cap, stats in day_metrics.get("application_usage", {}).items():
            if cap not in cap_totals:
                cap_totals[cap] = {"sessions": 0, "unresolved_sessions": 0,
                                   "correction_sessions": 0, "efficiency_sessions": 0,
                                   "promise_sessions": 0}
            for k in cap_totals[cap]:
                cap_totals[cap][k] += stats.get(k, 0)

    # Filter and sort by session count
    rows = [
        (cap, t) for cap, t in cap_totals.items()
        if t["sessions"] >= min_sessions
    ]
    rows.sort(key=lambda x: -x[1]["sessions"])

    if not rows:
        console.print(f"[yellow]No applications with ≥{min_sessions} sessions found.[/]")
        return

    from rich.table import Table
    t = Table(show_header=True, header_style="bold blue", title=f"{bot_id} — application usage ({days}d)")
    t.add_column("Application")
    t.add_column("Sessions", justify="right")
    t.add_column("Unresolved", justify="right")
    t.add_column("Corrections", justify="right")
    t.add_column("Efficiency⚠", justify="right")
    t.add_column("Promises", justify="right")
    t.add_column("Health", justify="center")

    for cap, s in rows:
        total = s["sessions"] or 1
        unres_rate = s["unresolved_sessions"] / total
        corr_rate = s["correction_sessions"] / total

        if unres_rate >= 0.50 or corr_rate >= 0.50:
            health = "[red]⚠ poor[/]"
        elif unres_rate >= 0.30 or corr_rate >= 0.30:
            health = "[yellow]⚠ fair[/]"
        else:
            health = "[green]✓ ok[/]"

        t.add_row(
            cap,
            str(s["sessions"]),
            f"{s['unresolved_sessions']} ({unres_rate:.0%})",
            f"{s['correction_sessions']} ({corr_rate:.0%})",
            str(s["efficiency_sessions"]),
            str(s["promise_sessions"]),
            health,
        )

    console.print(t)


@application.command("install")
@click.argument("app_name")
@click.option("--bot", required=True, help="Bot ID to install on")
@click.option("--confirm", is_flag=True, default=False, help="Proceed with installation (default: dry run)")
@click.pass_context
def application_install(ctx: click.Context, app_name: str, bot: str, confirm: bool) -> None:
    """Install an application on a bot."""
    import shutil

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    # Locate the capability app directory
    caps_root = Path(__file__).parent.parent.parent.parent.parent / "capabilities"
    if not caps_root.exists():
        # Try relative to CWD
        caps_root = Path.cwd() / "capabilities"
    app_dir = caps_root / app_name

    if not app_dir.exists():
        console.print(f"[red]Capability app '{app_name}' not found.[/]")
        console.print(f"  Looked in: {caps_root}")
        console.print(f"  Available: {', '.join(d.name for d in caps_root.iterdir() if d.is_dir() and d.name != 'template') if caps_root.exists() else 'none'}")
        sys.exit(1)

    # Parse CAPABILITY.md
    cap_md = app_dir / "CAPABILITY.md"
    if not cap_md.exists():
        console.print(f"[red]No CAPABILITY.md found in {app_dir}[/]")
        sys.exit(1)

    cap_text = cap_md.read_text()

    # Extract name and version
    name_match = re.search(r"^#\s+Capability:\s+(.+)$", cap_text, re.MULTILINE)
    version_match = re.search(r"^##\s+Version\s*\n+(.+)$", cap_text, re.MULTILINE)
    cap_name = name_match.group(1).strip() if name_match else app_name
    cap_version = version_match.group(1).strip() if version_match else "0.1.0"

    # Extract requirements
    req_match = re.search(r"##\s+Requirements\s*\n(.*?)(?=\n##|\Z)", cap_text, re.DOTALL)
    req_text = req_match.group(1).strip() if req_match else ""

    integrations = []
    if m := re.search(r"Integrations:\s*(.+)", req_text):
        raw = m.group(1).strip()
        if raw.lower() not in ("none", ""):
            integrations = [i.strip().rstrip(",") for i in raw.split(",") if i.strip().lower() not in ("none", "optional")]

    tools_needed = []
    if m := re.search(r"Tools:\s*(.+)", req_text):
        raw = m.group(1).strip()
        if raw.lower() not in ("none", ""):
            tools_needed = [t.strip() for t in raw.split(",") if t.strip().lower() != "none"]

    schedule = None
    if m := re.search(r"Schedule:\s*(.+)", req_text):
        raw = m.group(1).strip()
        if raw.lower() != "none":
            schedule = raw

    # Check bot exists in network config
    bots_cfg = network.get("bots", {})
    bot_known = bot in bots_cfg
    bot_role = bots_cfg.get(bot, {}).get("role", "unknown") if bot_known else "unknown"

    # Check compatibility
    compat_match = re.search(r"##\s+Compatible roles\s*\n(.*?)(?=\n##|\Z)", cap_text, re.DOTALL)
    compat_roles = []
    if compat_match:
        for line in compat_match.group(1).splitlines():
            line = line.strip().lstrip("- ")
            if line:
                compat_roles.append(line)

    # Enumerate what would be installed
    scripts_dir = app_dir / "scripts"
    prompts_dir = app_dir / "prompts"
    scripts = sorted(scripts_dir.glob("*.py")) if scripts_dir.exists() else []
    prompts = sorted(prompts_dir.glob("*.md")) if prompts_dir.exists() else []

    console.print(f"\nInstalling [bold]{app_name}[/] on [bold]{bot}[/]...")
    console.print(f"  [dim]Manifest:[/] {cap_name} v{cap_version}")

    # Requirements checks
    ok_mark = "[green]✓[/]"
    warn_mark = "[yellow]⚠[/]"
    missing_integrations = []

    for integ in integrations:
        # Check if integration config exists in bot's openclaw.json
        oc_json = _bot_home(bot) / ".openclaw" / "openclaw.json"
        found = False
        if oc_json.exists():
            try:
                oc_cfg = json.loads(oc_json.read_text())
                # Heuristic: look for integration name in integrations or tools sections
                oc_str = json.dumps(oc_cfg).lower()
                found = integ.lower().replace(" ", "_") in oc_str or integ.lower().split()[0] in oc_str
            except (json.JSONDecodeError, OSError):
                pass
        status = ok_mark if found else f"[red]missing[/]"
        console.print(f"  {ok_mark if found else warn_mark} Requirement: {integ} ({status})")
        if not found:
            missing_integrations.append(integ)

    for tool in tools_needed:
        console.print(f"  {ok_mark} Tool: {tool} (assumed available)")

    if missing_integrations:
        console.print(f"\n  [yellow]⚠ Missing integrations:[/] {', '.join(missing_integrations)}")
        console.print(f"  → Configure these in {bot}'s openclaw.json, then re-run install")

    if compat_roles and bot_role not in compat_roles and bot_role != "unknown":
        console.print(f"\n  [yellow]⚠ Role mismatch:[/] {bot} is '{bot_role}', app supports {compat_roles}")

    console.print(f"\n  Would install:")
    for script in scripts:
        sched_note = f" — {schedule}" if schedule and script.name in schedule else ""
        console.print(f"  - {script.name}{sched_note} → cron script")
    for prompt in prompts:
        console.print(f"  - {prompt.name} → system prompt addition")

    # Installation destination
    install_log = shared_dir / "applications" / bot / "installed.json"
    console.print(f"\n  Install log: {install_log}")

    if not confirm:
        console.print("\n  Run with [bold]--confirm[/] to proceed.")
        return

    # ── Actual installation ────────────────────────────────────────────────────
    cap_shared = shared_dir / "applications" / bot / app_name
    cap_shared.mkdir(parents=True, exist_ok=True)

    if scripts_dir.exists():
        dest_scripts = cap_shared / "scripts"
        dest_scripts.mkdir(exist_ok=True)
        for script in scripts:
            shutil.copy2(script, dest_scripts / script.name)
            console.print(f"  [green]✓[/] Copied {script.name} → {dest_scripts / script.name}")

    if prompts_dir.exists():
        dest_prompts = cap_shared / "prompts"
        dest_prompts.mkdir(exist_ok=True)
        for prompt in prompts:
            shutil.copy2(prompt, dest_prompts / prompt.name)
            console.print(f"  [green]✓[/] Copied {prompt.name} → {dest_prompts / prompt.name}")

    # Write install log
    import datetime as _dt
    install_log.parent.mkdir(parents=True, exist_ok=True)
    installed: dict = {}
    if install_log.exists():
        try:
            installed = json.loads(install_log.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    installed[app_name] = {
        "name": cap_name,
        "version": cap_version,
        "installed_at": _dt.datetime.now().isoformat(),
        "scripts": [s.name for s in scripts],
        "prompts": [p.name for p in prompts],
        "missing_integrations": missing_integrations,
    }
    install_log.write_text(json.dumps(installed, indent=2))

    console.print(f"\n  [green]✓[/] Installed {cap_name} on {bot}.")
    if missing_integrations:
        console.print(f"  [yellow]⚠[/] Complete setup by configuring: {', '.join(missing_integrations)}")


# ── models ─────────────────────────────────────────────────────────

@main.group()
def models() -> None:
    """Manage model roles (fast/standard/power/max/judge).

    Commands accept either a role ID or a legacy tier key
    (tier0-tier3) during the rungs/roles transition
    (spec-model-rungs-and-roles-2026-06-09).
    """


@models.command("list")
@click.pass_context
def models_list(ctx: click.Context) -> None:
    """Show current role → rung → model mapping."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    from models import print_tier_table
    print_tier_table(network)


@models.command("show")
@click.argument("role")
@click.pass_context
def models_show(ctx: click.Context, role: str) -> None:
    """Show full details for a role ID (fast/standard/power/max/judge) or legacy tier key."""
    tier = role
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    from models import print_tier_detail
    print_tier_detail(tier, network)


@models.command("set")
@click.argument("role", metavar="ROLE")
@click.argument("rung", metavar="RUNG")
@click.option(
    "--yes", "-y", "assume_yes", is_flag=True, default=False,
    help="Skip the costClass-jump confirmation prompt (for non-tty automation).",
)
@click.pass_context
def models_set(ctx: click.Context, role: str, rung: str, assume_yes: bool) -> None:
    """Point a role at a rung in network.json::models.roles.

    ROLE is a role ID: fast, standard, power, max, judge.
    RUNG is a rung slug, e.g. haiku-class, sonnet-class, opus-class,
    fable-class (or a custom slug you've defined in models.rungs).

    Validation:
      • judge: errors if no model in the target rung has a different
        provider than the standard role's primary model (provider-
        diversity invariant for cross-model evaluation).
      • any role: warns when the re-point changes costClass by more
        than one step (e.g. fast → fable-class is a 3-step jump and
        almost certainly wrong).

    \b
    Examples:
      evolve-admin models set power opus-class
      evolve-admin models set max fable-class
      evolve-admin models set judge sonnet-class
    """
    from models import COST_CLASS_ORDER

    _VALID_ROLES = {"fast", "standard", "power", "max", "judge"}
    if role not in _VALID_ROLES:
        console.print(
            f"[red]Unknown role '{role}'. Valid roles: "
            + ", ".join(sorted(_VALID_ROLES))
            + "[/red]"
        )
        raise click.Abort()

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)

    rungs_list = (network.get("models") or {}).get("rungs") or []
    rung_by_id = {r["id"]: r for r in rungs_list if isinstance(r, dict) and "id" in r}

    if rung not in rung_by_id:
        console.print(
            f"[red]Rung '{rung}' not found in network.json models.rungs.[/red]\n"
            "Available rungs: "
            + (", ".join(sorted(rung_by_id)) or "(none — add to models.rungs first)")
        )
        raise click.Abort()

    target_rung = rung_by_id[rung]
    target_models = target_rung.get("models") or []
    target_cost = target_rung.get("costClass", "")

    roles_cfg = (network.get("models") or {}).get("roles") or {}

    # ── Judge provider-diversity invariant ──────────────────────────────────
    # The judge role must resolve to a model from a DIFFERENT provider than
    # the standard role's primary model. This is the "Goodhart's-law guard"
    # from spec-model-rungs-and-roles §Roles (judge row).
    if role == "judge":
        std_rung_id = roles_cfg.get("standard") or "sonnet-class"
        if isinstance(std_rung_id, dict):
            std_rung_id = std_rung_id.get("rung", "sonnet-class")
        std_rung = rung_by_id.get(std_rung_id, {})
        std_models = std_rung.get("models") or []
        std_primary = std_models[0] if std_models else ""
        # Bare ids (no slash) resolve to no provider — same convention as
        # model_registry._provider_of and ModelRouter._providerOf, so this
        # check matches what the runtime resolver would actually pick.
        std_provider = std_primary.split("/")[0] if "/" in std_primary else None

        diverse_models = [
            m for m in target_models
            if (m.split("/")[0] if "/" in m else None) != std_provider
        ]
        if not diverse_models:
            console.print(
                f"[red]Error: judge role requires at least one model in rung "
                f"'{rung}' from a different provider than standard's provider "
                f"('{std_provider or 'unknown'}'). Add a cross-provider model (e.g. openai/gpt-4o "
                f"or google/gemini-2.0-flash) to the rung first.[/red]"
            )
            raise click.Abort()

    # ── CostClass step-change warning ───────────────────────────────────────
    # Warn (but don't block) when re-pointing a role jumps more than one
    # step in costClass. A 2-step jump is usually an accident.
    current_rung_id = roles_cfg.get(role)
    if isinstance(current_rung_id, dict):
        current_rung_id = current_rung_id.get("rung")
    if current_rung_id and current_rung_id != rung:
        current_rung = rung_by_id.get(str(current_rung_id), {})
        current_cost = current_rung.get("costClass", "")
        if current_cost in COST_CLASS_ORDER and target_cost in COST_CLASS_ORDER:
            step = abs(
                COST_CLASS_ORDER.index(target_cost)
                - COST_CLASS_ORDER.index(current_cost)
            )
            if step > 1:
                console.print(
                    f"[yellow]Warning: re-pointing '{role}' from rung "
                    f"'{current_rung_id}' ({current_cost}) to '{rung}' ({target_cost}) "
                    f"is a {step}-step costClass jump. Pass --yes to confirm "
                    f"non-interactively, or Ctrl-C to cancel.[/yellow]"
                )
                if not assume_yes:
                    click.confirm("Proceed?", abort=True)

    # ── Write ────────────────────────────────────────────────────────────────
    if "models" not in network:
        network["models"] = {}
    if "roles" not in network["models"]:
        network["models"]["roles"] = {}

    if role == "judge":
        # Preserve the provider constraint shape when already present;
        # otherwise write the object form to make the diversity invariant
        # explicit in the config.
        existing = network["models"]["roles"].get("judge")
        if isinstance(existing, dict):
            existing["rung"] = rung
            network["models"]["roles"]["judge"] = existing
        else:
            network["models"]["roles"]["judge"] = {
                "rung": rung,
                "provider": "not-standard",
            }
    else:
        network["models"]["roles"][role] = rung

    save_network(network, network_path)
    console.print(
        f"[green]✓[/green] models.roles.{role} → [bold]{rung}[/bold] "
        f"(costClass: {target_cost or '?'})"
        + (
            f" — primary: {target_models[0]}" if target_models else ""
        )
    )


@models.command("cap")
@click.argument("bot")
@click.argument("value", type=int)
@click.pass_context
def models_cap(ctx: click.Context, bot: str, value: int) -> None:
    """Set the per-bot Power (tier1) daily cap.

    Writes ``{sharedDir}/{bot}/tiers.json::userTierOverride.dailyCap``.
    Range 0-100. Set 0 to disable the Power chip on this bot.
    Spec: docs/spec-user-tier-control-2026-05-26.md §"Per-bot opt-out
    + adjustable daily cap".

    \b
    Examples:
      evolve-admin models cap evolve 20    # raise to 20/day
      evolve-admin models cap forge 0      # disable Power on forge
      evolve-admin models cap evolve 10    # reset to default
    """
    if not (0 <= value <= 100):
        console.print(f"[red]value must be 0-100, got {value}[/red]")
        raise click.Abort()
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    bot_dir = shared / bot
    if not bot_dir.exists():
        bot_dir.mkdir(parents=True, exist_ok=True)
    tiers_path = bot_dir / "tiers.json"
    try:
        data = json.loads(tiers_path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    override = data.get("userTierOverride") or {}
    override["dailyCap"] = value
    # Default enabled=True if absent; setting a cap implies the chip
    # should at least be reachable. Use `models user-tier-control off`
    # to disable the chip entirely.
    if "enabled" not in override:
        override["enabled"] = True
    data["userTierOverride"] = override
    tiers_path.write_text(json.dumps(data, indent=2) + "\n")
    console.print(
        f"[green]✓[/green] {bot}: Power daily cap set to "
        f"[bold]{value}[/bold]/day"
        + (" (Power disabled)" if value == 0 else "")
    )


@models.command("user-tier-control")
@click.argument("bot")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.pass_context
def models_user_tier_control(ctx: click.Context, bot: str, state: str) -> None:
    """Show / hide the model-tier chip on the admin UI chat composer.

    Writes ``{sharedDir}/{bot}/tiers.json::userTierOverride.enabled``.
    Off hides the chip entirely; the backend still accepts whatever
    the frontend sends (defense in depth — a cached client may not
    have re-rendered yet).

    \b
    Examples:
      evolve-admin models user-tier-control evolve off
      evolve-admin models user-tier-control forge on
    """
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    bot_dir = shared / bot
    if not bot_dir.exists():
        bot_dir.mkdir(parents=True, exist_ok=True)
    tiers_path = bot_dir / "tiers.json"
    try:
        data = json.loads(tiers_path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    override = data.get("userTierOverride") or {}
    override["enabled"] = (state == "on")
    if "dailyCap" not in override:
        override["dailyCap"] = 10
    data["userTierOverride"] = override
    tiers_path.write_text(json.dumps(data, indent=2) + "\n")
    console.print(
        f"[green]✓[/green] {bot}: model-tier chip "
        f"[bold]{'enabled' if state == 'on' else 'hidden'}[/bold]"
    )


@models.command("usage")
@click.option("--bot", default=None, help="Specific bot ID")
@click.pass_context
def models_usage(ctx: click.Context, bot: str | None) -> None:
    """Show today's tier usage counts."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    members = [bot] if bot else network.get("members", [])

    from models import get_tier_usage_today

    from rich.table import Table
    # Usage records accept both legacy tier keys (tier0-tier3) and new
    # role IDs (fast/standard/power/judge/max). Sum both forms so the
    # table works during and after the migration.
    t = Table(show_header=True, header_style="bold blue")
    t.add_column("Bot")
    t.add_column("max")
    t.add_column("power")
    t.add_column("standard")
    t.add_column("fast")
    t.add_column("judge")
    for bot_id in members:
        usage = get_tier_usage_today(bot_id, shared_dir)
        t.add_row(
            bot_id,
            # max has no legacy tier counterpart — only the new role key
            str(usage.get("max", 0)),
            # Accept either the legacy tier key or the role ID
            str(usage.get("tier1", 0) + usage.get("power", 0)),
            str(usage.get("tier2", 0) + usage.get("standard", 0)),
            str(usage.get("tier3", 0) + usage.get("fast", 0)),
            str(usage.get("tier0", 0) + usage.get("judge", 0)),
        )
    console.print(t)


# ── keys ───────────────────────────────────────────────────────────────

@main.group()
def keys() -> None:
    """Manage shared and per-bot API keys."""


# ── keystore ────────────────────────────────────────────────────────────
#
# The ``keystore`` group is the read-side complement to ``keys``. It exists
# specifically for **shell consumers** at MCP-server launch time —
# packages/analyzer/mcp_admin/launcher.py renders wrapper scripts that
# shell out to ``evolve-admin keystore get <slot>`` to resolve credentials
# into env vars at exec time. The launcher's contract (launcher.py:106-114):
#
#     VAR=$(/usr/local/bin/evolve-admin keystore get <slot> 2>/dev/null)
#     if [ -z "${VAR}" ]; then echo "missing..." >&2; exit 64; fi
#     export VAR
#
# So this command MUST:
#   - print the raw value to stdout with no trailing newline (so the
#     env var carries the exact secret, not a value+\n)
#   - exit non-zero (and emit nothing on stdout) if the key is unknown or
#     has no stored value — the shell guard treats empty stdout as failure
#   - never print the secret to stderr (the launcher redirects stderr to
#     /dev/null, but other consumers may not)
#
# Pre-2026-05-30 the launcher's shell-out targeted a command that didn't
# exist. The first MCP install with required_env (notion, then linear and
# github-mcp) silently failed at exec time while resolve_status_mcp
# reported 'valid' (only checked slot presence, not exec viability). See
# docs/skills-deep-audit-2026-05-30.md F1.

@main.group()
def keystore() -> None:
    """Read keystore values (read-only, for shell consumers like MCP launchers)."""


@keystore.command("get")
@click.argument("name")
@click.pass_context
def keystore_get(ctx: click.Context, name: str) -> None:
    """Print the raw value of registered key NAME to stdout (no trailing newline).

    Exits 0 with the value on success; 1 with empty stdout on missing or
    empty key. Designed for shell consumers — see module docstring.
    """
    mgr, _ = _get_ks_manager(ctx)
    value = mgr.get_value(name)
    if not value:
        # Missing key OR empty stored value. Treat the same — the shell
        # guard at the consumer side can't distinguish, and an empty
        # value is just as broken as a missing one for the consumer's
        # purposes.
        sys.exit(1)
    # Write directly to stdout without click.echo's automatic newline so
    # the captured value is the raw secret, not value+'\n'.
    sys.stdout.write(value)
    sys.stdout.flush()


def _get_ks_manager(ctx: click.Context):
    from .keystore import KeystoreManager
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    return KeystoreManager(shared_dir), network


@keys.command("status")
@click.pass_context
def keys_status(ctx: click.Context) -> None:
    """Show all registered keys and sync status."""
    mgr, network = _get_ks_manager(ctx)
    members = network.get("members", [])
    mgr.status(members)


@keys.command("add")
@click.argument("name")
@click.option("--provider", required=True, help="Provider name (e.g. brave, anthropic)")
@click.option("--scope", type=click.Choice(["shared", "group", "per-bot"]), default="shared", show_default=True)
@click.option("--bots", default=None, help="Comma-separated bot IDs (for group scope; omit for all)")
@click.option("--description", default="", help="Human-readable description")
@click.option("--value", default=None, help="Key value (prompted if omitted)")
@click.pass_context
def keys_add(ctx: click.Context, name: str, provider: str, scope: str,
            bots: str | None, description: str, value: str | None) -> None:
    """Register a new API key."""
    mgr, _ = _get_ks_manager(ctx)
    bot_list = [b.strip() for b in bots.split(",")] if bots else None
    mgr.register(name, provider, scope, description, bot_list, value)
    console.print(f"[green]✓ Registered '{name}' ({scope}/{provider})[/]")


@keys.command("set")
@click.argument("name")
@click.option("--value", default=None, help="New value (prompted if omitted)")
@click.pass_context
def keys_set(ctx: click.Context, name: str, value: str | None) -> None:
    """Set or update the value for a registered key."""
    mgr, _ = _get_ks_manager(ctx)
    mgr.set_value(name, value)


@keys.command("sync")
@click.option("--all", "sync_all", is_flag=True, default=False, help="Sync to all network members")
@click.option("--bot", "bot_ids", multiple=True, help="Specific bot IDs to sync")
@click.option("--key", "key_names", multiple=True, help="Specific key names to sync (default: all shared)")
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_context
def keys_sync(ctx: click.Context, sync_all: bool, bot_ids: tuple,
             key_names: tuple, dry_run: bool) -> None:
    """Push shared key values to bot auth-profiles.json files."""
    mgr, network = _get_ks_manager(ctx)
    members = network.get("members", [])
    bots = list(members) if sync_all else list(bot_ids)
    if not bots:
        console.print("[yellow]Specify --all or --bot <id>[/]")
        return
    keys = list(key_names) or None
    results = mgr.sync(bots, keys, dry_run=dry_run)
    total = sum(len(v) for v in results.values())
    if not dry_run:
        console.print(f"[green]✓ Synced {total} key(s) across {len(bots)} bot(s)[/]")


@keys.command("rotate")
@click.argument("name")
@click.option("--value", default=None, help="New value (prompted if omitted)")
@click.option("--all", "sync_all", is_flag=True, default=True)
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_context
def keys_rotate(ctx: click.Context, name: str, value: str | None,
               sync_all: bool, dry_run: bool) -> None:
    """Rotate a key: update value and sync to all authorized bots."""
    mgr, network = _get_ks_manager(ctx)
    members = network.get("members", [])
    mgr.rotate(name, list(members), value, dry_run=dry_run)


@keys.command("rollback")
@click.argument("name")
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_context
def keys_rollback(ctx: click.Context, name: str, dry_run: bool) -> None:
    """Roll back a key to its previous value and re-sync to all bots."""
    mgr, network = _get_ks_manager(ctx)
    members = network.get("members", [])
    mgr.rollback(name, list(members), dry_run=dry_run)


@keys.command("list")
@click.pass_context
def keys_list(ctx: click.Context) -> None:
    """List all registered keys."""
    mgr, network = _get_ks_manager(ctx)
    ks_keys = mgr.ks.list_keys()
    if not ks_keys:
        console.print("[dim]No keys registered.[/]")
        return
    from rich.table import Table
    t = Table(show_header=True, header_style="bold blue")
    t.add_column("Name")
    t.add_column("Scope")
    t.add_column("Provider")
    t.add_column("Bots")
    t.add_column("Description")
    for k in ks_keys:
        bots_str = ", ".join(k["bots"]) if k.get("bots") else "all"
        t.add_row(k["name"], k["scope"], k["provider"], bots_str, k.get("description",""))
    console.print(t)


# ── Tasks (Continuity Engine v1) — REMOVED ──────────────────────────
# The `evolve-admin tasks` command group operated on Continuity v1's
# task_queue (extractor + agent runner + inline executor). v2 replaces
# this with the bot-driven `defer` tool — there's no admin queue to
# inspect, approve, or cancel under v2 (the bot owns its own queue,
# fired by defer_runner). Look at /Users/<bot>/.openclaw/workspace/
# evolve/defer-queue.jsonl directly if you need to inspect.


# ── Slack signals ─────────────────────────────────────────────────────

@main.group("slack")
def slack_cmd() -> None:
    """Slack quality signal ingestion (for multi-user bots like Team_bot_a)."""


@slack_cmd.command("ingest")
@click.option("--bot", "bot_id", default=None)
@click.option("--days", default=7, show_default=True)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def slack_ingest(ctx: click.Context, bot_id: str, days: int, dry_run: bool) -> None:
    """Ingest Slack reaction and thread signals for a bot."""
    network_path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = network.get("sharedDir", str(DEFAULT_SHARED_DIR))
    try:
        from slack_signals import ingest_slack_signals
        slack_token = network.get("slackBotToken", "")
        if not slack_token:
            console.print("[red]slackBotToken not configured in network.json[/]")
            return
        channels = network.get("slackChannels", ["#general"])
        count = ingest_slack_signals(bot_id, shared_dir, slack_token, channels, days, dry_run)
        console.print(f"[green]✓ {count} signal(s) ingested[/]")
    except ImportError as e:
        console.print(f"[red]slack_signals not found:[/] {e}")


@slack_cmd.command("report")
@click.option("--bot", "bot_id", default=None)
@click.option("--days", default=7, show_default=True)
@click.pass_context
def slack_report(ctx: click.Context, bot_id: str, days: int) -> None:
    """Show Slack quality signal summary by capability."""
    network_path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = network.get("sharedDir", str(DEFAULT_SHARED_DIR))
    try:
        from slack_signals import print_report
        print_report(shared_dir, bot_id, days)
    except ImportError as e:
        console.print(f"[red]slack_signals not found:[/] {e}")


# ── Expansion engine ───────────────────────────────────────────────

@main.group("expansion")
def expansion_cmd() -> None:
    """Proactive capability expansion engine."""


@expansion_cmd.command("run")
@click.option("--bot", "bot_id", required=True)
@click.option("--days", default=30, show_default=True)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def expansion_run(ctx: click.Context, bot_id: str, days: int, dry_run: bool) -> None:
    """Run the expansion engine and generate capability gap proposals."""
    network_path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = network.get("sharedDir", str(DEFAULT_SHARED_DIR))
    try:
        from expansion import run_expansion
        proposals = run_expansion(bot_id, shared_dir, network, days, dry_run)
        console.print(f"[green]✓ {len(proposals)} proposal(s) generated[/]")
    except ImportError as e:
        console.print(f"[red]expansion not found:[/] {e}")


@expansion_cmd.command("report")
@click.option("--bot", "bot_id", required=True)
@click.option("--days", default=30, show_default=True)
@click.pass_context
def expansion_report(ctx: click.Context, bot_id: str, days: int) -> None:
    """Show recurring theme analysis without generating proposals."""
    network_path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = network.get("sharedDir", str(DEFAULT_SHARED_DIR))
    try:
        from expansion import (
            load_session_summaries, load_manifests,
            identify_recurring_themes, KNOWN_APPLICATION_DOMAINS, LOOKBACK_DAYS
        )
        summaries = load_session_summaries(shared_dir, bot_id, days)
        manifests = load_manifests(shared_dir, bot_id)
        known = KNOWN_APPLICATION_DOMAINS | {m.get("id", "") for m in manifests}
        themes = identify_recurring_themes(summaries, known, 3)
        if not themes:
            console.print("[yellow]No recurring themes found.[/]")
            return
        from rich.table import Table
        t = Table(show_header=True, header_style="bold blue",
                  title=f"{bot_id} — application gaps ({days}d)")
        t.add_column("Theme")
        t.add_column("Sessions", justify="right")
        t.add_column("Correction Rate", justify="right")
        t.add_column("Gap Score", justify="right")
        t.add_column("Sample")
        for th in themes[:10]:
            t.add_row(
                th["theme"],
                str(th["sessions"]),
                f"{th['correction_rate']:.0%}",
                f"{th['gap_score']:.1f}",
                (th["sample_outcomes"][0][:50] if th["sample_outcomes"] else ""),
            )
        console.print(t)
    except ImportError as e:
        console.print(f"[red]expansion not found:[/] {e}")


# ── Modules ──────────────────────────────────────────────────────────

@main.group("modules")
def modules_group() -> None:
    """Enable, disable, and tune Evolve modules."""


@modules_group.command("list")
@click.pass_context
def modules_list(ctx: click.Context) -> None:
    """Show all modules and their current state."""
    from rich.table import Table
    from evolve_config import get_modules
    network_path = ctx.obj.get("network") if ctx.obj else None
    config = load_network(network_path) if network_path else {}
    modules = get_modules(config)
    t = Table(show_header=True, header_style="bold blue", title="Evolve Modules")
    t.add_column("Module")
    t.add_column("Status")
    t.add_column("Key Settings")
    for name, cfg in modules.items():
        enabled = cfg.get("enabled", True)
        status = "[green]enabled[/]" if enabled else "[red]disabled[/]"
        settings = ", ".join(
            f"{k}={v}" for k, v in cfg.items()
            if k not in ("enabled", "detectors") and not isinstance(v, dict)
        )
        if name == "analysis" and "detectors" in cfg:
            disabled_detectors = [d for d, dc in cfg["detectors"].items() if not dc.get("enabled", True)]
            if disabled_detectors:
                settings += f" | disabled detectors: {', '.join(disabled_detectors)}"
        t.add_row(name, status, settings or "—")
    console.print(t)


@modules_group.command("enable")
@click.argument("module")
@click.pass_context
def modules_enable(ctx: click.Context, module: str) -> None:
    """Enable a module."""
    from evolve_config import set_module_enabled, CANONICAL_NETWORK_JSON
    network_path = (ctx.obj.get("network") if ctx.obj else None) or CANONICAL_NETWORK_JSON
    set_module_enabled(Path(network_path), module, True)
    console.print(f"[green]✓[/] {module} enabled")


@modules_group.command("disable")
@click.argument("module")
@click.pass_context
def modules_disable(ctx: click.Context, module: str) -> None:
    """Disable a module."""
    from evolve_config import set_module_enabled, CANONICAL_NETWORK_JSON
    network_path = (ctx.obj.get("network") if ctx.obj else None) or CANONICAL_NETWORK_JSON
    set_module_enabled(Path(network_path), module, False)
    console.print(f"[yellow]✓[/] {module} disabled")


@modules_group.command("tune")
@click.argument("module")
@click.argument("key")
@click.argument("value")
@click.pass_context
def modules_tune(ctx: click.Context, module: str, key: str, value: str) -> None:
    """Set a tuning parameter for a module (value auto-parsed as int/float/bool/str)."""
    from evolve_config import set_module_setting, CANONICAL_NETWORK_JSON
    network_path = (ctx.obj.get("network") if ctx.obj else None) or CANONICAL_NETWORK_JSON
    # Auto-parse value
    parsed: object
    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    else:
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                parsed = value
    set_module_setting(Path(network_path), module, key, parsed)
    console.print(f"[green]✓[/] {module}.{key} = {parsed!r}")


@modules_group.group("detector")
def modules_detector() -> None:
    """Enable or disable individual analysis detectors."""


@modules_detector.command("enable")
@click.argument("detector")
@click.pass_context
def detector_enable(ctx: click.Context, detector: str) -> None:
    """Enable a specific analysis detector."""
    from evolve_config import set_detector_enabled, CANONICAL_NETWORK_JSON
    network_path = (ctx.obj.get("network") if ctx.obj else None) or CANONICAL_NETWORK_JSON
    set_detector_enabled(Path(network_path), detector, True)
    console.print(f"[green]✓[/] detector {detector} enabled")


@modules_detector.command("disable")
@click.argument("detector")
@click.pass_context
def detector_disable(ctx: click.Context, detector: str) -> None:
    """Disable a specific analysis detector."""
    from evolve_config import set_detector_enabled, CANONICAL_NETWORK_JSON
    network_path = (ctx.obj.get("network") if ctx.obj else None) or CANONICAL_NETWORK_JSON
    set_detector_enabled(Path(network_path), detector, False)
    console.print(f"[yellow]✓[/] detector {detector} disabled")


# ── upgrade ──────────────────────────────────────────────────────────────────

@main.command()
@click.option("--repo", "repo_path", default=None, type=click.Path(path_type=Path),
              help="Path to evolve repo (default: auto-detect from this package)")
@click.option("--skip-plugin", is_flag=True, help="Skip TypeScript plugin rebuild")
@click.option("--skip-deploy", is_flag=True, help="Skip re-deploying scripts to bots")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def upgrade(ctx: click.Context, repo_path: "Path | None", skip_plugin: bool, skip_deploy: bool, dry_run: bool) -> None:
    """Version-aware upgrade: compare install.json, git pull, rebuild, redeploy, update version record.

    Detects install mode from install.json:
      - Upgrade (installed version older than codebase)  → proceeds normally
      - Repair  (same version)                            → skips git pull, redeploys
      - Downgrade (installed newer than codebase)        → warns and prompts
      - No install.json (pre-v0.3 install)               → proceeds without version check
    """
    import shutil as _shutil
    import time as _time

    # Capture this BEFORE anything runs so we can verify daemon
    # restarts (admin server, bot gateways) actually swapped to new
    # processes whose start time is > upgrade_began_at. Without
    # this anchor a stale daemon looks identical to a freshly-
    # restarted one from the operator's point of view.
    upgrade_began_at = _time.time()

    shared_dir = Path("/Users/Shared/evolve")
    install_info = read_install_json(shared_dir)

    def _parse_ver(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split("."))
        except (ValueError, AttributeError):
            return (0,)

    # Version-aware pre-flight
    if install_info:
        installed_version = install_info.get("version", "unknown")
        installed_at = install_info.get("installed_at", "")[:10]
        installed_bots = install_info.get("bots", [])

        console.print(Panel(
            f"[bold]Installed:[/] v{installed_version}  (installed {installed_at})\n"
            f"[bold]Current:[/]   v{EVOLVE_VERSION}\n"
            f"[bold]Bots:[/]      {', '.join(installed_bots) or 'auto-detect from network.json'}",
            title="⚡ Evolve Upgrade",
        ))

        cur_tup = _parse_ver(EVOLVE_VERSION)
        inst_tup = _parse_ver(installed_version)

        if cur_tup == inst_tup:
            console.print(f"[green]Already at v{EVOLVE_VERSION}[/] — running in repair/redeploy mode.")
        elif cur_tup > inst_tup:
            console.print(f"\nUpgrading: [bold]v{installed_version} → v{EVOLVE_VERSION}[/]")
        else:
            console.print(f"[red bold]Warning: installed v{installed_version} is NEWER than codebase v{EVOLVE_VERSION}.[/]")
            console.print("[red]Downgrade may break your pod. User data will be preserved but schema changes may conflict.[/]")
            if not dry_run and not click.confirm("Proceed with downgrade?", default=False):
                sys.exit(0)
    else:
        console.print(f"[bold]Upgrading Evolve[/] (no install.json — treating as pre-v0.3 install)")

    # 1. Detect repo root
    if repo_path is not None:
        repo_root = repo_path
    else:
        candidate = Path(__file__).resolve().parent
        repo_root = None
        for _ in range(8):
            if (
                ((candidate / ".git").exists() or (candidate / "pyproject.toml").exists())
                and (candidate / "packages" / "analyzer").exists()
            ):
                repo_root = candidate
                break
            candidate = candidate.parent
        if repo_root is None:
            console.print("[red]Could not auto-detect repo root. Use --repo <path>.[/]")
            console.print("[dim]Looking for a parent with .git/ or pyproject.toml and packages/analyzer/[/]")
            sys.exit(1)

    console.print(f"[dim]Repo: {repo_root}[/]")

    git_result = None
    pkg_ok = False
    plugin_ok = False
    bot_results = {}

    # 2. git pull
    console.print("\n[bold]git pull[/]")
    if not dry_run:
        # When upgrade runs as root (sudo evolve-admin upgrade), git uses root's
        # SSH config which has no deploy key. Inject GIT_SSH_COMMAND to use the
        # evolve user's deploy key explicitly, mirroring what the repo-puller
        # daemon does when it pulls as the evolve user.
        pull_env = os.environ.copy()
        _deploy_key = Path("/Users/evolve/.ssh/evolve-repo")
        if _deploy_key.exists():
            pull_env["GIT_SSH_COMMAND"] = (
                f"ssh -i {_deploy_key} -o StrictHostKeyChecking=accept-new"
                " -o IdentitiesOnly=yes"
            )
        proc = subprocess.run(["git", "pull"], cwd=str(repo_root), env=pull_env)
        git_result = proc.returncode
        if proc.returncode != 0:
            console.print("[yellow]Warning: git pull returned non-zero (continuing)[/]")
        else:
            git_result = 0
    else:
        console.print("[dim][dry-run] Would run: git pull[/]")

    # 3. Reinstall admin package
    console.print("\n[bold]Reinstalling admin package[/]")
    python_exe = sys.executable
    if not dry_run:
        proc = subprocess.run(
            [python_exe, "-m", "pip", "install", "--no-cache-dir", "-e",
             str(repo_root / "packages" / "admin"), "-q"]
        )
        pkg_ok = proc.returncode == 0
        if pkg_ok:
            console.print("[green]  Package reinstalled[/]")
        else:
            console.print("[red]  Package reinstall failed[/]")
    else:
        console.print(f"[dim][dry-run] Would run: {python_exe} -m pip install -e packages/admin -q[/]")
        pkg_ok = True

    # 4. Rebuild TypeScript plugin
    #
    # Calls ``build_plugin()`` (deploy.py) instead of raw ``npm run build``
    # because ``build_plugin()`` ALSO syncs the compiled dist/ +
    # node_modules/ to ``/Users/Shared/evolve-plugin/`` (the
    # canonical install path that openclaw loads from). Doing only
    # ``npm run build`` updates the source-tree dist but leaves
    # ``/Users/Shared/evolve-plugin/dist/`` stale — bots keep loading
    # the previous plugin build, and operators get the very confusing
    # "I ran upgrade but the plugin still seems on old code"
    # experience (see PR #1145 motivation: admin_bot chimed in with
    # hallucinated commentary post-upgrade because the plugin install
    # didn't have PR #1142's before_prompt_build hook even though
    # the source rebuild succeeded).
    plugin_skip_reason: str | None = None
    if not skip_plugin:
        console.print("\n[bold]Rebuilding TypeScript plugin[/]")
        if not dry_run:
            node_bin = _shutil.which("node")
            if node_bin:
                # Verify Node version is new enough for TypeScript 5.x (requires 14+)
                ver_proc = subprocess.run([node_bin, "--version"], capture_output=True, text=True)
                ver_str = ver_proc.stdout.strip().lstrip("v")
                try:
                    node_major = int(ver_str.split(".")[0])
                except ValueError:
                    node_major = 99  # unparseable — let the build try
                if node_major < 14:
                    console.print(
                        f"  [red]Error:[/] Node.js {ver_str} is too old — "
                        f"TypeScript 5.x requires Node 14+ (Node 20 LTS recommended).\n"
                        f"  Upgrade with:  brew install node"
                    )
                    plugin_ok = False
                    plugin_skip_reason = f"Node {ver_str} too old"
                else:
                    try:
                        build_plugin()
                        plugin_ok = True
                        console.print("[green]  Plugin rebuilt + installed to /Users/Shared/evolve-plugin/[/]")
                    except Exception as build_exc:
                        plugin_ok = False
                        console.print(f"[red]  Plugin build failed:[/] {build_exc}")
            else:
                console.print(
                    "  [yellow]Warning:[/] Node.js not found — skipping plugin rebuild. "
                    "Run manually: cd packages/plugin && npm run build"
                )
                plugin_ok = False
                plugin_skip_reason = "no node"
        else:
            console.print("[dim][dry-run] Would run: npm install && npm run build in packages/plugin[/]")
            plugin_ok = True
    else:
        plugin_ok = True

    # 5. Redeploy to all bots
    network_path: Path = ctx.obj["network_path"]
    try:
        network = load_network(network_path)
    except Exception as e:
        console.print(f"[red]  Cannot load network.json: {e}[/]")
        network = {}

    # 5a. Remove orphaned plists + stale gateway units (Phase E.6: the plist
    # sweep misses the ``ai.openclaw.<bot>-gateway`` shape) before redeploying.
    console.print("\n[bold]Checking for orphaned jobs[/]")
    try:
        from .gateway_reaper import reap_stale_gateways
        removed, failures = remove_orphaned_plists(find_orphaned_plists(network), dry_run=dry_run)
        _found, gw_removed, gw_failures = reap_stale_gateways(network, dry_run=dry_run)
        removed, failures = removed + gw_removed, failures + gw_failures
        prefix = "[dim][dry-run][/] Would remove" if dry_run else "[green]✓[/] Removed"
        for label in removed:
            console.print(f"  {prefix}: {label}")
        for failure in failures:
            console.print(f"  [red]✗[/] Could not remove: {failure}")
        if not removed and not failures:
            console.print("  [green]✓[/] No orphaned jobs found")
    except Exception as e:
        console.print(f"  [yellow]⚠[/] Orphan check failed (non-fatal): {e}")

    if not skip_deploy:
        console.print("\n[bold]Redeploying to all bots[/]")
        bots_cfg = network.get("bots", {})
        members = network.get("members", [])
        # Capture per-bot deploy time anchors so the post-deploy
        # gateway-restart check has the right "began at" for each bot.
        # We use a single anchor (the loop start) — within a typical
        # multi-second deploy_bot run, the gateway always restarts at
        # the END (or doesn't restart at all, which is the bug we want
        # to catch).
        bot_deploy_began_at = _time.time()
        for bot_id in members:
            cfg = bots_cfg.get(bot_id, {})
            t_role = cfg.get("role") or "member"
            t_port = cfg.get("port")
            t_backup_url = cfg.get("backupRepoUrl", "")
            result = deploy_bot(bot_id, t_role, t_port, network_path, dry_run=dry_run, backup_repo_url=t_backup_url)
            bot_results[bot_id] = result.success
            icon = "[green]✓[/]" if result.success else "[red]✗[/]"
            console.print(f"  {icon} {bot_id}")
            if result.success and not dry_run:
                try:
                    record_bot_deploy(bot_id, shared_dir)
                except Exception as e:
                    console.print(f"  [yellow]⚠[/] Could not record deploy for {bot_id}: {e}")

    # 6. Restart the admin server so the reinstalled Python code is
    # actually loaded in the running process. Without this, pip
    # install -e updates the venv files but the long-running admin
    # daemon keeps stale modules in its sys.modules cache forever —
    # operators see "Evolve upgraded" but the admin server still
    # serves yesterday's code. Captured upgrade_began_at to verify
    # the restart actually swapped the process (PID start time >
    # upgrade start).
    from .deploy_verify import (
        verify_process_restarted, verify_bot_gateway_running_new_plugin,
        render_summary_block, all_ok, VerificationResult,
    )
    ADMIN_UI_LAUNCHD_LABEL = "ai.evolve.evolve.admin-ui"
    verifications: list = []
    if not dry_run:
        console.print("\n[bold]Restarting admin server[/]")
        try:
            restart_ok, restart_out = get_scheduler().restart(
                ADMIN_UI_LAUNCHD_LABEL)
            if not restart_ok and restart_out:
                # Pre-seam this stderr streamed straight to the terminal;
                # the seam captures it, so surface it for the operator.
                console.print(f"  [yellow]{restart_out}[/]")
            # Give launchd a moment to spawn the new process before
            # we probe for its PID. 2s is conservative; the kickstart
            # is typically sub-second.
            _time.sleep(2)
            verifications.append(verify_process_restarted(
                label=ADMIN_UI_LAUNCHD_LABEL,
                began_at_epoch=upgrade_began_at,
            ))
        except Exception as e:
            console.print(f"  [red]Admin restart failed:[/] {e}")
            verifications.append(VerificationResult(
                ok=False,
                summary=f"Daemon {ADMIN_UI_LAUNCHD_LABEL}: kickstart exception",
                detail=str(e),
            ))

    # 6b. Verify each bot's gateway picked up the new plugin —
    # i.e. its openclaw process restarted some time after the
    # deploy_bot loop began. Catches the failure mode where
    # deploy_bot succeeds at writing files but the gateway's
    # restart-trigger silently no-ops (per-bot launchd quirk,
    # already-stopped daemon, etc.) and the bot keeps running
    # the previous plugin build.
    if not skip_deploy and not dry_run and bot_results:
        for bot_id, ok in bot_results.items():
            if not ok:
                continue  # deploy itself failed — skip the gateway check
            verifications.append(
                verify_bot_gateway_running_new_plugin(
                    bot_id=bot_id,
                    deploy_began_at_epoch=bot_deploy_began_at,
                )
            )

    # 7. Write install.json with new version (if not dry-run)
    if not dry_run:
        try:
            try:
                _net = load_network(network_path)
            except Exception:
                _net = {}
            _network_id = _net.get("networkId", install_info.get("network_id", "unknown") if install_info else "unknown")
            _bots = list(_net.get("bots", {}).keys()) or (install_info.get("bots", []) if install_info else [])
            write_install_json(
                shared_dir=shared_dir,
                network_id=_network_id,
                bots=_bots,
                repo_path=str(repo_root),
            )
            console.print(f"  [green]✓[/] install.json updated → v{EVOLVE_VERSION}")
        except Exception as e:
            console.print(f"  [yellow]⚠[/] Could not update install.json: {e}")

    # 8. Print summary
    console.print(f"\n[bold green]Evolve upgraded to v{EVOLVE_VERSION}[/]")
    console.print(f"  git:     {'pulled' if git_result == 0 else 'pulled (see output above)'}")
    console.print(f"  package: {'reinstalled' if pkg_ok else 'FAILED'}")
    if not skip_plugin:
        if plugin_ok:
            _plugin_status = "rebuilt"
        elif plugin_skip_reason:
            _plugin_status = f"skipped ({plugin_skip_reason})"
        else:
            _plugin_status = "FAILED"
        console.print(f"  plugin:  {_plugin_status}")
    if not skip_deploy and bot_results:
        bots_line = "  ".join(
            f"{bid} {'OK' if ok else 'FAIL'}" for bid, ok in bot_results.items()
        )
        console.print(f"  bots:    {bots_line}")

    # 9. Verification block — surfaces any post-condition failures
    # operators would otherwise have to ssh in to discover. Each step
    # that has a verifier appended a VerificationResult above; render
    # them in a single block.
    verifications = [v for v in verifications if v is not None]
    if verifications:
        console.print()
        for line in render_summary_block(verifications).splitlines():
            console.print(line)
        if not all_ok(verifications):
            console.print(
                "\n[red]⚠ One or more post-deploy checks failed — "
                "see detail above. The upgrade may not be fully live.[/]"
            )
            ctx.exit(1)


# ── version ───────────────────────────────────────────────────────────────────

@main.command("repair-security_bot")
@click.option("--bot", "bot_id", default=None,
              help="Bot to repair (defaults to network.security.botId)")
@click.pass_context
def repair_security_bot(ctx: click.Context, bot_id: str | None) -> None:
    """Clean up the configured security/audit bot's openclaw.json — remove any
    partial evolve plugin entry.

    The security/audit bot is a read-only auditor and must NOT have an evolve
    plugin config.  This command:

    \b
      1. Resolves the bot from --bot or network.security.botId
      2. Reads /Users/<user>/.openclaw/openclaw.json
      3. Removes plugins.entries.evolve if present
      4. Writes the cleaned config back (<user>:staff, mode 644)
      5. Verifies /Users/<user> home permissions (755)

    Run as root (sudo evolve-admin repair-security_bot).
    """
    network_path: Path = ctx.obj["network_path"]
    result = repair_security_bot_config(bot_id=bot_id, network_path=network_path)

    if result.get("error"):
        console.print(f"[red]Error:[/] {result['error']}")
        raise SystemExit(1)

    for w in result.get("warnings", []):
        console.print(f"[yellow]Warning:[/] {w}")

    repaired_bot = result.get("bot_id") or "?"
    if result.get("changed"):
        console.print(f"[green]Removed plugins.entries.evolve from {repaired_bot}'s openclaw.json.[/]")
    else:
        console.print(f"[dim]{repaired_bot} openclaw.json already clean — no evolve entry found.[/]")

    console.print(f"[green]{repaired_bot} config permissions verified.[/]")


@main.command()
def version() -> None:
    """Show installed Evolve version."""
    from evolve_admin import __version__
    console.print(f"evolve-admin {__version__}")


@main.command()
@click.option("--keep-data", is_flag=True, help="Keep /Users/Shared/evolve data (metrics, proposals, etc.)")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def uninstall(ctx: click.Context, keep_data: bool, dry_run: bool) -> None:
    """Remove Evolve from all bots — launchd jobs, workspace scripts, optionally shared data."""
    import subprocess as _sp
    network_path: Path = ctx.obj["network_path"]

    console.print("[bold red]⚡ Evolve Uninstall[/]")
    console.print()

    if not network_path.exists():
        console.print("[yellow]No network.json found — nothing to uninstall.[/]")
        return

    network = load_network(network_path)
    bots = network.get("bots", {})
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

    # Collect all launchd labels
    launchd_prefixes = ["ai.openclaw.evolve."]
    from .deploy import LAUNCHD_DIR, _run_sudo, DeployResult

    import os as _os
    plists = list(Path("/Library/LaunchDaemons").glob("ai.openclaw.evolve.*.plist"))
    console.print(f"Found {len(plists)} launchd job(s) to remove:")
    for p in plists:
        console.print(f"  [dim]{p.name}[/]")

    scripts_to_remove = []
    for bot_id in bots:
        from .config import get_bot_workspace
        ws = get_bot_workspace(bot_id)
        if ws:
            evolve_dir = ws / "evolve"
            if evolve_dir.exists():
                scripts_to_remove.append((bot_id, evolve_dir))
                console.print(f"  Bot {bot_id}: remove {evolve_dir}")

    console.print()
    if not keep_data:
        console.print(f"[yellow]Will also remove shared data: {shared_dir}[/]")
        console.print("[yellow](pass --keep-data to preserve metrics and proposals)[/]")
    else:
        console.print(f"[dim]Shared data will be kept: {shared_dir}[/]")

    console.print()
    if not click.confirm("Proceed with uninstall?", default=False):
        console.print("Cancelled.")
        return

    result = DeployResult(bot_id="uninstall", success=True)

    # Remove launchd jobs — Scheduler.remove() is exactly this ritual:
    # bootout (rc ignored; not-loaded is fine) + sudo rm of the plist.
    for plist in plists:
        label = plist.stem
        if not dry_run:
            removed_ok, removed_msg = get_scheduler().remove(label)
            if not removed_ok:
                result.error(removed_msg)
        console.print(f"  [green]✓[/] Removed launchd: {label}")

    # Remove workspace scripts
    for bot_id, evolve_dir in scripts_to_remove:
        if not dry_run:
            _run_sudo(["rm", "-rf", str(evolve_dir)], result)
        console.print(f"  [green]✓[/] Removed scripts: {evolve_dir}")

    # Remove network.json
    if not dry_run:
        _run_sudo(["rm", "-f", str(network_path)], result)
    console.print(f"  [green]✓[/] Removed network config: {network_path}")

    # Optionally remove shared data
    if not keep_data and not dry_run:
        _run_sudo(["rm", "-rf", str(shared_dir)], result)
        console.print(f"  [green]✓[/] Removed shared data: {shared_dir}")

    console.print()
    if result.errors:
        for e in result.errors:
            console.print(f"  [red]✗[/] {e}")
        console.print("[yellow]Uninstall completed with errors.[/]")
    else:
        console.print("[bold green]✓ Uninstall complete.[/]")
        console.print()
        console.print("[dim]The evolve-venv and evolve-repo in /Users/Shared/ were not removed.[/]")
        console.print("[dim]To fully clean up: sudo rm -rf /Users/Shared/evolve-venv /Users/Shared/evolve-repo /usr/local/bin/evolve-admin[/]")


# ── backfill ──────────────────────────────────────────────────────────────────

@main.command()
@click.argument("bot_id")
@click.option("--days", default=30, show_default=True, help="Days of history to backfill")
@click.pass_context
def backfill(ctx: click.Context, bot_id: str, days: int) -> None:
    """Backfill historical metrics from OC turn logs.

    Reads /Users/<bot>/.openclaw/workspace/memory/turns-YYYY-MM-DD.jsonl
    and writes estimated metrics to the shared metrics directory.
    Skips days where real (non-backfilled) metrics already exist.

    Examples:
      evolve-admin backfill admin_bot --days 30
      evolve-admin backfill team_bot_a --days 14
    """

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    bot_user = get_bot_user(bot_id, network)
    bot_home_path = _bot_home(bot_id, network)
    console.print(f"[bold]Backfilling {days} days of history for {bot_id}[/]")
    console.print(f"[dim]Reading:  {bot_home_path}/.openclaw/workspace/memory/turns-*.jsonl[/]")
    console.print(f"[dim]Writing:  {shared_dir}/metrics/YYYY-MM-DD/{bot_id}.json[/]")
    console.print()

    result = subprocess.run(
        ["sudo", "-u", bot_user, "env", f"HOME={bot_home_path}",
         sys.executable, "-m", "evolve_admin.backfill_runner",
         "--bot-id", bot_id, "--days", str(days),
         "--shared-dir", str(shared_dir)],
        cwd=get_profile().scratch_dir, capture_output=True, text=True
    )

    if result.stdout:
        console.print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            console.print(f"[red]{result.stderr.rstrip()}[/]")
        sys.exit(1)
    else:
        console.print(f"\n[green]✓ Backfill complete for {bot_id}[/]")


# ── cost-events backfill ──────────────────────────────────────────────────────

@main.command("backfill-cost-events")
@click.argument("bot_id")
@click.option("--days", default=14, show_default=True,
              help="Days of history to backfill (counted back from today UTC)")
@click.option("--dry-run", is_flag=True, default=False,
              help="Compute counts but do not write")
@click.pass_context
def backfill_cost_events(
    ctx: click.Context, bot_id: str, days: int, dry_run: bool,
) -> None:
    """Backfill cost_event records from OC turn logs.

    Replaces the broken plugin llm_output → cost_event emission path.
    Reads /Users/<bot>/.openclaw/workspace/memory/turns-YYYY-MM-DD.jsonl
    (written by /Users/Shared/openclaw-usage/turn-collector.py — see PR
    description) and writes canonical cost_event records to:

        {sharedDir}/annotations/<bot>/cost_events-<date>.jsonl

    These are read by analyzer.cost_ledger via observations.access. The
    same converter is installed as a per-bot launchd job
    (ai.openclaw.evolve.cost-converter.<bot>) by `evolve-admin deploy`,
    running every 15 minutes — this command is for one-shot historical
    fills and post-hoc spot-checks.

    Idempotent: dedup by (ts, session_id), so re-running over the same
    window adds no duplicates.

    Examples:
      evolve-admin backfill-cost-events admin_bot --days 14
      evolve-admin backfill-cost-events team_bot_a --days 30 --dry-run
    """
    import subprocess

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    bot_user = get_bot_user(bot_id, network)
    bot_home_path = _bot_home(bot_id, network)
    converter = (
        Path(__file__).parent.parent.parent / "analyzer" / "cost_event_converter.py"
    )

    console.print(f"[bold]Backfilling {days} days of cost_event records for {bot_id}[/]")
    console.print(f"[dim]Reading:  {bot_home_path}/.openclaw/workspace/memory/turns-*.jsonl[/]")
    console.print(f"[dim]Writing:  {shared_dir}/annotations/{bot_id}/cost_events-*.jsonl[/]")
    console.print()

    cmd = [
        "sudo", "-u", bot_user, "env", f"HOME={bot_home_path}",
        sys.executable, str(converter),
        "--bot-id", bot_id,
        "--shared-dir", str(shared_dir),
        "--backfill", str(days),
    ]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, cwd=get_profile().scratch_dir, capture_output=True, text=True)

    if result.stdout:
        console.print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            console.print(f"[red]{result.stderr.rstrip()}[/]")
        sys.exit(1)
    else:
        console.print(f"\n[green]✓ Cost-event backfill complete for {bot_id}[/]")


# ── migrate-jobs ──────────────────────────────────────────────────────────────

_LAUNCHD_DIR = Path("/Library/LaunchDaemons")

_PLISTS_DIR = Path(__file__).parent / "plists"


def _step_ok(label: str) -> None:
    console.print(f"  [green]✓[/] {label}")


def _step_fail(label: str, reason: str) -> None:
    console.print(f"  [red]✗[/] {label}: {reason}")


def _run(cmd: list[str], *, check: bool = True) -> "subprocess.CompletedProcess[str]":
    import subprocess as _sp
    return _sp.run(cmd, capture_output=True, text=True, check=check)


@main.command("migrate-jobs")
@click.option("--dry-run", is_flag=True, default=False, help="Print steps without executing")
def migrate_jobs(dry_run: bool) -> None:
    """Migrate heal and measure launchd jobs to the evolve user.

    heal.py and measure.py are pod-wide jobs — they monitor all bots and
    read from the shared directory. They belong on the evolve user, not per-bot.

    apply.py is NOT migrated. It intentionally runs as each bot's own user
    so it can patch that bot's openclaw.json without cross-user writes or sudo.
    apply.py is installed per-bot by `evolve-admin deploy <bot>`.

    Steps:
      1. Install heal and measure plists as evolve-owned LaunchDaemons
      2. Remove old per-bot versions of heal, measure, analyze, report,
         expansion, and outcome (all now evolve-owned). Also sweeps the
         retired classifier-audit jobs from older installs.

    Must be run with sudo (writes to /Library/LaunchDaemons).
    """
    import subprocess as _sp
    import os as _os

    if not dry_run and _os.geteuid() != 0:
        console.print("[red]migrate-jobs must be run with sudo.[/]")
        sys.exit(1)

    console.print(f"\n[bold]Evolve Job Migration[/]{'  [dim](dry run)[/]' if dry_run else ''}\n")

    plist_files = sorted(_PLISTS_DIR.glob("*.plist"))
    if not plist_files:
        console.print(f"[red]No plist files found in {_PLISTS_DIR}[/]")
        sys.exit(1)

    # ── Step 1: Install evolve-owned plists (heal + measure + better) ───────────
    console.print("[bold]Step 1: Installing evolve-owned plists[/]")
    installed: list[str] = []
    failed: list[str] = []

    # Pre-flight: ensure the shared log dir and better-engine dir exist.
    # These plists write to /Users/Shared/evolve/logs/ which launchd requires
    # to exist at bootstrap time (missing dir → error 5: I/O error).
    # Also ensure better-engine dir is owned by evolve so the server can write.
    # Use -R (recursive) for better-engine so any existing files inside it
    # (e.g. recommendations.json / recommendations.tmp created when the server
    # was run manually as pod_admin) are also fixed — otherwise evolve cannot
    # overwrite those files even though it owns the directory.
    if not dry_run:
        for _dir, _owner, _recursive in [
            (Path("/Users/Shared/evolve/logs"), "evolve", False),
            (Path("/Users/Shared/evolve/better-engine"), "evolve", True),
        ]:
            try:
                _run(["/bin/mkdir", "-p", str(_dir)])
                chown_cmd = [get_profile().chown]
                if _recursive:
                    chown_cmd.append("-R")
                chown_cmd += [_owner, str(_dir)]
                _run(chown_cmd)
                _run(["/bin/chmod", "755", str(_dir)])
            except _sp.CalledProcessError:
                pass  # Non-fatal — bootstrap will surface the real error

    for plist_path in plist_files:
        label = plist_path.stem
        dest = _LAUNCHD_DIR / plist_path.name

        if dry_run:
            console.print(f"  [dim][dry-run] would install {plist_path.name}[/]")
            installed.append(label)
            continue

        try:
            _run(["cp", str(plist_path), str(dest)])
            _step_ok(f"cp {plist_path.name}")
        except _sp.CalledProcessError as e:
            _step_fail(f"cp {plist_path.name}", e.stderr.strip())
            failed.append(label)
            continue

        try:
            _run(["chown", "root:wheel", str(dest)])
            _step_ok(f"chown root:wheel {plist_path.name}")
        except _sp.CalledProcessError as e:
            _step_fail(f"chown {plist_path.name}", e.stderr.strip())
            failed.append(label)
            continue

        try:
            _run(["chmod", "644", str(dest)])
            _step_ok(f"chmod 644 {plist_path.name}")
        except _sp.CalledProcessError as e:
            _step_fail(f"chmod {plist_path.name}", e.stderr.strip())
            failed.append(label)
            continue

        # Bootout first (ignore error — job may not be loaded yet).
        # raw (not Scheduler.remove()): must keep the plist we just staged.
        # The seam sudo-prefixes; this command already requires root, where
        # the NOPASSWD sudo is a no-op.
        rc, _out, _err = get_launchd_scheduler().raw(
            "bootout", f"system/{label}")
        if rc == 0:
            _step_ok(f"bootout {label}")
        # Non-zero rc: not loaded — fine

        # raw (not Scheduler.install()): bootstrap of the static plist file
        # shipped in the package and cp'd above — there is no JobSpec, and
        # install() owns the plist write + byte-identical skip.
        rc, _out, err_s = get_launchd_scheduler().raw(
            "bootstrap", "system", str(dest))
        if rc == 0:
            _step_ok(f"bootstrap {label}")
            installed.append(label)
        else:
            err = err_s.strip()
            if "119" in err or "already" in err.lower():
                _step_ok(f"bootstrap {label} (already loaded)")
                installed.append(label)
            else:
                _step_fail(f"bootstrap {label}", err)
                failed.append(label)

    # ── Step 2: Remove old per-bot jobs ───────────────────────────────────────
    # These jobs previously ran as admin_bot/team_bot_a. They are now consolidated under
    # the evolve user (via install_evolve_infra_jobs or the plists above).
    # apply.*.admin_bot and apply.*.team_bot_a are NOT in this list — apply stays per-bot.
    console.print("\n[bold]Step 2: Removing old per-bot jobs[/]")

    old_labels = [
        "ai.openclaw.evolve.measure.admin_bot",
        "ai.openclaw.evolve.measure.team_bot_a",
        "ai.openclaw.evolve.heal.admin_bot",
        "ai.openclaw.evolve.analyze.admin_bot",
        # classifier-audit was retired 2026-05-23; sweep both the old
        # per-bot variant and the consolidated evolve-owned plist from
        # any existing install.
        "ai.openclaw.evolve.classifier-audit.admin_bot",
        "ai.openclaw.evolve.classifier-audit.evolve",
        "ai.openclaw.evolve.expansion.admin_bot",
        "ai.openclaw.evolve.outcome.admin_bot",
        "ai.openclaw.evolve.report.admin_bot",
    ]

    removed: list[str] = []
    skipped: list[str] = []

    for label in old_labels:
        plist_path = _LAUNCHD_DIR / f"{label}.plist"

        if dry_run:
            if plist_path.exists():
                console.print(f"  [dim][dry-run] would remove {label}[/]")
                removed.append(label)
            else:
                console.print(f"  [dim][dry-run] not present: {label}[/]")
                skipped.append(label)
            continue

        if not plist_path.exists():
            console.print(f"  [dim]not present (skip): {label}[/]")
            skipped.append(label)
            continue

        # raw bootout + separate rm (not Scheduler.remove()): keeps the
        # legacy per-step ✓/✗ output contract — remove() collapses both
        # steps into one result. rc ignored: not-loaded is fine.
        get_launchd_scheduler().raw("bootout", f"system/{label}")
        _step_ok(f"bootout {label}")

        try:
            _run(["rm", str(plist_path)])
            _step_ok(f"rm {plist_path.name}")
            removed.append(label)
        except _sp.CalledProcessError as e:
            _step_fail(f"rm {label}", e.stderr.strip())

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print("\n[bold]Migration summary[/]")
    console.print(f"  Installed: [green]{len(installed)}[/] — {', '.join(installed) or 'none'}")
    console.print(f"  Removed:   [green]{len(removed)}[/] — {', '.join(removed) or 'none'}")
    if skipped:
        console.print(f"  Skipped:   [dim]{len(skipped)}[/] (not present)")
    if failed:
        console.print(f"  Failed:    [red]{len(failed)}[/] — {', '.join(failed)}")
        sys.exit(1)
    console.print()
    console.print("  [green]Done.[/] apply.py continues running per-bot (admin_bot/team_bot_a) as intended.")


# ── pod-conduct ───────────────────────────────────────────────────────────────

@main.group("pod-conduct")
def pod_conduct() -> None:
    """Manage POD_CONDUCT.md injection across the pod."""


@pod_conduct.command("inject")
@click.argument("bot_id", required=False, default=None, metavar="BOT")
@click.option("--all", "all_bots", is_flag=True, default=False, help="Inject into all bots in network.json")
@click.pass_context
def pod_conduct_inject(ctx: click.Context, bot_id: str | None, all_bots: bool) -> None:
    """Inject POD_CONDUCT.md into a bot's openclaw.json contextFiles.

    Examples:
      evolve-admin pod-conduct inject admin_bot
      evolve-admin pod-conduct inject --all
    """
    if not bot_id and not all_bots:
        raise click.UsageError("Specify BOT or --all")

    network_path: Path = ctx.obj["network_path"]

    if all_bots:
        network = load_network(network_path)
        targets = network.get("members", [])
        if not targets:
            console.print("[red]No members in network.json[/]")
            sys.exit(1)
    else:
        targets = [bot_id]

    for target in targets:
        try:
            inject_pod_conduct(target)
            console.print(f"[green]✓ POD_CONDUCT.md injected into {target}[/]")
        except Exception as e:
            console.print(f"[red]✗ {target}: {e}[/]")


# ── service ───────────────────────────────────────────────────────────────────

@main.group()
def service() -> None:
    """Manage the evolve-admin server as a macOS launchd service.

    The service keeps the admin UI running persistently — starts at login,
    restarts on crash, no terminal session required.

    Typical first-time setup:
      evolve-admin service install
      evolve-admin service status
    """


@service.command("install")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address for the server")
@click.option("--port", default=5050, show_default=True,
              help="Port for the server")
def service_install(host: str, port: int) -> None:
    """Install and start the admin server as a launchd LaunchAgent."""
    from .service import install as _install, PLIST_PATH, LOG_PATH
    console.print(f"[bold]Installing evolve-admin service…[/]")
    console.print(f"  Plist: {PLIST_PATH}")
    console.print(f"  Log:   {LOG_PATH}")
    console.print(f"  URL:   http://{host}:{port}/")
    ok, msg = _install(host=host, port=port)
    if ok:
        console.print(f"[green]✓ {msg}[/]")
    else:
        console.print(f"[red]✗ {msg}[/]")
        sys.exit(1)


@service.command("uninstall")
def service_uninstall() -> None:
    """Stop and uninstall the launchd service."""
    from .service import uninstall as _uninstall
    ok, msg = _uninstall()
    if ok:
        console.print(f"[green]✓ {msg}[/]")
    else:
        console.print(f"[red]✗ {msg}[/]")
        sys.exit(1)


@service.command("start")
def service_start() -> None:
    """Start the service (must be installed)."""
    from .service import start as _start
    ok, msg = _start()
    if ok:
        console.print(f"[green]✓ Started[/]")
    else:
        console.print(f"[red]✗ {msg}[/]")
        sys.exit(1)


@service.command("stop")
def service_stop() -> None:
    """Stop the service without uninstalling it."""
    from .service import stop as _stop
    ok, msg = _stop()
    if ok:
        console.print(f"[green]✓ Stopped[/]")
    else:
        console.print(f"[red]✗ {msg}[/]")
        sys.exit(1)


@service.command("restart")
def service_restart() -> None:
    """Restart the service."""
    from .service import restart as _restart
    ok, msg = _restart()
    if ok:
        console.print(f"[green]✓ Restarted[/]")
    else:
        console.print(f"[red]✗ {msg}[/]")
        sys.exit(1)


@service.command("status")
def service_status() -> None:
    """Show service status (installed, running, PID)."""
    from .service import status as _status, PLIST_PATH, LOG_PATH
    s = _status()

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="dim", min_width=12)
    table.add_column("Value")

    if s.get("installed"):
        table.add_row("Installed", f"[green]yes[/] ({PLIST_PATH})")
    else:
        table.add_row("Installed", "[yellow]no[/] — run 'evolve-admin service install'")

    if s.get("managed"):
        table.add_row("Registered", "[green]yes[/] (launchd knows about it)")
    else:
        table.add_row("Registered", "[dim]no (plist present but not loaded)[/]")

    if s.get("running"):
        table.add_row("Running", f"[green]yes[/] — PID {s.get('pid')}")
    else:
        table.add_row("Running", "[red]no[/]")

    if s.get("last_exit") is not None:
        color = "green" if str(s["last_exit"]) == "0" else "red"
        table.add_row("Last exit", f"[{color}]{s['last_exit']}[/{color}]")

    table.add_row("Log file", str(LOG_PATH))

    console.print(Panel(table, title="evolve-admin service", border_style="dim"))


@service.command("logs")
@click.option("-n", default=50, show_default=True, help="Number of lines to show")
def service_logs(n: int) -> None:
    """Tail the admin server log."""
    from .service import tail_logs
    lines = tail_logs(n)
    if not lines:
        console.print("[dim](no log output)[/]")
    else:
        for line in lines:
            console.print(line)


# ── mcp-bridge ────────────────────────────────────────────────────────────────

@main.group("mcp-bridge")
def mcp_bridge() -> None:
    """Manage the Evolve MCP Bridge for Claude Desktop / Dispatch integration.

    The bridge exposes your pod's context (all bot memory, tasks, handoffs)
    as MCP tools that Claude Desktop can access over Tailscale.

    Quick start:
      evolve-admin mcp-bridge install
      evolve-admin mcp-bridge status
    """


@mcp_bridge.command("install")
@click.option("--host", default="0.0.0.0", show_default=True,
              help="Bind address (0.0.0.0 to accept Tailscale connections)")
@click.option("--port", default=5051, show_default=True, help="HTTP port")
@click.option("--network", default=None, help="Path to network.json (default: standard location)")
def mcp_bridge_install(host: str, port: int, network: str | None) -> None:
    """Install and start the MCP bridge as a system-scope LaunchDaemon.

    Requires sudo (writes to /Library/LaunchDaemons/). Pre-2026-05-30 the
    bridge was a per-user LaunchAgent under ~/Library/LaunchAgents/; that
    scope structurally can't load on headless pods (no Aqua session for
    admin user) so it was converted to system-scope.
    """
    from pathlib import Path
    from .mcp_service import install as _install, PLIST_PATH, LOG_PATH
    net_path = Path(network) if network else None
    kwargs = {"host": host, "port": port}
    if net_path:
        kwargs["network"] = net_path
    console.print(f"[bold]Installing Evolve MCP Bridge…[/]")
    ok, msg = _install(**kwargs)
    if ok:
        console.print(f"[green]✓[/] {msg}")
        console.print(f"  Plist: {PLIST_PATH}")
        console.print(f"  Log:   {LOG_PATH}")
        console.print(f"\n[bold]Add to Claude Desktop config[/] "
                      f"(~/Library/Application Support/Claude/claude_desktop_config.json):")
        console.print(f'  {{"mcpServers": {{"evolve-pod": {{"url": "http://localhost:{port}/sse"}}}}}}')
    else:
        console.print(f"[red]✗[/] {msg}")
        raise SystemExit(1)


@mcp_bridge.command("uninstall")
def mcp_bridge_uninstall() -> None:
    """Stop and uninstall the MCP bridge launchd service."""
    from .mcp_service import uninstall as _uninstall
    ok, msg = _uninstall()
    if ok:
        console.print(f"[green]✓[/] {msg}")
    else:
        console.print(f"[red]✗[/] {msg}")
        raise SystemExit(1)


@mcp_bridge.command("start")
def mcp_bridge_start() -> None:
    """Start the MCP bridge (must be installed)."""
    from .mcp_service import start as _start
    ok, msg = _start()
    console.print(("[green]✓[/] " if ok else "[red]✗[/] ") + msg)
    if not ok:
        raise SystemExit(1)


@mcp_bridge.command("stop")
def mcp_bridge_stop() -> None:
    """Stop the MCP bridge without uninstalling it."""
    from .mcp_service import stop as _stop
    ok, msg = _stop()
    console.print(("[green]✓[/] " if ok else "[red]✗[/] ") + msg)


@mcp_bridge.command("restart")
def mcp_bridge_restart() -> None:
    """Restart the MCP bridge (also sends SIGHUP to refresh bot registry)."""
    from .mcp_service import restart as _restart
    ok, msg = _restart()
    console.print(("[green]✓[/] " if ok else "[red]✗[/] ") + msg)
    if not ok:
        raise SystemExit(1)


@mcp_bridge.command("reload")
def mcp_bridge_reload() -> None:
    """Send SIGHUP to the running bridge to reload bot registry from network.json.

    Use this after adding or removing bots — no restart required.
    Evolve calls this automatically when provisioning or deprovisioning bots.
    """
    from .mcp_service import reload_registry as _reload
    ok, msg = _reload()
    console.print(("[green]✓[/] " if ok else "[red]✗[/] ") + msg)


@mcp_bridge.command("status")
def mcp_bridge_status() -> None:
    """Show MCP bridge status (installed, running, PID)."""
    from rich.table import Table
    from rich.panel import Panel
    from .mcp_service import status as _status, PLIST_PATH, LOG_PATH
    s = _status()

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold dim", width=18)
    table.add_column()

    if not s.get("installed"):
        table.add_row("Installed", "[yellow]no[/] — run 'evolve-admin mcp-bridge install'")
    elif not s.get("managed"):
        table.add_row("Installed", "[yellow]yes (not loaded)[/]")
        table.add_row("Managed by launchd", "[red]no[/]")
    elif s.get("running"):
        table.add_row("Status", "[green]● running[/]")
        table.add_row("PID", str(s.get("pid", "?")))
    else:
        table.add_row("Status", "[red]● stopped[/]")
        if s.get("last_exit") and s["last_exit"] != "0":
            table.add_row("Last exit", f"[red]{s['last_exit']}[/] (non-zero — may have crashed)")

    table.add_row("Plist", str(PLIST_PATH))
    table.add_row("Log", str(LOG_PATH))

    console.print(Panel(table, title="evolve-mcp-bridge", border_style="dim"))


@mcp_bridge.command("logs")
@click.option("-n", default=50, show_default=True, help="Number of lines to show")
def mcp_bridge_logs(n: int) -> None:
    """Tail the MCP bridge log."""
    from .mcp_service import tail_logs
    lines = tail_logs(n)
    if not lines:
        console.print("[dim](no log output)[/]")
    else:
        for line in lines:
            console.print(line)


@mcp_bridge.command("config")
@click.option("--remote/--local", default=False,
              help="Generate config for remote (Tailscale) or local (same machine) connection")
def mcp_bridge_config(remote: bool) -> None:
    """Print the Claude Desktop config snippet to paste into claude_desktop_config.json."""
    from .config import load_network, DEFAULT_NETWORK_CONFIG
    import json
    net = load_network(DEFAULT_NETWORK_CONFIG)
    mcp_cfg = net.get("mcp_bridge", {})
    port = mcp_cfg.get("port", 5051)
    tailscale_host = mcp_cfg.get("tailscale_hostname", "")

    if remote:
        if not tailscale_host:
            console.print("[red]✗[/] tailscale_hostname is not set in network.json mcp_bridge config.")
            console.print("  Add: \"tailscale_hostname\": \"mini.tail1234.ts.net\" to mcp_bridge in network.json")
            raise SystemExit(1)
        url = f"http://{tailscale_host}:{port}/sse"
        label = "Remote (Tailscale)"
    else:
        url = f"http://localhost:{port}/sse"
        label = "Local (same machine)"

    auth_mode = mcp_cfg.get("auth", {}).get("mode", "tailscale")
    snippet: dict = {"mcpServers": {"evolve-pod": {"url": url}}}

    if auth_mode == "api_key":
        api_key = mcp_cfg.get("auth", {}).get("api_key", "<your-api-key>")
        snippet["mcpServers"]["evolve-pod"]["headers"] = {
            "Authorization": f"Bearer {api_key}"
        }

    console.print(f"\n[bold]Claude Desktop config snippet ({label})[/]")
    console.print(f"[dim]File: ~/Library/Application Support/Claude/claude_desktop_config.json[/]")
    console.print()
    console.print(json.dumps(snippet, indent=2))
    console.print()
    console.print("[dim]Restart Claude Desktop after updating the config.[/]")


# ── report ────────────────────────────────────────────────────────────────────

@main.group()
def report() -> None:
    """Build and send diagnostic reports to help debug issues."""


@report.command("save")
@click.option("--note", default="", help="Brief description of the problem")
@click.option("--no-config", "skip_config", is_flag=True, default=False)
@click.pass_context
def report_save(ctx: click.Context, note: str, skip_config: bool) -> None:
    """Build and save a diagnostic report to ~/.evolve/reports/ (no email)."""
    from .reporter import build_report, save_report_to_file
    from .telemetry import setup_logging
    setup_logging()
    network_path: Path = ctx.obj["network_path"]
    console.print("[bold]Building diagnostic report…[/]")
    rpt = build_report(network_path, note=note, include_config=not skip_config)
    path = save_report_to_file(rpt)
    console.print(f"[green]✓ Report saved: {path}[/]")
    errors = rpt.get("recent_errors", [])
    if errors:
        console.print(f"[yellow]  {len(errors)} recent error lines found in log.[/]")


@report.command("show")
@click.option("--note", default="", help="Brief description of the problem")
@click.pass_context
def report_show(ctx: click.Context, note: str) -> None:
    """Print a diagnostic report to the terminal."""
    from .reporter import build_report, format_report_text
    from .telemetry import setup_logging
    setup_logging()
    network_path: Path = ctx.obj["network_path"]
    console.print("[bold]Building diagnostic report…[/]\n")
    rpt = build_report(network_path, note=note)
    console.print(format_report_text(rpt))


# ── Interactive admin menu ────────────────────────────────────────────────────
# `evolve-admin menu` is the canonical name. `evolve-admin oc` is a hidden
# alias kept for backwards compatibility with operators' muscle memory.
main.add_command(menu_group)
main.add_command(oc_group)
from .applications.cli_stamp_deliveries import register_cli as _register_stamp_deliveries; _register_stamp_deliveries(application)  # noqa: E402,E702 — body in helper keeps cli.py under its no-growth cap
from .applications.restore_cli import register_cli as _register_restore_manifest; _register_restore_manifest(application); from .applications.app_cron_cli import register_cli as _register_repair_app_crons; _register_repair_app_crons(application); from .purge_ingested_cli import register_cli as _register_purge_ingested; _register_purge_ingested(main)  # noqa: E402,E702 — bodies in helpers keep cli.py under its no-growth cap (restore-manifest + repair-app-crons + META:footprint _ingested-purge registrations folded onto one line)


# ── Evo subcommand surface ────────────────────────────────────────────────────
# Spec: docs/spec-evo-wizard-2026-05-05.md.

@click.group("evo")
def evo_group() -> None:
    """Manage the evo keyword surface — primary-user claims, identity, etc."""


@evo_group.command("claim-primary")
@click.argument("bot_id")
@click.option("--channel", required=True,
              help="Channel name (e.g. telegram, slack, discord)")
@click.option("--external-id", "external_id", required=True,
              help="The user's stable ID on that channel "
                   "(Telegram chat_id, Slack member ID, etc.)")
@click.option("--pod-user", "pod_user", default=None,
              help="Optional pod-level identifier for the primary user")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing claim on the same channel "
                   "(prefer `evo reassign-primary` for known transfers)")
@click.option("--reason", default=None,
              help="Reason recorded in the audit log")
@click.pass_context
def evo_claim_primary(
    ctx: click.Context,
    bot_id: str,
    channel: str,
    external_id: str,
    pod_user: str | None,
    force: bool,
    reason: str | None,
) -> None:
    """Record a primary user's external ID on a channel for a bot.

    Until the wizard captures identity automatically, this is how a primary
    user is registered for a bot. Subsequent `evo` commands from that
    external ID are recognized as primary; everyone else becomes secondary.

    Example:
        sudo evolve-admin evo claim-primary team_bot_a \\
            --channel slack --external-id U123ABC
    """
    from .evo import audit as _audit, identity as _identity
    network_path: Path = ctx.obj["network_path"]

    network = load_network(network_path)
    channel_lc = channel.strip().lower()

    prior = (
        ((network.get("bots") or {}).get(bot_id) or {})
        .get("primary_user", {})
        .get("external_ids", {})
        .get(channel_lc)
    )

    try:
        primary_block = _identity.claim_primary(
            network,
            bot_id,
            channel=channel,
            external_id=external_id,
            pod_user=pod_user,
            force=force,
        )
    except _identity.ClaimError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    try:
        save_network(network, network_path)
    except (PermissionError, OSError, RuntimeError) as e:
        console.print(f"[red]✗[/] failed to save network.json: {e}")
        sys.exit(2)

    new_value = external_id.strip()
    if str(prior or "") != new_value:
        try:
            shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
            _audit.append_event(
                shared_dir,
                actor=_audit.cli_actor(),
                action="claim",
                bot_id=bot_id,
                channel=channel_lc,
                from_external_id=(str(prior) if prior is not None else None),
                to_external_id=new_value,
                force=force,
                reason=reason,
            )
        except (PermissionError, OSError) as e:
            console.print(
                f"[yellow]⚠[/] network.json saved but audit write failed: {e}"
            )
            sys.exit(3)

    console.print(
        f"[green]✓[/] claimed [bold]{bot_id}[/] primary on "
        f"[bold]{channel_lc}[/] = {external_id!r}"
    )
    if pod_user:
        console.print(f"  pod_user: {pod_user}")
    other_channels = [
        c for c in (primary_block.get("external_ids") or {})
        if c != channel_lc
    ]
    if other_channels:
        console.print(
            f"  other channels recorded for this bot: "
            f"{', '.join(sorted(other_channels))}"
        )


@evo_group.command("reassign-primary")
@click.argument("bot_id")
@click.option("--channel", required=True,
              help="Channel to reassign on (e.g. telegram, slack, discord)")
@click.option("--from", "from_id", required=True,
              help="The current external_id (verified before mutation)")
@click.option("--to", "to_id", required=True,
              help="The new external_id to record as primary")
@click.option("--pod-user", "pod_user", default=None,
              help="Optional pod-level identifier for the new primary")
@click.option("--reason", default=None,
              help="Reason recorded in the audit log "
                   "(e.g. 'transferring ownership to Bob')")
@click.pass_context
def evo_reassign_primary(
    ctx: click.Context,
    bot_id: str,
    channel: str,
    from_id: str,
    to_id: str,
    pod_user: str | None,
    reason: str | None,
) -> None:
    """Transfer the primary on a channel from one external ID to another.

    Verifies the current value matches --from before mutating, so a typo
    or stale assumption can't silently overwrite the wrong person. Use
    `evo show-identity <bot>` first to see the current value.

    Example:
        sudo evolve-admin evo reassign-primary team_bot_a \\
            --channel slack --from U123ABC --to U456DEF \\
            --reason "transferring ownership to Bob"
    """
    from .evo import audit as _audit, identity as _identity
    network_path: Path = ctx.obj["network_path"]

    network = load_network(network_path)
    channel_lc = channel.strip().lower()

    try:
        primary_block = _identity.reassign_primary(
            network,
            bot_id,
            channel=channel,
            from_external_id=from_id,
            to_external_id=to_id,
            pod_user=pod_user,
        )
    except _identity.ClaimError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    try:
        save_network(network, network_path)
    except (PermissionError, OSError, RuntimeError) as e:
        console.print(f"[red]✗[/] failed to save network.json: {e}")
        sys.exit(2)

    if from_id.strip() != to_id.strip():
        try:
            shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
            _audit.append_event(
                shared_dir,
                actor=_audit.cli_actor(),
                action="reassign",
                bot_id=bot_id,
                channel=channel_lc,
                from_external_id=from_id.strip(),
                to_external_id=to_id.strip(),
                force=False,
                reason=reason,
            )
        except (PermissionError, OSError) as e:
            console.print(
                f"[yellow]⚠[/] network.json saved but audit write failed: {e}"
            )
            sys.exit(3)

    console.print(
        f"[green]✓[/] reassigned [bold]{bot_id}[/] on [bold]{channel_lc}[/]: "
        f"{from_id.strip()!r} → {to_id.strip()!r}"
    )
    if reason:
        console.print(f"  reason: {reason}")
    if pod_user:
        console.print(f"  pod_user: {pod_user}")


@evo_group.command("show-identity")
@click.argument("bot_id", required=False, default=None)
@click.pass_context
def evo_show_identity(ctx: click.Context, bot_id: str | None) -> None:
    """Show recorded primary_user identity for a bot, or all bots if no
    BOT_ID is given."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    members = network.get("members") or []
    bots = network.get("bots") or {}

    if bot_id is not None:
        if bot_id not in members:
            console.print(f"[yellow]⚠[/] {bot_id} is not a pod member")
        block = (bots.get(bot_id) or {}).get("primary_user") or {}
        _print_identity_block(bot_id, block)
        return

    if not members:
        console.print("[dim]no bots configured[/]")
        return
    for b in members:
        block = (bots.get(b) or {}).get("primary_user") or {}
        _print_identity_block(b, block)


def _print_identity_block(bot_id: str, block: dict) -> None:
    external_ids = block.get("external_ids") or {}
    pod_user = block.get("pod_user")
    if not external_ids and not pod_user:
        console.print(f"[bold]{bot_id}[/]: [dim]no primary recorded[/]")
        return
    console.print(f"[bold]{bot_id}[/]:")
    for ch, ext_id in sorted(external_ids.items()):
        console.print(f"  {ch}: {ext_id}")
    if pod_user:
        console.print(f"  pod_user: {pod_user}")


@evo_group.command("set-passphrase")
@click.option("--admin", "admin_pp", default=None,
              help="New admin passphrase (the word that proves pod-admin status)")
@click.option("--primary", "primary_pp", default=None,
              help="New primary passphrase (the word that proves bot-primary status)")
@click.pass_context
def evo_set_passphrase(
    ctx: click.Context,
    admin_pp: str | None,
    primary_pp: str | None,
) -> None:
    """Set or rotate the pod's admin / primary passphrases.

    Pod admin tells primary users "type `evo claim darwin`" and the system
    records them. Both passphrases are matched case-insensitively after
    stripping whitespace, so "Darwin", "DARWIN", and " darwin " all work.

    Either or both options may be provided. To rotate just the admin word
    while leaving primary alone, pass only --admin.
    """
    if admin_pp is None and primary_pp is None:
        console.print("[yellow]⚠[/] nothing to do — pass --admin or --primary")
        sys.exit(1)

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod

    if admin_pp is not None:
        pod["admin_passphrase"] = admin_pp.strip()
    if primary_pp is not None:
        pod["primary_passphrase"] = primary_pp.strip()

    try:
        save_network(network, network_path)
    except (PermissionError, OSError, RuntimeError) as e:
        console.print(f"[red]✗[/] failed to save network.json: {e}")
        sys.exit(2)

    if admin_pp is not None:
        console.print(f"[green]✓[/] admin passphrase set")
    if primary_pp is not None:
        console.print(f"[green]✓[/] primary passphrase set")
    if admin_pp == primary_pp and admin_pp is not None:
        console.print(
            "[yellow]⚠[/] admin and primary passphrases are the same; "
            "anyone using it will claim both roles atomically"
        )


@evo_group.command("show-admins")
@click.pass_context
def evo_show_admins(ctx: click.Context) -> None:
    """Show pod-level admins recorded in network.json."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    pod = network.get("pod") or {}
    admins = (pod.get("admins") or {}) if isinstance(pod, dict) else {}
    if not isinstance(admins, dict):
        admins = {}
    by_channel = admins.get("external_ids") or {}
    pod_users = admins.get("pod_users") or []

    if not by_channel and not pod_users:
        console.print("[dim]no pod admins recorded[/]")
        return

    console.print("[bold]Pod admins:[/]")
    if isinstance(by_channel, dict):
        for ch, ids in sorted(by_channel.items()):
            if isinstance(ids, list) and ids:
                for ext_id in ids:
                    console.print(f"  {ch}: {ext_id}")
    if isinstance(pod_users, list) and pod_users:
        console.print(f"  pod_users: {', '.join(sorted(pod_users))}")


@evo_group.command("show-audit")
@click.option("--bot", "bot_filter", default=None,
              help="Show only events for this bot")
@click.option("--channel", "chan_filter", default=None,
              help="Show only events for this channel")
@click.option("--limit", default=20, type=int, show_default=True,
              help="Show only the most recent N events")
@click.pass_context
def evo_show_audit(
    ctx: click.Context,
    bot_filter: str | None,
    chan_filter: str | None,
    limit: int,
) -> None:
    """Show the primary-user identity audit log."""
    from .evo import audit as _audit
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    events = _audit.read_events(shared_dir)
    if bot_filter:
        events = [e for e in events if e.get("bot_id") == bot_filter]
    if chan_filter:
        events = [e for e in events if e.get("channel") == chan_filter.lower()]
    if not events:
        console.print("[dim]no audit events recorded[/]")
        return
    for e in events[-limit:]:
        from_str = e.get("from") or "—"
        action = e.get("action", "?")
        forced = " [yellow](forced)[/]" if e.get("force") else ""
        reason = e.get("reason")
        console.print(
            f"{e.get('ts', '?')}  ({e.get('actor', '?')})  "
            f"{action} [bold]{e.get('bot_id')}[/]/{e.get('channel')}  "
            f"{from_str} → {e.get('to')}{forced}"
        )
        if reason:
            console.print(f"  reason: {reason}")


@evo_group.command("show-guide")
@click.argument("bot_id")
@click.option("--raw", is_flag=True, default=False,
              help="Print the raw markdown file (suitable for piping back "
                   "into `evo set-guide --file -`)")
@click.pass_context
def evo_show_guide(ctx: click.Context, bot_id: str, raw: bool) -> None:
    """Print the bot guide for BOT_ID, or note that no guide is recorded."""
    from .evo import guide as _guide
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    try:
        g = _guide.read_guide(shared_dir, bot_id)
    except _guide.GuideError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    if g is None:
        console.print(f"[dim]no guide recorded for {bot_id}[/]")
        return

    path = _guide.guide_path(shared_dir, bot_id)
    if raw:
        click.echo(path.read_text(encoding="utf-8"), nl=False)
        return

    console.print(f"[bold]{bot_id}[/] guide ([dim]{path}[/]):")
    if g.authored_by:
        console.print(f"  authored by: {g.authored_by}")
    if g.authored_at:
        console.print(f"  authored at: {g.authored_at}")
    if g.last_edited_at:
        console.print(f"  last edited: {g.last_edited_at}")
    audience = g.frontmatter.get("audience")
    tone = g.frontmatter.get("tone")
    if audience:
        console.print(f"  audience:    {audience}")
    if tone:
        console.print(f"  tone:        {tone}")
    do_say = g.frontmatter.get("do_say") or []
    dont_say = g.frontmatter.get("dont_say") or []
    if isinstance(do_say, list) and do_say:
        console.print("  do:")
        for item in do_say:
            console.print(f"    - {item}")
    if isinstance(dont_say, list) and dont_say:
        console.print("  don't:")
        for item in dont_say:
            console.print(f"    - {item}")
    if g.body.strip():
        console.print()
        click.echo(g.body)


@evo_group.command("set-guide")
@click.argument("bot_id")
@click.option("--file", "src_file", required=True,
              type=click.Path(exists=False, dir_okay=False, path_type=Path),
              help="Path to a markdown file with YAML frontmatter; "
                   "use '-' to read from stdin")
@click.option("--authored-by", default=None,
              help="Override the authored_by field (default: keep existing "
                   "or stamp current $SUDO_USER on first write)")
@click.pass_context
def evo_set_guide(
    ctx: click.Context,
    bot_id: str,
    src_file: Path,
    authored_by: str | None,
) -> None:
    """Write or replace the bot guide for BOT_ID from a markdown file.

    The file is expected to have YAML frontmatter (between '---' fences) and
    a markdown body. Required: nothing in v1 — but ``audience``, ``tone``,
    ``do_say``, and ``dont_say`` in the frontmatter are read by the
    secondary-user wizard (slice 4b) so it's worth filling them.

    Example:
        sudo evolve-admin evo set-guide team_bot_a --file ~/team_bot_a-guide.md
    """
    from .evo import guide as _guide

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    if str(src_file) == "-":
        raw = sys.stdin.read()
    else:
        if not src_file.exists():
            console.print(f"[red]✗[/] file not found: {src_file}")
            sys.exit(1)
        raw = src_file.read_text(encoding="utf-8")

    try:
        frontmatter, body = _guide._split_frontmatter(raw)
    except _guide.GuideError as e:
        console.print(f"[red]✗[/] could not parse {src_file}: {e}")
        sys.exit(1)

    # Default authored_by to the human who ran the command, only on first
    # write (write_guide preserves an existing authored_by if we don't
    # override it).
    if authored_by is None and not _guide.guide_exists(shared_dir, bot_id):
        from .evo import audit as _audit
        actor = _audit.cli_actor()
        if actor.startswith("cli:"):
            authored_by = actor[len("cli:"):]

    try:
        stored = _guide.write_guide(
            shared_dir,
            bot_id,
            frontmatter=dict(frontmatter),
            body=body,
            authored_by=authored_by,
        )
    except _guide.GuideError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(1)
    except (PermissionError, OSError) as e:
        console.print(f"[red]✗[/] failed to write guide: {e}")
        sys.exit(2)

    path = _guide.guide_path(shared_dir, bot_id)
    console.print(f"[green]✓[/] wrote {bot_id} guide to {path}")
    if stored.authored_by:
        console.print(f"  authored by: {stored.authored_by}")
    body_lines = stored.body.splitlines()
    console.print(
        f"  frontmatter: {len(stored.frontmatter)} keys, "
        f"body: {len(body_lines)} line{'s' if len(body_lines) != 1 else ''}"
    )


main.add_command(evo_group)


# ── Migration A — populate per-bot overrides from current state ──────────────
#
# Phase 3c of docs/spec-openclaw-json-derived-artifact-2026-05-24.md.

@main.command("openclaw-migrate-overrides")
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Compute the migration but don't write any overrides.",
)
@click.option(
    "--shared-dir", type=click.Path(path_type=Path), default=None,
    help=(
        "Path to the evolve shared dir. Defaults to network.json's "
        "sharedDir or /Users/Shared/evolve."
    ),
)
@click.pass_context
def openclaw_migrate_overrides_cmd(
    ctx: click.Context,
    dry_run: bool,
    shared_dir: Path | None,
) -> None:
    """Populate per-bot overrides from each bot's current openclaw.json.

    Reads every bot's plugins.entries.evolve.config block, identifies
    values that diverge from shipped defaults, and writes them to
    {shared_dir}/sandbox/overrides/<bot>.json with
    set_by="migration:openclaw_derived_2026_05_24" and
    needs_review=True.

    Idempotent — re-running skips keys already represented as overrides.
    Schema-invalid drift (wrong-type values, malformed enums) is logged
    and skipped; those values get reverted on the next deploy when the
    materializer strips them.

    After running, review each bot's needs_review entries in the
    Customizations UI (or via cat) and accept / annotate / revert. Once
    clean, redeploy bots to materialize from the reconciled inputs.

    Safe to run repeatedly. No side effects on openclaw.json — the
    actual stale-key cleanup happens on the next deploy.

    See docs/spec-openclaw-json-derived-artifact-2026-05-24.md §9.
    """
    from .openclaw_migration import (
        format_migration_summary,
        migrate_all_bots,
    )
    from .config import DEFAULT_SHARED_DIR as _DEFAULT_SHARED_DIR, load_network

    # Network resolution: the parent group already loaded network.json
    # into ctx.obj. Fall back to load_network() if that didn't run (e.g.
    # someone invoked the command outside the click context). Surface
    # any load failure as an operator-actionable error rather than a
    # Python traceback.
    network = ctx.obj.get("network") if ctx.obj else None
    if network is None:
        try:
            network = load_network()
        except FileNotFoundError:
            console.print(
                "[red]network.json not found.[/] Run "
                "`evolve-admin setup` first to create the network config."
            )
            sys.exit(1)
        except Exception as e:
            console.print(
                f"[red]Failed to load network.json:[/] "
                f"{type(e).__name__}: {e}"
            )
            sys.exit(1)

    sd = shared_dir
    if sd is None:
        if network and network.get("sharedDir"):
            sd = Path(network["sharedDir"])
        else:
            sd = _DEFAULT_SHARED_DIR

    try:
        result = migrate_all_bots(
            network=network,
            shared_dir=sd,
            dry_run=dry_run,
        )
    except Exception as e:
        # Should never happen — migrate_all_bots catches per-bot
        # exceptions internally. If it does, surface cleanly rather
        # than crashing.
        console.print(
            f"[red]Migration failed:[/] {type(e).__name__}: {e}"
        )
        sys.exit(1)

    console.print(format_migration_summary(result))
    if result.bots_with_errors():
        sys.exit(1)


# ── Phase E.2.b cutover — flip evo from `evolve` to `evo` macOS user ──────────


@main.command("migrate-evo-account-cutover")
@click.option(
    "--confirm", is_flag=True, default=False,
    help=(
        "Actually execute the migration. Without this flag the command "
        "runs in dry-run mode and only prints the plan."
    ),
)
@click.pass_context
def migrate_evo_account_cutover(ctx: click.Context, confirm: bool) -> None:
    """Phase E.2.b: cut evo's gateway over from the privileged `evolve`
    macOS user to the unprivileged `evo` user.

    Spec: docs/spec-evo-account-separation-2026-05-25.md §"Phase E.2.b".

    Prerequisites:
      1. Phase E.2.a has run — the `evo` macOS user exists with an
         empty /Users/evo/.openclaw/ tree. If not, run
         `sudo evolve-admin provision-evo-account` first.
      2. Phase E.3 admin-daemon endpoints are deployed (unix-socket
         auth + cross-bot read/write tools). Without these, evo loses
         its direct-fs reach the moment it switches account.
      3. Run as root (the migration touches /Library/LaunchDaemons/,
         /Users/evolve/, /Users/evo/, and the launchctl bootstrap path).

    Defaults to dry-run. Pass `--confirm` to execute.

    Idempotent: re-running after a successful cutover prints
    "Already cut over" and exits 0. Re-running mid-migration starts
    from the failed step (no state-corruption risk — each step is a
    discrete subprocess.run).

    After the cutover settles, ship Phase E.4 to remove the
    primary-bot exec-deny carve-out from deploy._infer_exec_policy.
    """
    network_path: Path = ctx.obj["network_path"]

    if os.geteuid() != 0:
        console.print(
            "[red]This command must run as root[/] — re-run with "
            "`sudo evolve-admin migrate-evo-account-cutover`."
        )
        sys.exit(2)

    from .setup_wizard import _perform_evo_cutover
    ok = _perform_evo_cutover(network_path, dry_run=not confirm)
    if not ok:
        sys.exit(1)


# ── Telemetry — wipe local tool-gap records (Phase 1.5b §14.3) ────────────────


@main.command("wipe-telemetry")
@click.option(
    "--category", "category", default="tool_gaps",
    type=click.Choice(["tool_gaps"]),
    help=(
        "Which local telemetry to wipe. Today only 'tool_gaps' exists "
        "(spec §14.3). Future categories will appear as choices here."
    ),
)
@click.option(
    "-y", "--yes", "yes", is_flag=True, default=False,
    help="Skip the confirmation prompt.",
)
@click.pass_context
def wipe_telemetry_cmd(ctx: click.Context, category: str, yes: bool) -> None:
    """Wipe local evo telemetry records.

    Per spec §14.3 and ``feedback_user_observation_optout``, every
    observation feature ships with a wipe path. Records live under
    ``{shared_dir}/observations/tool_gaps.jsonl``. This command
    deletes that file; it does NOT touch any uploaded rollups
    (those are governed by the operator's
    ``network.json::evo_telemetry.tool_gaps`` setting).

    Idempotent: running on a pod that's never written telemetry
    succeeds silently (no file to remove).
    """
    network_path: Path = ctx.obj["network_path"]
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]network.json read failed:[/] {exc}")
        sys.exit(2)
    shared_dir = Path(net.get("sharedDir") or DEFAULT_SHARED_DIR)

    # NOTE: when adding a new telemetry category, extend
    # ``click.Choice(...)`` above AND add a dispatch case below — there
    # is no defensive fallback. The Choice enforcement catches typos
    # before this function runs; an unhandled category here would be
    # a programmer error, not a user error.

    # Lazy import — evo.tool_gaps lives under the evolve_admin namespace
    # and pulling it during CLI bootstrap drags in unrelated transitive
    # imports we'd rather defer.
    from .evo import tool_gaps
    assert category == "tool_gaps"  # only choice today; see note above

    target = shared_dir / "observations" / "tool_gaps.jsonl"
    if not target.exists():
        console.print(
            f"[green]No telemetry to wipe[/] — {target} doesn't exist."
        )
        return

    if not yes:
        size = target.stat().st_size
        console.print(
            f"[yellow]About to delete[/] {target} ({size} bytes)."
        )
        if not click.confirm("Proceed?", default=False):
            console.print("[yellow]Cancelled.[/]")
            return

    removed = tool_gaps.wipe_tool_gaps(shared_dir)
    if removed:
        console.print(f"[green]✓[/] Wiped {target}")
    else:
        console.print(
            f"[yellow]No file removed[/] — {target} may already be absent "
            "or the unlink failed."
        )


# ── Intake (bug / feature / question capture + GitHub promotion) ──────────────
# Spec: docs/spec-primary-bot-interface-2026-05-14.md §6.

@main.group("intake")
def intake_group() -> None:
    """Manage the bug-report / feature-request intake queue."""


@intake_group.command("configure")
@click.option("--owner", required=True, help="GitHub owner (org or user) the issues land under")
@click.option("--repo", required=True, help="GitHub repo the issues land under")
@click.option("--name", "target_name", default=None,
              help="Named target — pass when adding a second/third target (e.g. 'openclaw'). "
                   "Omit on first run to write a single-target legacy block; the reader "
                   "tolerates both shapes. Default: 'default' when --owner+--repo introduce "
                   "the first target on this install.")
@click.option("--make-default", "make_default", is_flag=True, default=False,
              help="Mark this target as the configured default. Implicit when configuring "
                   "the first target on a fresh install.")
@click.option("--token-slot", "token_slot", default="github_intake", show_default=True,
              help="Keystore slot name for the GitHub PAT (can differ per target)")
@click.option("--bug-labels", "bug_labels", default="intake,bug", show_default=True,
              help="Comma-separated labels applied to bug issues")
@click.option("--feature-labels", "feature_labels", default="intake,enhancement", show_default=True,
              help="Comma-separated labels applied to feature issues")
@click.option("--question-labels", "question_labels", default="intake,question", show_default=True,
              help="Comma-separated labels applied to question issues")
@click.option("--token", default=None,
              help="Token value to store now; if omitted, you'll be prompted "
                   "(or you can set it later via `evolve-admin keys set <slot>`).")
@click.pass_context
def intake_configure(
    ctx: click.Context,
    owner: str,
    repo: str,
    target_name: str | None,
    make_default: bool,
    token_slot: str,
    bug_labels: str,
    feature_labels: str,
    question_labels: str,
    token: str | None,
) -> None:
    """Write intake.github to network.json and register the keystore slot.

    Configure a single (legacy) target:
        sudo evolve-admin intake configure \\
            --owner evolve-ops --repo evolve

    Add a second named target (e.g. to also file against OpenClaw):
        sudo evolve-admin intake configure \\
            --name openclaw --owner openclaw --repo openclaw \\
            --token-slot github_intake_openclaw

    Make an existing or new target the default for un-suffixed
    promotes:
        sudo evolve-admin intake configure \\
            --name openclaw --owner openclaw --repo openclaw --make-default
    """
    from .keystore import KeystoreManager

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)

    def _split(csv: str) -> list[str]:
        return [s.strip() for s in csv.split(",") if s.strip()]

    target_entry = {
        "owner": owner.strip(),
        "repo": repo.strip(),
        "token_slot": token_slot.strip(),
        "labels": {
            "bug": _split(bug_labels),
            "feature": _split(feature_labels),
            "question": _split(question_labels),
        },
    }

    intake_block = network.setdefault("intake", {})
    github_block = intake_block.get("github")
    if not isinstance(github_block, dict):
        github_block = {}

    # Decide whether we're writing the v1 single-target shape or the
    # v2 multi-target shape. Heuristic: stay on v1 when --name is omitted
    # AND no v2 targets already exist (keeps existing single-target
    # automation unchanged). Switch to v2 the moment a name appears.
    has_v2 = isinstance(github_block.get("targets"), dict)
    name = (target_name or "").strip()
    if not has_v2 and not name:
        # v1 path: write owner/repo at top level.
        intake_block["github"] = target_entry
        if make_default:
            # No-op semantically (single target IS the default), but
            # explicit confirmation in output is friendlier.
            pass
        save_network(network, network_path)
        console.print(f"[green]✓[/] intake.github → {owner}/{repo}")
    else:
        # v2 path: targets dict + default name.
        resolved_name = name or "default"
        targets = github_block.get("targets")
        if not isinstance(targets, dict):
            targets = {}
        # If we're upgrading v1 → v2, fold the existing single-target
        # config into the targets dict under the name "default" before
        # adding the new one — preserves the operator's prior setup.
        if not targets and github_block.get("owner") and github_block.get("repo"):
            targets["default"] = {
                "owner": github_block["owner"],
                "repo": github_block["repo"],
                "token_slot": github_block.get("token_slot", "github_intake"),
                "labels": github_block.get("labels", {}),
            }
        targets[resolved_name] = target_entry

        # Pick default. If operator passed --make-default, honor it.
        # If this is the first target, it's the default.
        # Otherwise, preserve whatever was set before.
        existing_default = github_block.get("default")
        if make_default or len(targets) == 1:
            default_name = resolved_name
        elif isinstance(existing_default, str) and existing_default in targets:
            default_name = existing_default
        else:
            # Fall back to first declared.
            default_name = next(iter(targets))

        intake_block["github"] = {
            "default": default_name,
            "targets": targets,
        }
        save_network(network, network_path)
        console.print(
            f"[green]✓[/] intake.github targets.{resolved_name} → {owner}/{repo}"
            + (" [default]" if default_name == resolved_name else "")
        )

    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    mgr = KeystoreManager(shared_dir)
    existing = mgr.ks.get_key_entry(token_slot)
    if not existing:
        mgr.register(
            token_slot,
            provider="github",
            scope="shared",
            description="GitHub PAT for posting intake issues",
            bots=None,
            value=token,
        )
        if not token:
            console.print(
                f"  Register the token value when you have it: "
                f"`evolve-admin keys set {token_slot}`"
            )
    elif token:
        mgr.set_value(token_slot, token)


@intake_group.command("list-targets")
@click.pass_context
def intake_list_targets(ctx: click.Context) -> None:
    """Print every configured intake target with its repo and token slot.

    Use this to remember which `--to <name>` values are valid when
    promoting, and to verify what's been wired up after `intake configure`.
    """
    from .intake import promote as _promote

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    cfg = _promote.PromotionConfig.from_network(network)
    if cfg is None:
        console.print("[yellow]·[/] no intake targets configured")
        console.print("  Run `evolve-admin intake configure --owner <org> --repo <repo>`.")
        return
    console.print(f"[bold]Intake targets[/] (default = [cyan]{cfg.default_target_name}[/]):")
    for t in cfg.targets:
        marker = "[cyan]●[/]" if t.name == cfg.default_target_name else "[dim]○[/]"
        console.print(
            f"  {marker} [bold]{t.name}[/]  →  {t.owner}/{t.repo}  "
            f"(token slot: [dim]{t.token_slot}[/])"
        )


@intake_group.command("list")
@click.option("--state", default=None,
              type=click.Choice(["open", "triaged", "filed", "closed"]),
              help="Filter by state")
@click.option("--kind", default=None,
              type=click.Choice(["bug", "feature", "question"]),
              help="Filter by kind")
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def intake_list(
    ctx: click.Context,
    state: str | None,
    kind: str | None,
    limit: int,
) -> None:
    """List recent intakes."""
    from .intake import store as _store

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    rows = []
    for ix in _store.iter_intakes(shared_dir, state=state, kind=kind):  # type: ignore[arg-type]
        rows.append(ix)
        if len(rows) >= limit:
            break

    if not rows:
        console.print("(no intakes)")
        return

    from rich.table import Table
    t = Table(title=f"Intakes ({len(rows)} shown)")
    t.add_column("id")
    t.add_column("state")
    t.add_column("kind")
    t.add_column("created")
    t.add_column("body")
    t.add_column("github")
    for ix in rows:
        body_preview = ix.body.replace("\n", " ")
        if len(body_preview) > 60:
            body_preview = body_preview[:57] + "…"
        t.add_row(
            ix.id,
            ix.state,
            ix.kind,
            ix.created_at,
            body_preview,
            ix.promotion.github_issue_url or "—",
        )
    console.print(t)


@intake_group.command("promote")
@click.argument("intake_id")
@click.option("--include-transcript", "include_transcript", is_flag=True, default=False,
              help="Include the recent_turns excerpt in the GitHub issue (off by default)")
@click.option("--to", "target_name", default=None,
              help="Named intake target (see `evolve-admin intake configure --list-targets`). "
                   "Defaults to the configured default target.")
@click.pass_context
def intake_promote(
    ctx: click.Context,
    intake_id: str,
    include_transcript: bool,
    target_name: str | None,
) -> None:
    """File an intake to GitHub as an issue."""
    from .intake import promote as _promote, store as _store
    from .keystore import KeystoreManager

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    located = _store.find_intake(shared_dir, intake_id)
    if located is None:
        console.print(f"[red]✗[/] intake {intake_id} not found")
        sys.exit(2)
    intake, _, _ = located

    cfg = _promote.PromotionConfig.from_network(network)
    if cfg is None:
        console.print("[red]✗[/] intake.github not configured — run `evolve-admin intake configure` first")
        sys.exit(2)
    try:
        target = cfg.resolve(target_name)
    except _promote.PromotionError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(2)
    token = KeystoreManager(shared_dir).get_value(target.token_slot)
    if not token:
        console.print(f"[red]✗[/] no token in keystore slot '{target.token_slot}' — run `evolve-admin keys set {target.token_slot}`")
        sys.exit(2)

    try:
        updated = _promote.promote(
            intake,
            network=network,
            shared_dir=shared_dir,
            token=token,
            include_transcript=include_transcript,
            promoted_by="cli",
            target_name=target.name,
        )
    except _promote.PromotionError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(2)

    console.print(f"[green]✓[/] filed: {updated.promotion.github_issue_url}")


# ── Help index (grounded Q&A corpus for the primary bot) ──────────────────────
# Spec: docs/spec-primary-bot-interface-2026-05-14.md §4.

@main.group("help-index")
def help_index_group() -> None:
    """Build and query the help-doc index used by the primary bot."""


def _resolve_docs_root(network_path: Path, override: Path | None) -> Path:
    """Pick a sensible default docs/ dir to index from.

    Priority:
      1. ``--docs-root`` override (CLI flag)
      2. ``<repo>/docs`` where ``repo`` is the deploy checkout
         (``/Users/Shared/evolve-repo`` on the mini)
      3. The local checkout's ``docs/`` walking up from cwd

    Tested on the mini and in local dev. Fails noisily rather than
    silently producing an empty index.
    """
    if override:
        return override
    deploy_repo_docs = Path(get_profile().deploy_checkout_default) / "docs"
    if deploy_repo_docs.exists():
        return deploy_repo_docs
    # Walk up from this file to find a docs/ sibling
    p = Path(__file__).resolve()
    for _ in range(8):
        candidate = p.parent / "docs"
        if candidate.exists() and candidate.is_dir():
            return candidate
        p = p.parent
    raise click.UsageError(
        "Could not locate a docs/ directory — pass --docs-root explicitly."
    )


@help_index_group.command("build")
@click.option(
    "--docs-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to the docs/ directory to index. Defaults to /Users/Shared/evolve-repo/docs on the mini, or the repo's docs/ in local dev.",
)
@click.pass_context
def help_index_build(ctx: click.Context, docs_root: Path | None) -> None:
    """Rebuild the help index from in-tree markdown.

    Writes to ``{shared_dir}/help_index.json`` (atomic). Safe to run
    repeatedly; idempotent given identical source.
    """
    from .help_index import build as _build

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    resolved = _resolve_docs_root(network_path, docs_root)
    index = _build.build_index(resolved)
    path = _build.write_index(index, shared_dir)

    by_cat: dict[str, int] = {}
    for d in index.docs:
        by_cat[d.category] = by_cat.get(d.category, 0) + 1
    cat_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items()))
    console.print(
        f"[green]✓[/] indexed {len(index.docs)} docs ({cat_summary}) → {path}"
    )


@help_index_group.command("list")
@click.pass_context
def help_index_list(ctx: click.Context) -> None:
    """List every doc currently in the index."""
    from .help_index import build as _build

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    index = _build.load_index(shared_dir)
    if index is None:
        console.print(
            "[yellow]No help index found — run `evolve-admin help-index build`[/]"
        )
        sys.exit(2)

    from rich.table import Table
    t = Table(title=f"Help index ({len(index.docs)} docs)")
    t.add_column("doc_id")
    t.add_column("category")
    t.add_column("title")
    t.add_column("size")
    for d in index.docs:
        t.add_row(d.doc_id, d.category, d.title, str(d.size))
    console.print(t)


@help_index_group.command("search")
@click.argument("query")
@click.option("-k", "--top-k", "top_k", default=3, show_default=True,
              help="Number of hits to return")
@click.pass_context
def help_index_search(ctx: click.Context, query: str, top_k: int) -> None:
    """BM25 search the help index — for sanity-checking what the bot will see."""
    from .help_index import build as _build
    from . import help_search as _search

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    index = _build.load_index(shared_dir)
    if index is None:
        console.print(
            "[yellow]No help index found — run `evolve-admin help-index build`[/]"
        )
        sys.exit(2)

    hits = _search.search(index, query, k=top_k)
    if not hits:
        console.print(f"(no hits for {query!r})")
        return
    for i, h in enumerate(hits, 1):
        console.print(
            f"\n[bold]{i}. {h.title}[/] [dim]({h.doc_id}, score {h.score:.2f})[/]"
        )
        console.print(f"   {h.snippet}")


# ── intent (config-intent system) ───────────────────────────────────────────
#
# Backfill shim for the config-intent system
# (docs/spec-config-intent-system-2026-05-21.md). Phase 2 ships the read +
# write seams; this command lets the operator manually record intents for
# pre-existing deliberate deviations so auth_drift_filler stops proposing
# their revert. Phase 3 (LLM inference) automates the back-fill for legacy
# unannotated state.


@main.group("intent")
def intent_group() -> None:
    """Manage config-intent records (per-bot deliberate-deviation annotations)."""


@intent_group.command("set")
@click.argument("bot_id")
@click.argument("field_path")
@click.argument("value")
@click.option("--reason", required=True,
              help="Operator-readable explanation (why this deviation is deliberate)")
@click.option("--set-by", default="pod_admin (CLI backfill)", show_default=True,
              help="Taxonomy entry from the spec §2.2")
@click.option("--depends-on-plugin", default=None,
              help="Bind this intent to a plugin (intent stale-flags if the plugin is removed)")
@click.option("--actor", default=None,
              help="Actor name for audit_history (defaults to set_by)")
@click.option("--shared-dir", "shared_dir", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help=f"Override shared dir (default {DEFAULT_SHARED_DIR})")
@click.pass_context
def intent_set_cmd(
    ctx: click.Context, bot_id: str, field_path: str, value: str,
    reason: str, set_by: str,
    depends_on_plugin: str | None, actor: str | None,
    shared_dir: Path | None,
) -> None:
    """Record an intent for ``BOT_ID``'s ``FIELD_PATH`` having ``VALUE``.

    VALUE is parsed as JSON first ("full" → "full", "true" → True, "42" → 42,
    "null" → None). Falls back to the raw string if JSON parsing fails so
    common shapes like ``"full"`` without explicit quoting still work.

    The current openclaw.json field is not modified — set-intent records the
    intent metadata only. To change the actual config, use the admin UI's
    Permissions panel or the applier path.

    Example backfilling the 2026-05-24 triage findings:

        evolve-admin intent set team_bot_a tools.exec.security '"full"' \\
            --reason "codex plugin requires exec" \\
            --set-by "plugin_side_effect:codex" \\
            --depends-on-plugin codex
    """
    import json as _json
    from evolve_admin.config_intent import set_intent

    network_path: Path = ctx.obj["network_path"]
    try:
        parsed_value = _json.loads(value)
    except _json.JSONDecodeError:
        parsed_value = value

    depends_on = ({"plugin": depends_on_plugin}
                   if depends_on_plugin else None)

    intent_id = set_intent(
        bot_id, field_path, parsed_value,
        reason=reason, set_by=set_by,
        depends_on=depends_on, actor=actor,
        shared_dir=shared_dir,
        network_path=network_path,
    )
    console.print(
        f"[green]✓[/] Recorded intent [bold]{intent_id}[/] "
        f"for {bot_id}::{field_path} = {parsed_value!r}"
    )
    console.print(f"  reason: {reason}")
    console.print(f"  set_by: {set_by}")
    if depends_on:
        console.print(f"  depends_on: {depends_on}")


@intent_group.command("list")
@click.argument("bot_id")
@click.option("--shared-dir", "shared_dir", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help=f"Override shared dir (default {DEFAULT_SHARED_DIR})")
def intent_list_cmd(bot_id: str, shared_dir: Path | None) -> None:
    """List active intents for BOT_ID."""
    from evolve_admin.config_intent import list_intents

    intents = list_intents(bot_id, shared_dir=shared_dir)
    if not intents:
        console.print(f"[dim](no intents recorded for {bot_id})[/]")
        return
    t = Table(show_header=True, header_style="bold blue", title=f"Intents — {bot_id}")
    t.add_column("ID", style="bold")
    t.add_column("Field")
    t.add_column("Value")
    t.add_column("set_by")
    t.add_column("Reason")
    for entry in intents:
        t.add_row(
            entry.get("id", ""),
            entry.get("field_path", ""),
            repr(entry.get("value")),
            entry.get("set_by", ""),
            (entry.get("reason") or "")[:80],
        )
    console.print(t)


@intent_group.command("revoke")
@click.argument("bot_id")
@click.argument("intent_id")
@click.option("--actor", default="pod_admin (CLI)", show_default=True)
@click.option("--shared-dir", "shared_dir", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help=f"Override shared dir (default {DEFAULT_SHARED_DIR})")
@click.pass_context
def intent_revoke_cmd(
    ctx: click.Context, bot_id: str, intent_id: str,
    actor: str, shared_dir: Path | None,
) -> None:
    """Revoke an intent for BOT_ID by INTENT_ID.

    Revocation only removes the metadata record; the underlying config
    field is unchanged. The next sweep that sees the deviation will
    legitimately emit a revert proposal, which the operator can accept
    normally.
    """
    from evolve_admin.config_intent import revoke_intent

    network_path: Path = ctx.obj["network_path"]
    ok = revoke_intent(
        bot_id, intent_id, actor=actor,
        shared_dir=shared_dir, network_path=network_path,
    )
    if ok:
        console.print(f"[green]✓[/] Revoked intent {intent_id} on {bot_id}")
    else:
        console.print(f"[red]✗[/] Intent {intent_id} not found for {bot_id}")
        sys.exit(2)


# ── export-app — scanned-export pipeline CLI ──────────────────────────────────


@main.command("export-app")
@click.option("--bot", "bot_id", required=True,
              help="Source bot id (must match a bot in network.json)")
@click.option("--manifest", "manifest_id", required=True,
              help="Scanner-assigned manifest id (e.g. i-9c16b1c7)")
@click.option("--slug", "slug",
              help="Gallery slug to publish under (e.g. unified-task-system). "
                   "Required when --publish is set.")
@click.option("--publish", is_flag=True, default=False,
              help="After review, write the draft to gallery/<slug>/<pkg_id>.json")
@click.option("--force", is_flag=True, default=False,
              help="With --publish, overwrite an existing gallery file")
@click.option("--skip-round-trip", is_flag=True, default=False,
              help="Skip Stage 0d (dry-run + structural diff) for cost savings")
@click.option("--no-strip-source-specific", is_flag=True, default=False,
              help="Keep source-bot-specific bits (maintenance, Slack DMs, "
                   "severity) in the derived build_spec. Default: strip them.")
@click.option("--previous-pkg-version", default=None,
              help="For re-exports: the prior pkg_version so the new one "
                   "bumps the minor counter (e.g. 2026.06.02-1.3)")
@click.option("--gallery-dir", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help="Override the gallery directory (defaults to <repo>/gallery)")
@click.option("--out", "out_path", default=None,
              type=click.Path(dir_okay=False, path_type=Path),
              help="Write the draft JSON to this file (defaults to stdout)")
@click.pass_context
def export_app_cmd(
    ctx: click.Context,
    bot_id: str,
    manifest_id: str,
    slug: str | None,
    publish: bool,
    force: bool,
    skip_round_trip: bool,
    no_strip_source_specific: bool,
    previous_pkg_version: str | None,
    gallery_dir: Path | None,
    out_path: Path | None,
) -> None:
    """Export a scanner-discovered manifest into a gallery package.

    Runs Stages 0a-0d of the scanned-export pipeline (spec:
    docs/spec-scanned-export-2026-06-02.md) against the named
    manifest. Prints a summary of the round-trip verdict and writes
    the draft JSON to ``--out`` (or stdout).

    With ``--publish``, also writes to ``gallery/<slug>/<pkg_id>.json``
    after the draft is built. The publish step refuses to overwrite
    an existing file unless ``--force`` is passed.

    Requires ``ANTHROPIC_API_KEY`` in the environment.

    Examples:

      \b
      # Build a draft, print it to stdout for review
      evolve-admin export-app --bot team-bot-a --manifest i-9c16b1c7

      \b
      # Same, but write to a file and publish to the gallery
      evolve-admin export-app --bot team-bot-a --manifest i-9c16b1c7 \\
          --slug unified-task-system --out /tmp/draft.json --publish
    """
    import json as _json
    import os as _os

    from .config import bot_home, load_network
    from .applications.export_engine import build_export_draft

    if publish and not slug:
        console.print("[red]✗[/] --slug is required when --publish is set")
        sys.exit(2)

    if not _os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]✗[/] ANTHROPIC_API_KEY is not set in the environment — "
            "the export pipeline calls Claude for Stages 0b/0c/0d. Export "
            "ANTHROPIC_API_KEY=... and retry."
        )
        sys.exit(2)

    network_path: Path = ctx.obj["network_path"]
    try:
        network = load_network(network_path)
    except Exception as exc:
        console.print(f"[red]✗[/] Could not load network.json at {network_path}: {exc}")
        sys.exit(2)
    if bot_id not in (network.get("bots") or {}):
        console.print(
            f"[red]✗[/] Bot {bot_id!r} is not in network.json. "
            f"Known bots: {sorted((network.get('bots') or {}).keys())}"
        )
        sys.exit(2)

    workspace = bot_home(bot_id, network) / ".openclaw" / "workspace"
    manifest_path = workspace / "manifests" / f"{manifest_id}.json"
    if not manifest_path.is_file():
        console.print(
            f"[red]✗[/] No manifest found at {manifest_path}. "
            f"Scanner-discovered manifests live at "
            f"~{bot_id}/.openclaw/workspace/manifests/i-*.json."
        )
        sys.exit(2)

    try:
        scanned_manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]✗[/] Could not parse manifest at {manifest_path}: {exc}")
        sys.exit(2)

    if (scanned_manifest.get("pkg_id") or "").strip() or \
            (scanned_manifest.get("build_spec") or "").strip():
        console.print(
            f"[red]✗[/] {manifest_id} already carries pkg_id or build_spec — "
            "use the forge improvement flow instead."
        )
        sys.exit(2)

    console.print(
        f"[cyan]→[/] Running export pipeline for {bot_id}/{manifest_id} "
        f"({scanned_manifest.get('display_name', 'unnamed')}). "
        "This calls Claude — may take 30-90s."
    )

    shared_dir = Path(network.get("sharedDir") or DEFAULT_SHARED_DIR)
    try:
        draft = build_export_draft(
            bot_id, scanned_manifest, workspace,
            previous_pkg_version=previous_pkg_version,
            strip_source_specific=not no_strip_source_specific,
            skip_round_trip=skip_round_trip,
            shared_dir=shared_dir,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/] Pipeline rejected the request: {exc}")
        sys.exit(2)
    except Exception as exc:
        console.print(f"[red]✗[/] Pipeline crashed: {exc}")
        sys.exit(3)

    # ── Print the verdict summary ────────────────────────────────────────
    pkg_id = draft.get("pkg_id", "?")
    pkg_version = draft.get("pkg_version", "?")
    export_stage = draft.get("export_stage", "?")
    round_trip = draft.get("round_trip") or {}
    verdict = round_trip.get("verdict", "n/a")
    verdict_colour = {
        "good": "green", "drift": "yellow", "broken": "red", "n/a": "grey50",
    }.get(verdict, "grey50")

    console.print()
    console.print(f"[bold]Stage:[/] {export_stage}")
    console.print(f"[bold]pkg_id:[/] {pkg_id}")
    console.print(f"[bold]pkg_version:[/] {pkg_version}")
    console.print(f"[bold]Round-trip verdict:[/] [{verdict_colour}]{verdict}[/]")

    findings = round_trip.get("structural_findings") or []
    if findings:
        console.print()
        console.print(f"[bold]Structural findings ({len(findings)}):[/]")
        for f in findings:
            console.print(
                f"  [yellow]•[/] [{f.get('kind', '?')}] {f.get('detail', '')}"
            )
            if f.get("hint"):
                console.print(f"    [grey50]hint:[/] {f['hint']}")
    missing = round_trip.get("dry_run_missing") or []
    extra = round_trip.get("dry_run_extra") or []
    if missing:
        console.print(f"[bold]Dry-run missing files:[/] {', '.join(missing)}")
    if extra:
        console.print(f"[bold]Dry-run extra files:[/] {', '.join(extra)}")
    if round_trip.get("dry_run_failed"):
        console.print(
            "[red]Dry-run failed[/] — operator must re-run or hand-edit "
            "before publish."
        )

    # ── Write the draft ──────────────────────────────────────────────────
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_json.dumps(draft, indent=2), encoding="utf-8")
        console.print(f"\n[green]✓[/] Draft written to {out_path}")
    else:
        console.print()
        console.print(_json.dumps(draft, indent=2))

    # ── Publish (if requested) ───────────────────────────────────────────
    if publish:
        if verdict == "broken" and not force:
            console.print(
                "\n[red]✗[/] Round-trip verdict is 'broken' — refusing to "
                "publish without --force. Re-run the pipeline or hand-edit "
                "the build_spec before retrying."
            )
            sys.exit(2)

        _REPO_ROOT = Path(__file__).resolve().parents[3]
        resolved_gallery = gallery_dir or (_REPO_ROOT / "gallery")
        target = resolved_gallery / slug / f"{pkg_id}.json"
        if target.exists() and not force:
            console.print(
                f"\n[red]✗[/] {target} already exists. Either bump "
                "--previous-pkg-version and re-run, or pass --force."
            )
            sys.exit(2)

        # Flip status to active for the published copy.
        published = dict(draft)
        if published.get("status") == "draft":
            published["status"] = "active"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_json.dumps(published, indent=2), encoding="utf-8")
        console.print(f"\n[green]✓[/] Published to {target}")
        console.print(
            "[grey50]Commit this file to the repo (`git add` + `git commit`) "
            "so the next repo-puller cycle picks it up on the mini.[/]"
        )


# ── snapshot-files-pack — files-pack hybrid CLI (F-P.3) ──────────────────────


# Tokens we auto-detect as candidates for placeholder substitution.
# Each pattern produces a single ``placeholders`` suggestion plus the
# substituted content. Conservative — we only flag patterns we're
# confident about; the operator reviews the metadata before
# committing.
_FP3_PLACEHOLDER_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (compiled-pattern-string, placeholder-name, what-to-replace-with)
    (r"/Users/{BOT_USER}/\.openclaw/workspace", "workspace", "{workspace}"),
    (r"/Users/{BOT_USER}/", "bot_user", "/Users/{bot_user}/"),
    (r"com\.{BOT_ID}\.", "bot_id", "com.{bot_id}."),
    # bare `{bot_id}` references in the source already use the
    # placeholder convention; mark them so the metadata stays honest.
)


@main.command("snapshot-files-pack")
@click.option("--bot", "bot_id", required=True,
              help="Source bot id (must match a bot in network.json) — "
                   "the bot whose installed files become the snapshot")
@click.option("--pkg", "pkg_id", required=True,
              help="Gallery package id (e.g. p-9bfa1c84) — used to "
                   "resolve the bot's installed manifest and the output "
                   "gallery directory")
@click.option("--out", "out_path",
              type=click.Path(file_okay=False, path_type=Path),
              help="Output directory; defaults to "
                   "<repo>/gallery/<slug>/files/ derived from the "
                   "package's gallery location")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing files-pack at the output path")
@click.option("--no-auto-detect", is_flag=True, default=False,
              help="Don't try to suggest placeholders from on-disk "
                   "content; operator will edit metadata by hand")
@click.pass_context
def snapshot_files_pack_cmd(
    ctx: click.Context,
    bot_id: str,
    pkg_id: str,
    out_path: Path | None,
    force: bool,
    no_auto_detect: bool,
) -> None:
    """Snapshot a bot's installed app files into a gallery files-pack.

    Reads ``manifest.files[]`` from the bot's installed manifest,
    copies each file from the bot's workspace into the output
    directory, computes per-file SHA-256 + size, auto-detects
    placeholder candidates (unless ``--no-auto-detect`` is set), and
    writes the per-file ``manifest.json`` the install dispatcher
    reads.

    Spec: docs/spec-files-pack-hybrid-2026-06-03.md §8.

    The output is a directory the operator should review + commit to
    the repo via the normal git workflow. The CLI does NOT auto-commit
    (per spec Q4 resolution: local write, operator-commits).

    Examples:

      \b
      # Snapshot atlas's clean task-manager install:
      evolve-admin snapshot-files-pack --bot atlas --pkg p-9bfa1c84

      \b
      # Write to a custom location for hand-editing:
      evolve-admin snapshot-files-pack --bot atlas --pkg p-9bfa1c84 \\
          --out /tmp/task-manager-files

    Required: ACL read on the source bot's workspace. The ``evolve``
    user has this via the ``set_evolve_read_acl(bot_id)`` step that
    runs on deploy.
    """
    import sys as _sys

    from .applications import snapshot_engine
    from .applications.gallery import find_files_pack_dir
    from .config import load_network

    network_path: Path = ctx.obj["network_path"]
    try:
        network = load_network(network_path)
    except Exception as exc:
        console.print(f"[red]✗[/] Could not load network.json: {exc}")
        _sys.exit(2)

    # Resolve output directory. CLI-specific UX: default to the
    # in-repo gallery files-pack dir; the engine accepts any Path.
    if out_path is None:
        fp_dir = find_files_pack_dir(pkg_id)
        if fp_dir is not None:
            out_path = fp_dir
        else:
            # Last resort: derive `<repo>/gallery/<slug>/files/` from
            # the package's known directory.
            from .applications.gallery import _BUILTIN_GALLERY_DIR
            slug_candidates = [
                d for d in _BUILTIN_GALLERY_DIR.iterdir()
                if d.is_dir() and (d / f"{pkg_id}.json").exists()
            ]
            if not slug_candidates:
                console.print(
                    f"[red]✗[/] Couldn't infer --out: no gallery directory "
                    f"contains {pkg_id}.json. Pass --out explicitly."
                )
                _sys.exit(2)
            out_path = slug_candidates[0] / "files"

    # Delegate to the engine. All input validation, file handling,
    # placeholder detection, and manifest emission live there.
    result = snapshot_engine.snapshot_installed_app(
        bot_id=bot_id,
        pkg_id=pkg_id,
        out_dir=out_path,
        auto_detect=not no_auto_detect,
        force=force,
        network=network,
    )

    if not result.get("ok"):
        err = result.get("error", "snapshot_failed")
        console.print(f"[red]✗[/] {err}")
        _sys.exit(2)

    files_count = result.get("files_count", 0)
    console.print(
        f"[cyan]→[/] Snapshotting {files_count} file(s) from "
        f"{bot_id} into {out_path}"
    )
    for skip in result.get("skipped") or []:
        console.print(
            f"   [yellow]⚠[/] {skip.get('path')}: "
            f"{skip.get('reason', 'skipped')}"
        )

    console.print(
        f"\n[green]✓[/] Wrote files-pack to {out_path} "
        f"({files_count} file(s))"
    )

    # Per-file placeholder review surface — extracted from the
    # engine's per_file output. Operators check bare-token counts to
    # spot false positives (e.g. bot_id matches a real-language word).
    notes: list[tuple[str, list[str], int]] = []
    for entry in result.get("per_file") or []:
        placeholders = entry.get("placeholders") or []
        bare_n = entry.get("bare_token_count", 0) or 0
        if placeholders:
            notes.append((entry["path"], placeholders, bare_n))
    if notes:
        console.print(
            "\n[bold]Auto-detected placeholders:[/] (review before commit)"
        )
        for rel, names, bare_n in sorted(notes):
            if bare_n > 0:
                console.print(
                    f"  [yellow]•[/] {rel}: {', '.join(names)} "
                    f"[dim]({bare_n} bare bot_id token"
                    f"{'s' if bare_n != 1 else ''} substituted)[/]"
                )
            else:
                console.print(f"  [yellow]•[/] {rel}: {', '.join(names)}")
    console.print(
        "\n[grey50]Next steps:\n"
        "  1. git diff to review the files-pack content\n"
        "  2. Edit files/manifest.json if any placeholder list is wrong\n"
        "  3. Update the package's pkg_id.json to include "
        "files_pack metadata\n"
        "  4. Commit + push, then the next install on a fresh bot "
        "runs the cheap path.[/]"
    )


# ── promote-app — F-P.10 unified promote-to-gallery CLI ──────────────────────
#
# Distinct from ``export-app`` (which runs the scanned-export pipeline
# Stages 0a-0d for ADDING NEW apps to the gallery from scanner-
# discovered manifests). ``promote-app`` handles the orthogonal
# workflow: take an EXISTING gallery package that lives as
# manifest-only and promote it to manifest+files class by snapshotting
# the source bot's installed files. Two complementary CLIs, two
# different jobs.


@main.command("promote-app")
@click.option("--bot", "bot_id", required=True,
              help="Source bot id (must match a bot in network.json) — "
                   "the bot whose installed app gets promoted to a "
                   "gallery files-pack")
@click.option("--pkg", "pkg_id", required=True,
              help="Gallery package id (e.g. p-9bfa1c84). Must already "
                   "exist in the gallery as manifest-only; this command "
                   "promotes it to manifest+files class.")
@click.option("--bump", "bump_kind",
              type=click.Choice(["patch", "minor", "major"], case_sensitive=False),
              default="minor",
              help="How to bump pkg_version. Default: minor (e.g. "
                   "2026.06.03-1.3 → 2026.06.03-1.5).")
@click.option("--pkg-version", "explicit_version", default=None,
              help="Override pkg_version directly (skips --bump).")
@click.option("--bundle-only", "bundle_only_patterns", multiple=True,
              help="Glob-style pattern (e.g. 'scripts/*.py') identifying "
                   "files to include in the files-pack as bundled. "
                   "Files not matching ANY --bundle-only pattern are "
                   "marked provenance=forge in the package manifest and "
                   "EXCLUDED from the files-pack content. Stamps "
                   "partial=true on the files-pack metadata. Repeatable. "
                   "When omitted, every file in the install becomes "
                   "bundled (today's behavior).")
@click.option("--partial", is_flag=True, default=False,
              help="Stamp partial=true on the files-pack metadata even "
                   "when --bundle-only isn't used. Useful when operator "
                   "knows the package manifest declares files not yet "
                   "covered (planned for a future snapshot).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Compute everything but don't write to disk.")
@click.pass_context
def promote_app_cmd(
    ctx: click.Context,
    bot_id: str,
    pkg_id: str,
    bump_kind: str,
    explicit_version: str | None,
    bundle_only_patterns: tuple[str, ...],
    partial: bool,
    dry_run: bool,
) -> None:
    """One-shot promote an installed app to a gallery files-pack.

    Wraps the F-P.6 runbook's nine manual steps into one command:
    snapshot the bot's installed files → stamp the gallery package
    manifest with a ``files_pack`` block → bump ``pkg_version`` →
    bump ``gallery/index.json``. Operator then commits via the
    normal git workflow.

    Spec: docs/spec-files-pack-hybrid-2026-06-03.md §8.
    Runbook (manual equivalent): docs/runbook-snapshot-install-to-
    gallery-2026-06-03.md.

    Examples:

      \b
      # Snapshot atlas's task-manager and stamp the gallery in one step:
      evolve-admin promote-app --bot atlas --pkg p-9bfa1c84

      \b
      # Custom version bump:
      evolve-admin promote-app --bot atlas --pkg p-9bfa1c84 \\
          --pkg-version 2026.07.01-2.0

      \b
      # See what would change without writing anything:
      evolve-admin promote-app --bot atlas --pkg p-9bfa1c84 --dry-run

    Required: ACL read on the source bot's workspace (the ``evolve``
    user has this via ``set_evolve_read_acl(bot_id)`` at deploy time)
    AND write access to the operator's gallery dev clone (this CLI
    writes into the local checkout's ``gallery/<slug>/`` —
    not the deploy box's read-only repo).
    """
    import json as _json
    import sys as _sys

    from .applications import snapshot_engine
    from .applications.files_pack import (
        FILES_PACK_FORMAT_VERSION,
        compute_files_pack_sha256,
    )
    from .applications.gallery import _BUILTIN_GALLERY_DIR
    from .config import load_network

    network_path: Path = ctx.obj["network_path"]
    try:
        network = load_network(network_path)
    except Exception as exc:
        console.print(f"[red]✗[/] Could not load network.json: {exc}")
        _sys.exit(2)

    # Resolve gallery dir for the slug carrying pkg_id.
    slug_candidates = [
        d for d in _BUILTIN_GALLERY_DIR.iterdir()
        if d.is_dir() and (d / f"{pkg_id}.json").exists()
    ]
    if not slug_candidates:
        console.print(
            f"[red]✗[/] Couldn't resolve gallery dir: no slug under "
            f"{_BUILTIN_GALLERY_DIR} contains {pkg_id}.json. Make sure "
            f"the package exists in the gallery first (scanned-export or "
            f"manual)."
        )
        _sys.exit(2)
    if len(slug_candidates) > 1:
        console.print(
            f"[red]✗[/] Multiple gallery dirs contain {pkg_id}.json: "
            f"{[str(c) for c in slug_candidates]}. Resolve by renaming "
            f"or removing the stale copy."
        )
        _sys.exit(2)
    gallery_slug_dir = slug_candidates[0]
    package_manifest_path = gallery_slug_dir / f"{pkg_id}.json"
    files_pack_dir = gallery_slug_dir / "files"

    # Read the existing package manifest to compute the new pkg_version.
    try:
        package_manifest = _json.loads(package_manifest_path.read_text())
    except Exception as exc:
        console.print(f"[red]✗[/] Couldn't read {package_manifest_path}: {exc}")
        _sys.exit(2)
    current_version = package_manifest.get("pkg_version") or ""
    new_version = (
        explicit_version
        or _bump_pkg_version(current_version, bump_kind)
    )

    console.print(
        f"[cyan]→[/] Promote {pkg_id} from {bot_id}: "
        f"{current_version} → {new_version}"
    )
    if files_pack_dir.exists() and any(files_pack_dir.iterdir()):
        console.print(
            f"   [yellow]⚠[/] {files_pack_dir} already has files — "
            f"will be overwritten with the new snapshot"
        )

    if dry_run:
        console.print("[yellow]⚠[/] --dry-run: no writes performed.")
        return

    # Step 1: snapshot to gallery/<slug>/files/ via the engine.
    result = snapshot_engine.snapshot_installed_app(
        bot_id=bot_id,
        pkg_id=pkg_id,
        out_dir=files_pack_dir,
        auto_detect=True,
        # scrub_personalization (F-P.8) will land here once #2054 merges;
        # for now the engine doesn't accept the kwarg yet on this branch.
        force=True,  # we're writing to the canonical gallery dir
        network=network,
    )
    if not result.get("ok"):
        console.print(f"[red]✗[/] Snapshot failed: {result.get('error')}")
        _sys.exit(2)

    files_count = result.get("files_count", 0)
    snapshot_at = result.get("snapshot_at", "")
    snapshot_pkg_version = result.get("snapshot_source_pkg_version", "")
    sha256 = result.get("sha256", "")
    console.print(
        f"[green]✓[/] Snapshotted {files_count} file(s) "
        f"to {files_pack_dir}"
    )

    # F-P.10.x — apply --bundle-only patterns. When set, trim the
    # files-pack to just the matching paths, and stamp provenance on
    # the package manifest's files[]. Files-pack metadata gets
    # partial=true so F-P.4.x's integrity sweep treats the manifest's
    # forge-marked paths as intentional, not missing.
    snapshot_all_paths = [e.get("path", "") for e in (result.get("per_file") or [])]
    snapshot_all_paths = [p for p in snapshot_all_paths if p]
    if bundle_only_patterns:
        import fnmatch as _fnmatch
        bundled_paths_set: set[str] = set()
        for p in snapshot_all_paths:
            if any(_fnmatch.fnmatch(p, pat) for pat in bundle_only_patterns):
                bundled_paths_set.add(p)
        forge_paths_set = set(snapshot_all_paths) - bundled_paths_set
        # Trim the files-pack on disk to bundled-only.
        files_count, sha256 = _trim_files_pack_to_subset(
            files_pack_dir, bundled_paths_set,
            mark_partial=True,
        )
        console.print(
            f"[green]✓[/] Trimmed files-pack to {files_count} bundled "
            f"file(s); {len(forge_paths_set)} marked provenance=forge"
        )
    else:
        bundled_paths_set = set(snapshot_all_paths)
        forge_paths_set = set()
        if partial:
            # Operator explicitly stamps partial without filtering.
            files_count, sha256 = _trim_files_pack_to_subset(
                files_pack_dir, bundled_paths_set, mark_partial=True,
            )

    # Step 2: stamp the gallery package manifest with the files_pack
    # block + bump pkg_version + per-file provenance annotations.
    # snapshot_source_bot_id is intentionally OMITTED (see F-P.5 /
    # #2052 — reserved-token bot ids would leak into the public
    # gallery manifest).
    package_manifest["pkg_version"] = new_version
    package_manifest["files_pack"] = {
        "format_version": FILES_PACK_FORMAT_VERSION,
        "files_count": files_count,
        "snapshot_source_pkg_version": snapshot_pkg_version,
        "snapshot_at": snapshot_at,
        "sha256": sha256,
    }
    if bundle_only_patterns or partial:
        # Document why partial mode is in effect on the package side.
        package_manifest["files_pack"]["partial"] = True
    # Smart-forge model (#2058): write per-file provenance into the
    # package manifest so the install dispatcher and F-P.4.x sweep
    # both see the operator's intent. List shape:
    #   [{path, provenance: "bundled" | "forge"}]
    # Existing files[] entries are preserved if present, with the
    # provenance field overwritten; new entries are appended.
    existing_files = list(package_manifest.get("files") or [])
    existing_by_path = {
        (e.get("path") if isinstance(e, dict) else e): e
        for e in existing_files
        if e
    }
    merged_files: list[dict] = []
    for path in snapshot_all_paths:
        prov = "bundled" if path in bundled_paths_set else "forge"
        prior = existing_by_path.get(path)
        if isinstance(prior, dict):
            entry = dict(prior)
            entry["path"] = path
            entry["provenance"] = prov
        else:
            entry = {"path": path, "provenance": prov}
        merged_files.append(entry)
    # Preserve any pre-existing entries that weren't in this snapshot
    # (operator might have hand-added a forge file).
    snapshot_set = set(snapshot_all_paths)
    for prior_path, prior_entry in existing_by_path.items():
        if prior_path and prior_path not in snapshot_set:
            if isinstance(prior_entry, dict):
                merged_files.append(prior_entry)
    package_manifest["files"] = merged_files
    package_manifest_path.write_text(
        _json.dumps(package_manifest, indent=2) + "\n",
    )
    console.print(
        f"[green]✓[/] Stamped {package_manifest_path.name}: "
        f"files_pack block + pkg_version → {new_version}"
    )

    # Step 3: bump gallery/index.json for this slug.
    index_path = _BUILTIN_GALLERY_DIR / "index.json"
    try:
        index_entries = _json.loads(index_path.read_text())
    except Exception as exc:
        console.print(
            f"[yellow]⚠[/] Couldn't read {index_path}: {exc}. "
            f"Skipping index bump — operator must update by hand."
        )
    else:
        bumped = False
        for entry in index_entries:
            if isinstance(entry, dict) and entry.get("pkg_id") == pkg_id:
                entry["pkg_version"] = new_version
                bumped = True
                break
        if bumped:
            index_path.write_text(
                _json.dumps(index_entries, indent=2) + "\n",
            )
            console.print(
                f"[green]✓[/] Bumped gallery/index.json entry to {new_version}"
            )
        else:
            console.print(
                f"[yellow]⚠[/] No gallery/index.json entry for {pkg_id}; "
                f"operator may need to add one."
            )

    # Personalization summary (F-P.8) — pod-wide totals. The engine
    # only emits this field once F-P.8 lands; until then ``totals`` is
    # an empty dict and this block is skipped silently.
    totals = result.get("personalization_totals") or {}
    if totals:
        console.print(
            "\n[bold]Personalization scrub:[/] (verify before commit)"
        )
        for ph in sorted(totals):
            console.print(
                f"  [magenta]•[/] {{{ph}}}: {totals[ph]} substitution"
                f"{'s' if totals[ph] != 1 else ''}"
            )

    console.print(
        "\n[grey50]Next steps:\n"
        "  1. git diff to review the staged changes\n"
        "  2. git add gallery/ && git commit\n"
        "  3. Open a PR; CI runs the integrity sweep + scrub guard "
        "automatically.[/]"
    )


def _trim_files_pack_to_subset(
    files_pack_dir: Path,
    bundled_paths: set[str],
    *,
    mark_partial: bool = False,
) -> tuple[int, str]:
    """Trim a snapshotted files-pack to just the bundled subset.

    F-P.10.x — when ``promote-app --bundle-only PATTERN`` is used, the
    snapshot engine writes everything from the install but only the
    paths matching the operator's patterns belong in the files-pack
    bundle. This helper:

      1. Deletes non-bundled files from the gallery files-pack dir.
      2. Rewrites ``files/manifest.json`` to keep only bundled entries.
      3. Stamps ``partial: true`` on the metadata when ``mark_partial``
         (so F-P.4.x's integrity sweep treats forge-marked paths in
         the package manifest as intentional, not missing).
      4. Recomputes the top-level files-pack SHA-256.

    Returns ``(files_count, top_level_sha256)`` so the caller can
    stamp them on the package manifest's ``files_pack`` block without
    re-walking the directory.
    """
    import json as __json
    from .applications.files_pack import compute_files_pack_sha256

    manifest_path = files_pack_dir / "manifest.json"
    metadata = __json.loads(manifest_path.read_text(encoding="utf-8"))
    kept_entries = []
    for entry in metadata.get("files") or []:
        path = (entry.get("path") if isinstance(entry, dict) else "") or ""
        if path in bundled_paths:
            kept_entries.append(entry)
        else:
            # Drop the file from the files-pack dir (best-effort).
            target = files_pack_dir / path
            try:
                if target.is_file():
                    target.unlink()
            except OSError:
                pass
    metadata["files"] = kept_entries
    if mark_partial:
        metadata["partial"] = True
    manifest_path.write_text(__json.dumps(metadata, indent=2) + "\n")

    # Recompute top-level SHA — it's the hash of the per-file SHAs in
    # the (now trimmed) manifest, so dropping entries changes it.
    new_sha = compute_files_pack_sha256(files_pack_dir)
    return len(kept_entries), new_sha


def _bump_pkg_version(current: str, bump_kind: str) -> str:
    """Derive the next pkg_version from the current one.

    Pkg-version format used by the gallery: ``YYYY.MM.DD-MAJOR.MINOR``
    (e.g. ``2026.06.03-1.3``). Bump kinds:

      patch  ->  2026.06.03-1.3 -> 2026.06.03-1.4
      minor  ->  2026.06.03-1.3 -> 2026.06.03-1.5  (most common — keeps
                 odd-numbered minor as the "stamped a files-pack" marker)
      major  ->  2026.06.03-1.3 -> 2026.06.03-2.0

    Falls back to a sensible default (today + 1.0) when the current
    version doesn't parse.
    """
    from datetime import datetime as _datetime, timezone as _timezone

    today = _datetime.now(_timezone.utc).strftime("%Y.%m.%d")
    if not current or "-" not in current:
        return f"{today}-1.0"
    date_part, rest = current.split("-", 1)
    if "." not in rest:
        return f"{date_part}-1.0"
    try:
        major_s, minor_s = rest.split(".", 1)
        major = int(major_s)
        minor = int(minor_s.split(".")[0])  # drop trailing patches if any
    except (ValueError, IndexError):
        return f"{today}-1.0"

    bump = bump_kind.lower()
    if bump == "major":
        major += 1
        minor = 0
    elif bump == "patch":
        minor += 1
    else:  # minor
        # Bump by 2 to skip a slot, matching the F-P.5 convention of
        # going 1.3 → 1.5 to signal "files-pack stamped" at a glance
        # in git history.
        minor += 2
    return f"{date_part}-{major}.{minor}"


# ── F-P.13.b — files-pack signing CLI ───────────────────────────────────────


@main.command("gen-signing-key")
@click.option("--out-dir", "out_dir", required=True,
              type=click.Path(file_okay=False, path_type=Path),
              help="Directory to write the keypair (creates it if absent)")
@click.option("--name", default="files-pack-signing",
              help="Filename stem; produces <name>.pem (private) + "
                   "<name>.pub.pem (public) inside --out-dir")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing keys at the target paths")
def gen_signing_key_cmd(
    out_dir: Path, name: str, force: bool,
) -> None:
    """Generate a fresh Ed25519 keypair for files-pack signing.

    Writes two PEM files under ``--out-dir``:

      <name>.pem      — the PRIVATE key. Treat as a secret.
                        Mode 0600.
      <name>.pub.pem  — the PUBLIC key. Embed this in
                        ``contributor.public_key`` on packages you
                        publish so verifiers can confirm your
                        signatures.

    F-P.13 only supports Ed25519. Existing keys are NOT overwritten
    unless ``--force`` is passed.

    Example:

      \b
      evolve-admin gen-signing-key --out-dir ~/.evolve/keys

      Generates:
        ~/.evolve/keys/files-pack-signing.pem
        ~/.evolve/keys/files-pack-signing.pub.pem
    """
    import os as _os
    import sys as _sys

    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _Ed25519PrivateKey,
    )

    from .applications.files_pack_signing import compute_key_id

    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / f"{name}.pem"
    pub_path = out_dir / f"{name}.pub.pem"
    if (priv_path.exists() or pub_path.exists()) and not force:
        console.print(
            f"[red]✗[/] One or both target paths already exist:\n"
            f"  {priv_path}\n  {pub_path}\n"
            f"Pass --force to overwrite, or pick a different --name."
        )
        _sys.exit(2)

    priv = _Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PrivateFormat.PKCS8,
        encryption_algorithm=_ser.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PublicFormat.SubjectPublicKeyInfo,
    )

    priv_path.write_bytes(priv_pem)
    _os.chmod(priv_path, 0o600)
    pub_path.write_bytes(pub_pem)

    key_id = compute_key_id(priv.public_key())
    console.print(f"[green]✓[/] Wrote private key to {priv_path} (0600)")
    console.print(f"[green]✓[/] Wrote public key to {pub_path}")
    console.print(f"[cyan]→[/] Key fingerprint: {key_id}")
    console.print(
        "\n[grey50]Next steps:\n"
        "  1. KEEP the private key secret. Anyone with it can sign\n"
        "     files-packs in your name.\n"
        "  2. Embed the public key bytes in package manifests'\n"
        "     contributor.public_key field so verifiers can confirm\n"
        "     your signatures (F-P.13.c / F-P.13.d).\n"
        "  3. Sign a files-pack via "
        "`evolve-admin sign-files-pack --pack-dir ... --key-file " +
        str(priv_path) + "`[/]"
    )


@main.command("sign-files-pack")
@click.option("--pack-dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Files-pack directory containing manifest.json")
@click.option("--key-file", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Path to the PEM-encoded Ed25519 private key")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing signature on the manifest")
def sign_files_pack_cmd(
    pack_dir: Path, key_file: Path, force: bool,
) -> None:
    """Sign a files-pack's manifest with an Ed25519 private key.

    Reads ``pack-dir/manifest.json``, signs the canonical bytes of
    the metadata (everything except the existing signature field),
    writes the signature block back into the manifest, and persists.

    Refuses to overwrite an existing signature unless ``--force`` is
    passed — sign-then-sign-again is usually a bug (and the second
    signature replaces the first; there's no chain).

    Example:

      \b
      evolve-admin sign-files-pack \\
          --pack-dir gallery/my-app/files \\
          --key-file ~/.evolve/keys/files-pack-signing.pem
    """
    import json as _json
    import sys as _sys

    from .applications.files_pack import load_files_pack_metadata
    from .applications.files_pack_signing import (
        FilesPackSignatureError,
        load_private_key_pem,
        sign_files_pack,
    )

    try:
        metadata = load_files_pack_metadata(pack_dir)
    except Exception as exc:
        console.print(f"[red]✗[/] Could not load metadata at {pack_dir}: {exc}")
        _sys.exit(2)
    if metadata is None:
        console.print(
            f"[red]✗[/] No manifest.json found at "
            f"{pack_dir / 'manifest.json'}. Is this a files-pack directory?"
        )
        _sys.exit(2)
    if metadata.signature and not force:
        console.print(
            f"[red]✗[/] {pack_dir}/manifest.json already has a signature "
            f"(signer_key_id={metadata.signature.get('signer_key_id')!r}). "
            f"Pass --force to replace it."
        )
        _sys.exit(2)

    try:
        priv = load_private_key_pem(key_file.read_bytes())
    except FilesPackSignatureError as exc:
        console.print(f"[red]✗[/] Could not load private key: {exc}")
        _sys.exit(2)

    try:
        signature = sign_files_pack(metadata, priv)
    except FilesPackSignatureError as exc:
        console.print(f"[red]✗[/] Signing failed: {exc}")
        _sys.exit(2)

    # Read the raw manifest as a dict so we preserve any fields the
    # FilesPackMetadata dataclass doesn't model explicitly (forward
    # compat). Then write back with the signature block.
    manifest_path = pack_dir / "manifest.json"
    raw = _json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["signature"] = signature
    manifest_path.write_text(_json.dumps(raw, indent=2) + "\n")

    console.print(
        f"[green]✓[/] Signed {pack_dir.name}/manifest.json"
    )
    console.print(f"  signer_key_id: {signature['signer_key_id']}")
    console.print(f"  signed_at:     {signature['signed_at']}")
    console.print(
        "\n[grey50]Verifiers will check this signature against the\n"
        "  contributor.public_key field on the package manifest. Make\n"
        "  sure that field is populated with the matching public key\n"
        "  before publishing.[/]"
    )


if __name__ == "__main__":
    main()
