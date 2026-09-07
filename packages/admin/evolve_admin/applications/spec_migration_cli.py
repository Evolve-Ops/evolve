"""``application migrate-specs`` CLI command (AL-1.5a).

The v-next Spec readiness census. Logic lives in ``spec_migration.py``; the
body lives here rather than in cli.py, which is no-growth capped, and is
attached to the ``application`` group via a one-line ``register_cli`` call
there — the same split ``migrate-ids`` uses.

READ-ONLY IN BOTH MODES. ``--apply`` writes the census table and nothing else;
no manifest, Instance or gallery Spec is modified. v-next is migrate-on-read
(see spec_migration's module docstring), so there is no artifact rewrite for
``--apply`` to perform — that stays true in AL-1.5b, which makes the *reflex*
write a Spec when it defines an app and does NOT turn this command into a
pod-wide migration.

AL-1.5b added the shape line: how many artifacts already ARE v-next Specs
versus how many the reader migrated. That is the number the rollout is
measured against, so it prints before the per-status breakdown.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from ..config import DEFAULT_SHARED_DIR, load_network
from .spec_migration import (
    KIND_APP_SPEC,
    SHAPE_LEGACY,
    SHAPE_VNEXT,
    STATUS_BLOCKED,
    STATUS_CLEAN,
    STATUS_DRAFT,
    STATUS_PARTIAL,
    build_report,
)

_console = Console()


def _scope(entry) -> str:
    """Where the artifact lives, for the operator's eye.

    Three pod-wide sources now share ``bot_id == ""`` — gallery Specs and
    v-next App Specs — so "gallery" alone stopped being true the moment
    AL-1.5b's writer landed.
    """
    if entry.bot_id:
        return entry.bot_id
    return "pod" if entry.kind == KIND_APP_SPEC else "gallery"

_STATUS_STYLE = {
    STATUS_CLEAN: "green",
    STATUS_PARTIAL: "yellow",
    STATUS_DRAFT: "cyan",
    STATUS_BLOCKED: "red",
}


@click.command("migrate-specs")
@click.option("--bot", "bot", default="",
              help="Single bot id to census; default: all pod members.")
@click.option("--dry-run/--apply", "dry_run", default=True,
              help="Report only (default), or also write the census table. "
                   "Neither mode modifies any manifest or gallery Spec.")
@click.option("--verbose", is_flag=True, default=False,
              help="List every artifact, not just the ones needing attention.")
@click.pass_context
def migrate_specs_cmd(
    ctx: click.Context, bot: str, dry_run: bool, verbose: bool,
) -> None:
    """Census how every app on the pod reads as a v-next Spec.

    Derives the minimum spec (design §5) from each manifest, v7-arc Instance,
    gallery Spec and written v-next Spec, and reports what derives cleanly,
    what derives incompletely, and what cannot derive at all. The 'shape' line
    answers how much of the pod is v-next already. 'blocked' rows have no
    conforming identity and are not drafts — they cannot be published or
    granted against. 'no home' names real content the frozen field list has
    nowhere to put: a measurement for the operator, not a defect.
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
        f"{tag}[green]migrate-specs[/] {len(report.entries)} artifact(s) across "
        f"{len(bot_ids)} bot(s) + gallery"
    )
    shapes = report.shape_counts
    _console.print(
        f"  [magenta]shape[/]: {shapes[SHAPE_VNEXT]} v-next, "
        f"{shapes[SHAPE_LEGACY]} legacy (migrated on read)"
    )
    for status in (STATUS_CLEAN, STATUS_PARTIAL, STATUS_DRAFT, STATUS_BLOCKED):
        if counts.get(status):
            _console.print(f"  [{_STATUS_STYLE[status]}]{status}[/]: {counts[status]}")

    shown = report.entries if verbose else [
        e for e in report.entries if e.status in (STATUS_PARTIAL, STATUS_BLOCKED)
    ]
    for entry in shown:
        _console.print(
            f"  [{_STATUS_STYLE.get(entry.status, 'white')}]{entry.status}[/] "
            f"[magenta]{entry.shape}[/] {entry.kind} "
            f"{entry.app_id or '(no app_id)'} "
            f"v{entry.spec_version} [dim]({_scope(entry)})[/]"
        )
        for problem in entry.problems:
            _console.print(f"      [dim]- {problem}[/]", soft_wrap=True)
        if entry.status in (STATUS_PARTIAL, STATUS_BLOCKED):
            # Only rows an operator has to go and look at get the full path;
            # printing it for every row wraps the report into unreadable soup.
            _console.print(f"      [dim]{entry.path}[/]", soft_wrap=True)

    no_home = report.no_home_counts
    if no_home:
        _console.print(
            f"  [yellow]no home[/]: {len(no_home)} populated field(s) the "
            "frozen design-§5 list cannot carry"
        )
        for name, count in list(no_home.items())[:12]:
            _console.print(f"      [dim]{name}: {count} artifact(s)[/]")
        if len(no_home) > 12:
            _console.print(f"      [dim]… and {len(no_home) - 12} more[/]")

    unclassified = report.unclassified_counts
    if unclassified:
        _console.print(
            f"  [red]unclassified[/]: {len(unclassified)} top-level key(s) no "
            "disposition covers — decide before more of the pod goes v-next"
        )
        for name, count in list(unclassified.items())[:12]:
            _console.print(f"      [dim]{name}: {count} artifact(s)[/]")

    if report.blocking:
        _console.print(
            f"  [red]![/] {len(report.blocking)} artifact(s) cannot derive a "
            "portable spec — no conforming app_id and not a draft. AL-3.1 "
            "(publish then install elsewhere) is gated on these."
        )
    for err in report.errors:
        _console.print(f"  [red]error[/] {err}")

    if dry_run:
        _console.print(
            f"  [dim]table would be written to {report.table_path}; "
            "re-run with --apply (no artifact is modified either way)[/]"
        )
    else:
        _console.print(f"  [green]wrote[/] {report.table_path}")

    if report.errors:
        raise SystemExit(1)


def register_cli(application_group) -> None:
    """Attach ``migrate-specs`` to the ``application`` click group. Called from
    cli.py via a one-line registration (keeps cli.py under its size cap)."""
    application_group.add_command(migrate_specs_cmd)
