"""``evolve-admin models`` — read the model-swap ledger and undo a swap.

Design: internal/design-model-swap-behavior-guard-2026-08-19.md. Command bodies
live here (not cli.py, which is no-growth capped); cli.py attaches via
``register_cli(models)`` folded onto its registration line — attaching to the
EXISTING ``models`` group, never re-declaring it.

Two commands:

``models swaps``
    Print the ledger — every recorded rung change, newest first. Read-only.

``models rollback BOT_ID --tier TIER``
    Restore the ``models[]`` the rung held before its most recent recorded
    swap. This is the one-command undo the 2026-08-14 incident did not have:
    the operator had to reconstruct the previous model from the audit log by
    hand, days after the swap.

The rollback writes through the SAME rung-collision-safe path the admin UI
uses (``model_tier_apply.stage_tier_model`` is not needed — the ledger stores
the full previous ``models[]``, so we restore it verbatim — but the
send-only-the-changed-tier rule and the ``model_landed`` post-write
verification both still apply; see ``model_tier_apply``). A rollback is
itself recorded in the ledger with ``source="cli_rollback"``, so the history stays
honest — but rollback records are skipped when choosing the next undo
target, which makes the command idempotent (running it twice leaves the
rung at the pre-swap model rather than re-applying the swap).

Registration runs at cli.py MODULE LOAD, so nothing at ``register_cli`` scope
may import the analyzer package — a failure there takes down the entire
``evolve-admin`` CLI, not just this group. Every such import lives inside a
command callback.
"""

from __future__ import annotations

from pathlib import Path


def _shared_dir(ctx) -> Path:
    """Resolve the shared dir from the global --network option."""
    from evolve_config import get_shared_dir

    from .config import load_network

    return Path(get_shared_dir(load_network(ctx.obj["network_path"])))


def register_cli(models_group) -> None:  # noqa: ANN001 — click.Group
    """Attach ``swaps`` / ``rollback`` to the EXISTING ``models`` group.

    Takes the group, not the top-level ``main`` — ``cli.py`` already defines
    ``@main.group() def models`` (roles/rungs management). Re-declaring
    ``@main.group("models")`` here would REPLACE it, silently deleting
    ``models set`` / ``list`` / ``show`` / ``cap`` / ``usage`` from the CLI.
    """
    import click
    from rich.console import Console

    console = Console()

    @models_group.command("swaps")
    @click.option("--bot", "bot_filter", default=None, help="Only this bot's swaps.")
    @click.option("--limit", default=20, show_default=True, help="Max rows to print.")
    @click.pass_context
    def models_swaps(ctx: click.Context, bot_filter: "str | None", limit: int) -> None:
        """Print recorded model-rung changes, newest first."""
        from model_swap_ledger import read_swaps  # type: ignore

        rows = read_swaps(_shared_dir(ctx))
        if bot_filter:
            rows = [r for r in rows if r.get("bot_id") == bot_filter]
        if not rows:
            console.print(
                "[yellow]No model swaps recorded.[/] The ledger starts at the "
                "first tier write after this feature shipped — swaps made "
                "before that are only in the audit log."
            )
            return
        console.print(f"[bold]{len(rows)} recorded swap(s)[/] — newest first:\n")
        for rec in list(reversed(rows))[:limit]:
            prev = ", ".join(rec.get("previous_models") or []) or "(none)"
            new = ", ".join(rec.get("new_models") or []) or "(none)"
            console.print(
                f"  [cyan]{rec.get('ts', '?')}[/]  "
                f"[bold]{rec.get('bot_id')}[/] / {rec.get('tier')} "
                f"({rec.get('source', '?')})\n"
                f"      {prev}  →  {new}"
            )

    @models_group.command("rollback")
    @click.argument("bot_id")
    @click.option("--tier", required=True, help="The rung to roll back (e.g. standard).")
    @click.option("--dry-run", is_flag=True, default=False,
                  help="Show the write without performing it.")
    @click.pass_context
    def models_rollback(ctx: click.Context, bot_id: str, tier: str, dry_run: bool) -> None:
        """Restore BOT_ID's TIER to the models it held before its last swap."""
        import sys

        from model_swap_ledger import (  # type: ignore
            ROLLBACK_SOURCE, latest_swaps_by_rung, record_swap,
        )
        from runtime.agent_runtime import get_runtime  # type: ignore

        from .web.model_tier_apply import tier_models

        shared_dir = _shared_dir(ctx)
        # Skip prior rollbacks so this command is idempotent — see
        # latest_swaps_by_rung. Undoing an undo would re-break the bot.
        swap = latest_swaps_by_rung(
            shared_dir, exclude_sources={ROLLBACK_SOURCE},
        ).get((bot_id, tier))
        if swap is None:
            console.print(
                f"[red]No recorded swap for {bot_id} / {tier}.[/] "
                "Run `evolve-admin models swaps` to see what is recorded; the "
                "ledger only covers tier writes made after it shipped."
            )
            sys.exit(1)

        target = list(swap.get("previous_models") or [])
        if not target:
            console.print(
                f"[red]The recorded swap for {bot_id} / {tier} has no previous "
                "models[/] — the rung was empty before it. Nothing to restore."
            )
            sys.exit(1)

        rt = get_runtime()
        cfg = rt.full_config_get(bot_id)
        if not cfg:
            console.print(f"[red]Could not read config for {bot_id}.[/]")
            sys.exit(1)
        current = tier_models(cfg, tier)

        console.print(
            f"[bold]Rollback {bot_id} / {tier}[/]\n"
            f"  swapped at : {swap.get('ts')} (via {swap.get('source', '?')})\n"
            f"  current    : {', '.join(current) or '(none)'}\n"
            f"  restore to : {', '.join(target)}"
        )
        if current == target:
            console.print("[yellow]Already at the pre-swap models — nothing to do.[/]")
            return
        if dry_run:
            console.print("[cyan]--dry-run: no write performed.[/]")
            return

        # Send ONLY this tier. The full synthesized dict would let an
        # unchanged sibling sharing the same rung clobber the restore — the
        # false-success bug model_tier_apply exists to prevent.
        entry = dict((cfg.get("tiers") or {}).get(tier) or {})
        entry["models"] = target
        result, err = rt.full_config_set_with_error(bot_id, {"tiers": {tier: entry}})
        if not result:
            console.print(f"[red]Write failed:[/] {err or 'check server logs'}")
            sys.exit(1)

        # A truthy setter result is NOT proof of persistence (model_tier_apply).
        landed = tier_models(result, tier)
        if landed != target:
            console.print(
                f"[red]Write reported success but {tier} is now "
                f"{landed} — the rollback did not persist.[/]"
            )
            sys.exit(1)

        record_swap(bot_id, tier, swap.get("provider"), current, target,
                    source="cli_rollback", shared_dir=shared_dir)
        console.print(
            f"[green]✓ {bot_id} / {tier} restored to {', '.join(target)}.[/]\n"
            "  Gateways pick the change up on their next session; restart the "
            "bot's gateway if you need it to take effect immediately."
        )
