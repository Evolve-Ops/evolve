"""``application repair-app-crons`` CLI command.

Heals installed launchd app-cron plists that lack a PATH able to find
openclaw/node — the exit-127 silent-non-delivery bug (atlas daily-digest +
ledger morning-briefing/note-taker, 2026-06-22). Body lives here (not cli.py,
which is no-growth capped); attached to the ``application`` group via a one-line
``register_cli`` call there.
"""

from __future__ import annotations

import click
from rich.console import Console

from ..config import load_network
from .install_helpers import repair_app_cron_env_paths

_console = Console()


@click.command("repair-app-crons")
@click.option("--bot", "bot", default="",
              help="Single bot id to repair; default: all members.")
@click.option("--check", is_flag=True, default=False,
              help="Report which app crons lack a PATH without re-installing.")
@click.pass_context
def repair_app_crons_cmd(ctx: click.Context, bot: str, check: bool) -> None:
    """Heal app-cron launchd plists missing a PATH (openclaw exit-127 fix).

    launchd hands a job a minimal PATH that excludes /opt/homebrew/bin, so an app
    cron that shells out to ``openclaw`` dies with exit 127 and silently delivers
    nothing. This re-installs each launchd scheduled-action whose installed plist
    has no PATH, via the (now-fixed) command installer.
    """
    network_path = ctx.obj["network_path"]
    network = load_network(network_path)
    members = [bot] if bot else network.get("members", [])
    if not members:
        _console.print("[yellow]no members to check[/]")
        return

    rep = repair_app_cron_env_paths(members, network=network, check_only=check)
    _console.print(f"checked {rep['checked']} installed app-cron plist(s)")
    for label in rep["missing"]:
        _console.print(f"  [yellow]missing PATH[/] {label}")
    for label in rep["healed"]:
        _console.print(f"  [green]healed[/] {label}")
    for failure in rep["failed"]:
        _console.print(f"  [red]failed[/] {failure['label']}: {failure['error']}")
    if check and rep["missing"]:
        _console.print("[dim]re-run without --check to re-install these.[/]")
    elif not rep["missing"] and not rep["healed"]:
        _console.print("[green]all app crons already have a PATH.[/]")


def register_cli(application_group) -> None:
    """Attach ``repair-app-crons`` to the ``application`` click group."""
    application_group.add_command(repair_app_crons_cmd)
