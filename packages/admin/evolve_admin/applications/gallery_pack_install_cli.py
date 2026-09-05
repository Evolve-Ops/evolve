"""``application install-gallery-pack`` CLI command.

Logic lives in ``gallery_pack_install.py``; the body lives here rather than in
``cli.py``, which is no-growth capped, and is attached to the ``application``
group there via a one-line ``register_cli`` call — the same split
``snapshot`` / ``migrate-specs`` / ``migrate-ids`` use.

WRITES on ``--apply`` (the pack + Spec under ``{shared_dir}/apps``, the files on
the bot, the AGENTS.md section). ``--dry-run`` is the default for exactly that
reason: the plan prints in full and nothing lands until the operator asks.

    sudo evolve-admin application install-gallery-pack --pkg p-38da680b --bot <bot>
    sudo evolve-admin application install-gallery-pack --pkg p-38da680b --bot <bot> --apply
"""

from __future__ import annotations

import json as _json
import sys

import click
from rich.console import Console

_console = Console()


def _print_result(result: dict) -> None:
    if not result.get("ok"):
        kind = "refused" if result.get("refused") else "failed"
        _console.print(f"[red]✗ install {kind}:[/] {result.get('error', '')}")
        install = result.get("install") or {}
        for item in install.get("failed") or []:
            _console.print(f"    [red]•[/] {item.get('rel')}: {item.get('error')}")
        for sec in result.get("sections") or []:
            if not sec.get("ok"):
                _console.print(f"    [red]•[/] {sec['file']}#{sec['section_anchor']}: {sec['error']}")
        return
    seed = result.get("seed") or {}
    install = result.get("install") or {}
    mode = "DRY RUN — nothing written" if result.get("dry_run") else "applied"
    _console.print(
        f"[bold]{result['app_id']}[/] ← gallery {result['pkg_id']} → bot "
        f"[bold]{result['bot_id']}[/]  [dim]({mode})[/]"
    )
    _console.print(f"  pack: {seed.get('pack_dir')}  [dim]sha {str(seed.get('pack_sha256', ''))[:12]}[/]"
                   + ("  [dim](already seeded)[/]" if seed.get("already_seeded") else ""))
    _console.print(f"  spec: {seed.get('spec_path')}")
    if result.get("dry_run"):
        for path in install.get("planned") or []:
            _console.print(f"    [dim]plan[/] {path}")
    else:
        for path in install.get("installed") or []:
            _console.print(f"    [green]✓[/] {path}")
        proof = install.get("proof") or {}
        _console.print(f"  proof: realized diffs explained by substitution = {proof.get('explained')}")
        _console.print(f"  instance: {install.get('manifest_path')}")
    for sec in result.get("sections") or []:
        if result.get("dry_run"):
            _console.print(f"    [dim]plan[/] {sec['file']}#{sec['section_anchor']} ({sec['bytes']} bytes)")
        else:
            state = "already present" if sec.get("already_present") else "written"
            _console.print(f"    [green]✓[/] {sec['file']}#{sec['section_anchor']} — {state}")
    if result.get("installed_apps_md"):
        _console.print(f"  menu: {result['installed_apps_md']}")
    if result.get("dry_run"):
        _console.print("\nRe-run with [bold]--apply[/] to install.")


@click.command("install-gallery-pack")
@click.option("--pkg", "pkg_id", required=True, help="gallery package id (p-xxxxxxxx) that ships a files/ pack")
@click.option("--bot", "bot_id", required=True, help="target bot id")
@click.option("--apply", "apply_", is_flag=True, default=False,
              help="write: seed the pack + Spec, install the files, install the AGENTS.md section")
@click.option("--json", "as_json", is_flag=True, default=False, help="print the raw envelope")
def install_gallery_pack_cmd(pkg_id: str, bot_id: str, apply_: bool, as_json: bool) -> None:
    """Install a gallery files-pack app onto a bot through the AL-3.2 spine (no LLM).

    Seeds {shared_dir}/apps/packs/<app_id>/ and the Spec from the gallery's
    own files/ pack, runs the deterministic install (create-only, sha-proved),
    then installs the package's AGENTS.md section with the evolve-managed
    marker. Dry-run by default.
    """
    from .gallery_pack_install import install_gallery_pack

    result = install_gallery_pack(pkg_id, bot_id, dry_run=not apply_)
    if as_json:
        print(_json.dumps(result, indent=2, default=str))
    else:
        _print_result(result)
    if not result.get("ok"):
        sys.exit(2 if result.get("refused") else 1)


def register_cli(application_group) -> None:
    """Attach ``install-gallery-pack`` to the ``application`` click group.
    Called from cli.py via a one-line registration (keeps cli.py under its
    size cap)."""
    application_group.add_command(install_gallery_pack_cmd)
