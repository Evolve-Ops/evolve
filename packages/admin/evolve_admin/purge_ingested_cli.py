"""``evolve-admin purge-ingested-backlog`` CLI command.

One-time, guarded, idempotent purge of the audit-outbox ``_ingested`` backlog
(``audit_outbox/_ingested`` per bot + the shared ``infra_audit_outbox/_ingested``).
Body lives here (not cli.py, which is no-growth capped); attached to the top-level
``main`` group via a one-line ``register_cli`` call there.

See ``evolve_admin.purge_ingested`` for the guards and rationale.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from .purge_ingested import _fmt_bytes, purge_ingested_backlog

_console = Console()


@click.command("purge-ingested-backlog")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what WOULD be removed (counts + bytes per target) without deleting.",
)
@click.option(
    "--older-than-days",
    type=int,
    default=None,
    help="Only remove date-dirs strictly older than N days (default: remove ALL).",
)
@click.option(
    "--shared-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the pod shared dir (default: platform profile default).",
)
@click.pass_context
def purge_ingested_backlog_cmd(
    ctx: click.Context,
    dry_run: bool,
    older_than_days: "int | None",
    shared_dir: "Path | None",
) -> None:
    """Purge the audit-outbox ``_ingested`` archive backlog pod-wide.

    Nothing in production reads ``_ingested`` (the audit poller only writes
    it). This clears the historical backlog the 30-day retention can't reach.
    The live outbox roots are never touched. Run ``--dry-run`` first.
    """
    from platform_profile import get_profile

    if shared_dir is None:
        shared_dir = Path(get_profile().shared_dir_default)

    result = purge_ingested_backlog(
        shared_dir,
        dry_run=dry_run,
        older_than_days=older_than_days,
    )

    verb = "would remove" if result.dry_run else "removed"
    for t in result.targets:
        if t.skipped_reason and not t.files_removed:
            _console.print(f"  [dim]{t.label:<20} {t.skipped_reason}[/]")
        else:
            color = "yellow" if result.dry_run else "green"
            _console.print(
                f"  [{color}]{t.label:<20}[/] {verb} {t.files_removed} files / "
                f"{_fmt_bytes(t.bytes_removed)} ({t.date_dirs_removed} date-dirs)"
            )
        for err in t.errors:
            _console.print(f"    [red]! {err}[/]")

    prefix = "[yellow]DRY-RUN — [/]" if result.dry_run else ""
    _console.print(
        f"{prefix}total {verb}: [bold]{result.total_files}[/] files / "
        f"[bold]{_fmt_bytes(result.total_bytes)}[/] across {len(result.targets)} "
        f"targets ({result.total_date_dirs} date-dirs)"
    )
    if result.dry_run:
        _console.print("[dim]re-run without --dry-run to delete.[/]")


def register_cli(main_group) -> None:
    """Attach ``purge-ingested-backlog`` to the top-level ``main`` click group."""
    main_group.add_command(purge_ingested_backlog_cmd)
