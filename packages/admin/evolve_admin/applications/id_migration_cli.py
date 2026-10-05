"""``application migrate-ids`` CLI command (AL-1.4a).

Census + one-shot backfill of the canonical ``app_id`` across every
identity-bearing artifact on the pod, and the writer of
``{shared_dir}/apps/id-migration.json``. Logic lives in ``id_migration.py``;
the body lives here rather than in cli.py, which is no-growth capped, and is
attached to the ``application`` group via a one-line ``register_cli`` call
there.

Idempotent by construction: ``--apply`` twice stamps nothing the second time.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). The single mention is this CLI's docstring
# naming the legacy chain it exists to report on. Describing the chain IS this
# surface's job — ``migrate-ids`` is what proves the chain can eventually be
# dropped in 1.4c.

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from ..config import DEFAULT_SHARED_DIR, load_network
from .id_migration import (
    STATUS_ALREADY,
    STATUS_DRAFT,
    STATUS_NO_ID,
    STATUS_NON_CONFORMING,
    STATUS_STAMPED,
    build_report,
)

_console = Console()

_STATUS_STYLE = {
    STATUS_STAMPED: "green",
    STATUS_ALREADY: "dim",
    STATUS_DRAFT: "cyan",
    STATUS_NON_CONFORMING: "yellow",
    STATUS_NO_ID: "red",
}


@click.command("migrate-ids")
@click.option("--bot", "bot", default="",
              help="Single bot id to census; default: all pod members.")
@click.option("--dry-run/--apply", "dry_run", default=True,
              help="Report only (default), or stamp app_id and write the "
                   "migration table.")
@click.option("--verbose", is_flag=True, default=False,
              help="List every artifact, not just the ones needing attention.")
@click.pass_context
def migrate_ids_cmd(
    ctx: click.Context, bot: str, dry_run: bool, verbose: bool,
) -> None:
    """Census (and optionally stamp) the canonical app_id on every manifest.

    AL-1.4a. app_id is the one identity going forward; the stamp writes the id
    the legacy chain (pkg_id -> id -> spec_id -> instance_id) already resolved
    to, so nothing changes meaning. Rows reported as non_conforming or no_id
    are what still needs the legacy fallback — AL-1.4c cannot remove it while
    any remain.
    """
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    bot_ids = [bot] if bot else list(network.get("members", []))
    if not bot_ids:
        _console.print("[yellow]no pod members to census[/]")

    report = build_report(shared_dir, bot_ids, apply=not dry_run)

    tag = "[yellow](dry-run)[/] " if dry_run else ""
    counts = report.counts
    _console.print(
        f"{tag}[green]migrate-ids[/] {len(report.entries)} artifact(s) across "
        f"{len(bot_ids)} bot(s) + gallery"
    )
    for status in (STATUS_STAMPED, STATUS_ALREADY, STATUS_DRAFT,
                   STATUS_NON_CONFORMING, STATUS_NO_ID):
        if counts.get(status):
            label = "would stamp" if (dry_run and status == STATUS_STAMPED) else status
            _console.print(
                f"  [{_STATUS_STYLE[status]}]{label}[/]: {counts[status]}"
            )

    shown = report.entries if verbose else report.blocking
    for entry in shown:
        _console.print(
            f"  [{_STATUS_STYLE.get(entry.status, 'white')}]{entry.status}[/] "
            f"{entry.kind} {entry.legacy_id or entry.draft_id or '(no id)'} "
            f"[dim]({entry.bot_id or 'gallery'})[/]"
        )
        if entry.status in (STATUS_NON_CONFORMING, STATUS_NO_ID):
            # Only the rows an operator has to go and fix get the full path —
            # printing it for every row wraps the report into unreadable soup.
            _console.print(f"      [dim]{entry.path}[/]", soft_wrap=True)

    if report.blocking:
        _console.print(
            f"  [yellow]![/] {len(report.blocking)} artifact(s) carry no "
            "conforming app_id — the legacy resolution fallback is still "
            "load-bearing for them (AL-1.4c blocker)."
        )
    for err in report.errors:
        _console.print(f"  [red]error[/] {err}")

    if dry_run:
        _console.print(
            f"  [dim]table would be written to {report.table_path}; "
            "re-run with --apply[/]"
        )
    else:
        _console.print(
            f"  [green]wrote[/] {report.table_path} "
            f"({len(report.written)} artifact(s) stamped)"
        )

    if report.errors:
        raise SystemExit(1)


def register_cli(application_group) -> None:
    """Attach ``migrate-ids`` to the ``application`` click group. Called from
    cli.py via a one-line registration (keeps cli.py under its size cap)."""
    application_group.add_command(migrate_ids_cmd)
