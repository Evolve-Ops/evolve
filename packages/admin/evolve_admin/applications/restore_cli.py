"""``application restore-manifest`` CLI command.

Lives outside cli.py (which is no-growth capped) and is attached to the
``application`` group via a one-line ``add_command`` there. Restores an
orphaned/set-aside app manifest so it reappears on the grid; see
manifest_recovery.py and internal/incident-atlas-app-conflation-2026-06-22.md.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from ..config import DEFAULT_SHARED_DIR, load_network
from .manifest import applications_dir
from .manifest_recovery import restore_orphaned_manifest

_console = Console()


@click.command("restore-manifest")
@click.argument("bot_id")
@click.option("--from", "from_name", required=True,
              help="Set-aside manifest filename to restore, e.g. "
                   "atlas-daily-digest.json.bak-2026-06-16")
@click.option("--unmerge-from", default=None,
              help="Active sibling stem that absorbed this app's files (e.g. "
                   "atlas-article-capture). Strips ONLY files uniquely the "
                   "restored app's — shared library/data substrate is kept.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an active manifest of a DIFFERENT id at the "
                   "target name (otherwise refused).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Report planned actions without writing.")
@click.pass_context
def restore_manifest_cmd(ctx: click.Context, bot_id: str, from_name: str,
                         unmerge_from: str | None, force: bool, dry_run: bool) -> None:
    """Restore an orphaned/set-aside app manifest so it reappears on the grid.

    The grid loader only loads *.json, so a manifest set aside as
    <name>.json.bak-YYYY-MM-DD silently leaves the grid. This validates and
    restores it (refusing to clobber a different active app), and can
    conservatively de-conflate a sibling that absorbed its files.
    """
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    caps_dir = applications_dir(shared_dir, bot_id)
    try:
        report = restore_orphaned_manifest(
            caps_dir, from_name, unmerge_from=unmerge_from, force=force, dry_run=dry_run,
        )
    except ValueError as e:
        _console.print(f"[red]restore-manifest: {e}[/]")
        raise SystemExit(1)

    tag = "[yellow](dry-run)[/] " if dry_run else ""
    _console.print(
        f"{tag}[green]restore-manifest[/] {report['from']} → "
        f"{report['restored']} (id={report['id']})"
    )
    for action in report["actions"]:
        _console.print(f"  [dim]· {action}[/]")
    if not dry_run and report["removed_files"]:
        _console.print(
            f"  [green]de-conflated[/] {report['unmerge_from']}: "
            f"removed {len(report['removed_files'])} file(s)"
        )
    for warning in report.get("warnings", []):
        _console.print(f"  [yellow]![/] {warning}")


def register_cli(application_group) -> None:
    """Attach ``restore-manifest`` to the ``application`` click group. Called
    from cli.py via a one-line registration (keeps cli.py under its size cap)."""
    application_group.add_command(restore_manifest_cmd)
