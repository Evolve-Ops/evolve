"""``application snapshot`` CLI command (AL-3.1).

Logic lives in ``app_snapshot.py``; the body lives here rather than in
``cli.py``, which is no-growth capped, and is attached to the ``application``
group there via a one-line ``register_cli`` call — the same split
``migrate-specs`` / ``migrate-ids`` / ``repair-app-crons`` use.

WRITES, unlike its neighbours in that registration line. ``--dry-run`` is the
default for exactly that reason: the read half runs, the report prints in
full, and nothing lands until the operator asks for it with ``--apply``.
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path

import click
from rich.console import Console

_console = Console()


def _print_surface(surface: dict) -> None:
    """The shared-surface half of the report.

    Split out because it must ALSO print on a refusal: an app that declares
    no files is refused, and that is exactly the app whose whole behaviour
    lives in the shared surface. Swallowing the report there would leave the
    operator with a decline and no reason for it.
    """
    deltas = surface.get("deltas") or []
    counts = surface.get("counts") or {}
    _console.print(
        f"\n[bold]Shared-surface deltas:[/] {len(deltas)} "
        f"[dim](marker {counts.get('marker', 0)} / declared "
        f"{counts.get('declared', 0)} / config {counts.get('config', 0)} / "
        f"not found {counts.get('not_found', 0)})[/]"
    )
    for d in deltas:
        mark = "green" if d.get("attribution") == "marker" else "yellow"
        # The carriers spell an anchor both ways ("## Foo" and "Foo"); strip
        # the leading hashes so the artifact form reads "AGENTS.md#Foo"
        # rather than "AGENTS.md### Foo".
        section = str(d.get("section") or "").lstrip("#").strip()
        _console.print(
            f"  [{mark}]•[/] {d.get('file')}#{section} "
            f"[dim]{d.get('attribution')}"
            + (f" — {d.get('detail')}" if d.get("detail") else "")
            + "[/]"
        )
    unattr = surface.get("unattributed_sections") or {}
    if unattr.get("count"):
        _console.print(
            f"  [dim]{unattr.get('count')} section(s) on this BOT carry no "
            f"marker and are claimed by no manifest — the size of what "
            f"attribution cannot see, not this app's footprint.[/]"
        )
    _console.print(
        "  [dim]Report only: no delta is carried in the pack or the Spec. "
        "A footprint/shared_edits field is a §5-freeze decision.[/]"
    )


def _print_report(result: dict, *, applied: bool) -> None:
    """The operator-facing rendering of one snapshot envelope."""
    app_id = result.get("app_id", "")
    files = result.get("per_file") or []
    notes = result.get("notes") or []
    surface = result.get("shared_surface") or {}

    verb = "Snapshotted" if applied else "Would snapshot"
    _console.print(
        f"[cyan]→[/] {verb} [bold]{app_id}[/] — {len(files)} file(s)"
    )
    if result.get("changed"):
        label = "pack" if applied else "would write"
        _console.print(f"   {label}: {result.get('pack_dir')}")
    else:
        _console.print(
            f"   pack: {result.get('pack_dir')} "
            f"[dim](already current — {'nothing rewritten' if applied else 'no write needed'}"
            f", verified={result.get('verified')})[/]"
        )
    if result.get("spec_written"):
        _console.print(f"   spec: {result.get('spec_path')}")

    with_ph = [f for f in files if f.get("placeholders")]
    if with_ph:
        _console.print("\n[bold]Reverse-substituted placeholders:[/] "
                       "[dim](review — detection finds only the tokens it "
                       "was given)[/]")
        for f in sorted(with_ph, key=lambda e: e.get("path", "")):
            bare = f.get("bare_token_count") or 0
            tail = (f" [dim]({bare} bare bot_id token"
                    f"{'s' if bare != 1 else ''})[/]" if bare else "")
            _console.print(
                f"  [yellow]•[/] {f.get('path')}: "
                f"{', '.join(f.get('placeholders') or [])}{tail}"
            )

    with_tokens = [f for f in files if f.get("path_tokens")]
    if with_tokens:
        _console.print(
            "\n[bold]Source-bot tokens left in file PATHS:[/] "
            "[dim](content is substituted; names are not)[/]")
        for f in sorted(with_tokens, key=lambda e: e.get("path", "")):
            _console.print(
                f"  [yellow]•[/] {f.get('path')} "
                f"[dim]({', '.join(f.get('path_tokens') or [])})[/]"
            )

    if notes:
        _console.print("\n[bold]Files reported, not packed:[/]")
        for n in notes:
            _console.print(
                f"  [yellow]⚠[/] {n.get('path')}: {n.get('kind')}"
                + (f" — {n.get('detail')}" if n.get("detail") else "")
            )

    removed = result.get("removed") or []
    if removed:
        _console.print("\n[bold]Removed from the pack (no longer declared):[/]")
        for rel in removed:
            _console.print(f"  [dim]-[/] {rel}")

    _print_surface(surface)


@click.command("snapshot")
@click.option("--bot", "bot_id", required=True,
              help="Bot whose workspace holds the app's files.")
@click.option("--app", "app_id", required=True,
              help="The app's conferred app_id. A draft has none and is "
                   "refused.")
@click.option("--dry-run/--apply", "dry_run", default=True,
              help="Report only (default), or write the files-pack and "
                   "update the app's Spec.")
@click.option("--no-auto-detect", is_flag=True, default=False,
              help="Pack files verbatim: skip reverse-substitution, so "
                   "every placeholders[] list stays empty.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the raw envelope instead of the rendered report.")
@click.pass_context
def snapshot_app_cmd(
    ctx: click.Context,
    bot_id: str,
    app_id: str,
    dry_run: bool,
    no_auto_detect: bool,
    as_json: bool,
) -> None:
    """Turn a defined app into a files-pack (AL-3.1).

    Reads the app's declared files where the bot wrote them, reverse-
    substitutes the tokens that are known to vary per bot, writes a
    files-pack to ``{shared_dir}/apps/packs/<app_id>/`` and points the app's
    Spec ``package.files[].sha256`` at the pack's SOURCE digests. The pack is
    verified against ``verify_files_pack_integrity`` before the command
    reports success.

    Also detects and REPORTS what the app changed in shared OpenClaw
    surfaces — AGENTS.md / HEARTBEAT.md sections and declared config keys.
    Nothing is carried in the pack: an addressable ``footprint`` field is an
    operator decision (design §5 is frozen).

    Examples:

      \b
      # See what a snapshot would contain, change nothing:
      evolve-admin application snapshot --bot atlas --app task-manager

      \b
      # Write the pack and update the Spec:
      evolve-admin application snapshot --bot atlas --app task-manager --apply

    Exit codes: 0 success, 1 refusal (a draft, an app that is still
    ``discovered``, an app this bot does not have), 2 failure.
    """
    from ..config import load_network
    from .app_snapshot import snapshot_app

    network_path: Path = ctx.obj["network_path"]
    try:
        network = load_network(network_path)
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator
        _console.print(f"[red]✗[/] Could not load network.json: {exc}")
        sys.exit(2)

    result = snapshot_app(
        bot_id, app_id,
        network=network,
        auto_detect=not no_auto_detect,
        dry_run=dry_run,
    )

    if as_json:
        click.echo(_json.dumps(result, indent=2, sort_keys=True))
        sys.exit(0 if result.get("ok")
                 else (1 if result.get("refused") else 2))

    if not result.get("ok"):
        refused = bool(result.get("refused"))
        _console.print(
            (f"[yellow]—[/] {result.get('error')}") if refused
            else (f"[red]✗[/] {result.get('error')}")
        )
        for n in result.get("notes") or []:
            _console.print(
                f"  [yellow]⚠[/] {n.get('path')}: {n.get('kind')}"
                + (f" — {n.get('detail')}" if n.get("detail") else "")
            )
        if result.get("shared_surface"):
            _print_surface(result["shared_surface"])
        sys.exit(1 if refused else 2)

    _print_report(result, applied=not dry_run)
    if dry_run:
        _console.print(
            "\n[grey50]Dry run — nothing written. Re-run with --apply to "
            "write the pack and update the Spec.[/]"
        )
    sys.exit(0)


def register_cli(application_group) -> None:
    """Attach ``snapshot`` to the ``application`` click group. Called from
    cli.py via a one-line registration (keeps cli.py under its size cap)."""
    application_group.add_command(snapshot_app_cmd)
