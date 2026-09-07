"""``evolve-admin models`` — read the model-swap ledger and undo a swap.

Design: internal/design-model-swap-behavior-guard-2026-08-19.md. Command bodies
live here (not cli.py, which is no-growth capped); cli.py attaches via
``register_cli(models)`` folded onto its registration line — attaching to the
EXISTING ``models`` group, never re-declaring it.

Four commands:

``models swaps``
    Print the ledger — every recorded rung change, newest first. Read-only.

``models rollback BOT_ID --tier TIER``
    Restore the ``models[]`` the rung held before its most recent recorded
    swap. This is the one-command undo the 2026-08-14 incident did not have:
    the operator had to reconstruct the previous model from the audit log by
    hand, days after the swap. The rollback also PINS the backed-out models
    as behavior-rejected (``model_swap_pins.jsonl``) so the tier-write
    endpoints refuse to reintroduce them without an explicit override — the
    2026-08-21 recurrence happened because a Model Freshness "Apply All"
    silently re-applied the very model a rollback had just backed out.

``models pins`` / ``models unpin BOT_ID --tier TIER --model MODEL``
    Show the active behavior pins / lift one deliberately.

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
            ROLLBACK_SOURCE, latest_swaps_by_rung, model_key, record_pin_event,
            record_swap,
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

        # The models the swap introduced and this rollback rejects — pinned
        # after the restore so a Model Freshness apply can't silently re-swap
        # them (the 2026-08-21 recurrence).
        rejected = [
            m for m in (swap.get("new_models") or [])
            if model_key(m) not in {model_key(t) for t in target}
        ]

        def _pin_rejected() -> None:
            for m in rejected:
                ok = record_pin_event(
                    bot_id, tier, m, action="pin",
                    reason=(
                        f"behavior-rejected by models rollback (swap "
                        f"{swap.get('ts')} via {swap.get('source', '?')})"
                    ),
                    source="cli_rollback", shared_dir=shared_dir,
                )
                if ok:
                    console.print(
                        f"[green]📌 pinned[/] {m} on {bot_id} / {tier} — the "
                        "admin tier-write endpoints will refuse to re-apply "
                        "it without an explicit override "
                        "(`evolve-admin models unpin` lifts it)."
                    )
                else:
                    console.print(
                        f"[red]Could not write the behavior pin for {m}[/] — "
                        "the rollback stands, but it is NOT sticky: a Model "
                        "Freshness apply can re-introduce this model. Check "
                        f"write access to the pin ledger under {shared_dir}."
                    )

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
        if dry_run:
            if rejected:
                console.print(
                    f"[cyan]--dry-run:[/] would pin {', '.join(rejected)} as "
                    "behavior-rejected."
                )
            console.print("[cyan]--dry-run: no write performed.[/]")
            return
        if current == target:
            console.print("[yellow]Already at the pre-swap models — nothing to do.[/]")
            # Still pin: the operator's intent is "reject the swapped-in
            # model", and a manual revert before running this command must
            # not leave the rollback non-sticky.
            _pin_rejected()
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
        # Declare the write so heal credits it instead of reporting the
        # restored rung as unexplained ``tiers:<key>`` drift forever
        # (spec-delta-digest-audit-noise-2026-08-25 D3). ``agents`` rides
        # along: the tier change recomputes openclaw.json's flat fallback.
        from .provisioning import _record_audit
        from .web.routes_shared import tier_write_oc_keys
        _record_audit("models.rollback", bot_id, {
            "tier": tier, "provider": swap.get("provider"),
            "from": current, "to": target, "source": "cli_rollback",
        }, oc_keys=tier_write_oc_keys(result, {"agents"}))
        _pin_rejected()
        console.print(
            f"[green]✓ {bot_id} / {tier} restored to {', '.join(target)}.[/]\n"
            "  Gateways pick the change up on their next session; restart the "
            "bot's gateway if you need it to take effect immediately."
        )

    @models_group.command("pins")
    @click.pass_context
    def models_pins(ctx: click.Context) -> None:
        """Show active behavior pins — models a rollback rejected for a rung."""
        from model_swap_ledger import PinLedgerUnreadable, active_pins  # type: ignore

        try:
            pins = active_pins(_shared_dir(ctx))
        except PinLedgerUnreadable as exc:
            console.print(
                f"[red]The pin ledger exists but could not be read:[/] {exc}\n"
                "The tier-write endpoints refuse applies while pin state is "
                "unknown — repair or remove the file."
            )
            raise SystemExit(1)
        if not pins:
            console.print("[yellow]No active behavior pins.[/]")
            return
        console.print(f"[bold]{len(pins)} active pin(s):[/]\n")
        for rec in sorted(pins.values(), key=lambda r: r.get("ts", "")):
            console.print(
                f"  [cyan]{rec.get('ts', '?')}[/]  "
                f"[bold]{rec.get('bot_id')}[/] / {rec.get('tier')}  "
                f"{rec.get('model')}\n"
                f"      {rec.get('reason', '')}"
            )

    @models_group.command("unpin")
    @click.argument("bot_id")
    @click.option("--tier", required=True, help="The pinned rung (e.g. standard).")
    @click.option("--model", "model", required=True,
                  help="The pinned model to allow again.")
    @click.pass_context
    def models_unpin(ctx: click.Context, bot_id: str, tier: str, model: str) -> None:
        """Lift a behavior pin so the model may be applied to the rung again."""
        from model_swap_ledger import (  # type: ignore
            PinLedgerUnreadable, find_active_pin, record_pin_event,
        )

        shared_dir = _shared_dir(ctx)
        try:
            pin = find_active_pin(bot_id, tier, model, shared_dir)
        except PinLedgerUnreadable as exc:
            console.print(f"[red]The pin ledger could not be read:[/] {exc}")
            raise SystemExit(1)
        if pin is None:
            console.print(
                f"[yellow]No active pin for {bot_id} / {tier} / {model}.[/] "
                "Run `evolve-admin models pins` to see what is pinned."
            )
            raise SystemExit(1)
        ok = record_pin_event(
            bot_id, tier, model, action="unpin",
            reason="lifted by operator via models unpin",
            source="cli_unpin", shared_dir=shared_dir,
        )
        if not ok:
            console.print("[red]Could not write the unpin record.[/]")
            raise SystemExit(1)
        console.print(
            f"[green]✓ unpinned[/] {model} on {bot_id} / {tier} — freshness "
            "applies may re-introduce it. model_swap_watch still judges any "
            "re-swap at the tighter repeat floor."
        )
