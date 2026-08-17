"""CLI: ``application stamp-deliveries`` — backfill delivery contracts on
user-facing extracted scheduled_actions.

The command body lives here (not inline in ``cli.py``) so the hot-hazard
``cli.py`` stays under its no-growth cap (4.1a); it is attached to the
``application`` group via :func:`register_cli`, mirroring
``migrate_model_roles.register_cli``.

Background: an app adopted via scanner extraction (never forge-approved) can keep
``outputs:[]`` + ``delivery_contract:null`` even when its bound Spec declares
a user-facing delivery, leaving it invisible to the proactive-delivery
monitor. The scan's pod-wide repair leg now re-asserts the contract on every
run; this command applies the same stamp immediately, without a full LLM
scan. See :func:`forge_engine.stamp_bot_user_facing_deliveries`.
"""

from __future__ import annotations

import sys
from pathlib import Path


def register_cli(application) -> None:  # noqa: ANN001 — click.Group
    """Attach the ``stamp-deliveries`` command to the ``application`` group."""
    import click
    from rich.console import Console

    from ..config import DEFAULT_SHARED_DIR, load_network
    from .forge_engine import stamp_bot_user_facing_deliveries

    console = Console()

    @application.command("stamp-deliveries")
    @click.argument("bot_id", required=False)
    @click.option("--all-bots", is_flag=True, default=False,
                  help="Stamp every bot in network.json instead of a single BOT_ID")
    @click.pass_context
    def application_stamp_deliveries(
        ctx: click.Context, bot_id: str | None, all_bots: bool,
    ) -> None:
        """Backfill delivery contracts on user-facing extracted scheduled_actions.

        An app adopted via scanner extraction (never forge-approved) can keep
        outputs:[] + delivery_contract:null even when its bound Spec declares a
        user-facing delivery, leaving it invisible to the proactive-delivery
        monitor. The scan's pod-wide repair leg re-asserts the contract on every
        run; this command applies the same stamp immediately, without a full LLM
        scan. Spec-gated + live-channel-gated, so internal actions are left
        untouched. Writes manifests — run in the admin (evolve) context.
        """
        network_path: Path = ctx.obj["network_path"]
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

        if all_bots and bot_id:
            console.print("[red]Provide BOT_ID or --all-bots, not both.[/]")
            sys.exit(1)
        targets: list[str]
        if all_bots:
            targets = [str(b) for b in (network.get("bots") or {}).keys()]
        elif bot_id:
            targets = [bot_id]
        else:
            console.print("[red]Provide BOT_ID or --all-bots.[/]")
            sys.exit(1)

        total = 0
        for b in targets:
            stamped_map = stamp_bot_user_facing_deliveries(b, shared_dir)
            for app_id, action_ids in stamped_map.items():
                console.print(
                    f"[green]{b}/{app_id}[/]: stamped {', '.join(action_ids)} "
                    f"(now monitored)"
                )
                total += len(action_ids)
        console.print(
            f"[green]Stamped {total} action(s) across {len(targets)} bot(s).[/]"
            if total else "[dim]Nothing to stamp — all user-facing deliveries "
            "already declared (or no live channel).[/]"
        )
