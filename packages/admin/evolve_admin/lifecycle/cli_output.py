"""lifecycle.cli_output — operator-facing console output for the lifecycle CLI.

The presentation layer for ``detach-bot`` / ``retire-bot`` / ``delete-bot``:
the pre-flight inventory preview and the post-run credential follow-up. Both
are pure output — they read, they print, they never block a lifecycle command
and never raise into one.

Why these live here and not in ``cli.py``
=========================================

``cli.py`` is at its ``tools/file-size-baseline.txt`` no-growth cap, which is
the mechanism that pushes exactly this kind of helper out of it (the same way
``analyzer_monitor_jobs`` was extracted from ``deploy.py``). Beyond the cap,
this is the right home on the merits: the package docstring for
``lifecycle/`` already names the manual-cleanup checklist for "off-host
artifacts (backup repo, bot tokens, SSH deploy keys, etc.) that Evolve cannot
safely automate from inside the pod" as part of this package's job.
:func:`print_credential_followup` is the first piece of that checklist to
actually ship.

The ``console`` is passed in rather than imported so this module carries no
dependency on ``cli`` (which imports it) — and so tests can capture output
with a plain ``rich.Console(file=StringIO())``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def print_lifecycle_preview(
    console: Any, bot_id: str, network_path: Path, action: str
) -> None:
    """One-shot pre-flight inventory preview, printed before a lifecycle run.

    Surfaces the count of items each action will touch and the off-host
    cleanup checklist (if any) so the operator sees the impact before the
    destructive op runs. Failure to load the inventory is silent — the
    lifecycle command continues. ``action`` is "detach", "archive", or
    "delete".
    """
    try:
        from ..config import load_network
        from . import compile_bot_inventory
        network = load_network(network_path)
        inv = compile_bot_inventory(bot_id, network=network)
    except Exception as e:  # noqa: BLE001 — preview is never a gate
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


def resolve_bot_home_quietly(bot_id: str, network_path: Path) -> "Path | None":
    """The bot's home via the blessed resolver, or None if it can't be read.

    Must be called BEFORE a retirement or deletion runs: afterwards the bot is
    gone from ``network.json`` and ``get_bot_user`` degrades to returning the
    bot id — which is wrong for any bot running under a different account name
    (on the reference pod ``team_bot_b`` runs on the account ``shared_account_b``), and would
    point the credential follow-up at a home that does not exist.
    """
    try:
        from evolve_config import bot_home
        from ..config import load_network
        return bot_home(bot_id, load_network(network_path))
    except Exception:  # noqa: BLE001 — cosmetic follow-up only
        return None


def collect_credentials_quietly(home: "Path | None") -> "Any | None":
    """The credential inventory for *home*, or None if it can't be produced.

    Split from the printer because ``delete-bot`` must call this **before** the
    deletion — afterwards the account and home are gone and there is nothing
    left to enumerate, even though the credentials themselves are still live
    at their sources. Retire calls it at the same point for symmetry.

    Never raises: a lifecycle command must not fail over a follow-up note.
    """
    if home is None:
        return None
    try:
        import bot_credential_inventory as _cred  # type: ignore  # analyzer pkg
        return _cred.collect(home)
    except Exception:  # noqa: BLE001 — see docstring
        return None


def print_credential_followup(
    console: Any, inv: "Any | None", *, deleted: bool = False
) -> None:
    """Name every credential still valid after a retirement or deletion.

    Retirement stops services and takes the bot off the roster; deletion also
    removes the account and home. **Neither revokes anything.** A Telegram bot
    token is revoked in BotFather, an SSH key by removing its public half from
    wherever it was authorized — actions at the credential's source that no
    code inside the pod can perform. So the least Evolve can do is name what
    needs revoking at the moment the operator is looking at the terminal.

    ``deleted=True`` changes the framing rather than the list: after
    ``delete-bot`` the local files are gone, but that makes revocation *more*
    urgent, not less — the token is still live at the platform and the local
    copy that would have reminded anyone no longer exists. This is the last
    moment the list can be shown at all.

    Takes a pre-collected inventory (see :func:`collect_credentials_quietly`)
    rather than a home, because on the delete path the home no longer exists
    by the time this runs. Never raises and never blocks: a note, not a gate.
    """
    if inv is None:
        return

    live = inv.live_artifacts
    if not live and not inv.unread:
        console.print("  [dim]No live credentials found in the account.[/]")
        return

    if deleted:
        console.print(
            f"\n[yellow]⚠ The account is gone, but {len(live)} credential(s) "
            "it held are STILL VALID at their source. This is the last time "
            "this list can be shown — revoke them now:[/]"
        )
    else:
        console.print(
            f"\n[yellow]⚠ {len(live)} credential(s) in this account are STILL "
            "LIVE — Evolve cannot revoke them for you:[/]"
        )
    for a in live:
        console.print(f"    · [bold]{a.location}[/] [dim]({a.shape})[/]")
        console.print(f"      [dim]revoke: {a.revoke_with}[/]")
    if inv.unread:
        console.print(
            f"  [dim]{len(inv.unread)} location(s) could not be read — this "
            "list may be incomplete.[/]"
        )
    if not deleted:
        console.print(
            "  [dim]Full detail is in the closure summary under "
            "'Credentials still live'.[/]"
        )
