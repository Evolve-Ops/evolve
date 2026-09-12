"""evolve-admin cost … — reconcile the pod's cost estimate against the bill.

Every dollar figure Evolve shows is an estimate: token counts times a
published rate. ``cost reconcile`` is the standing check that the estimate
still matches what the provider actually charges — the check whose absence
let a family-substring price table overstate the power model ~3x for months
(``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §1).

    sudo evolve-admin cost reconcile --console-csv ~/Downloads/costs.csv
    sudo evolve-admin cost reconcile --console-csv costs.csv --bot personal_bot
    sudo evolve-admin cost reconcile --console-csv costs.csv --provider anthropic

``--console-csv`` is the daily cost export downloaded from the provider
console's Cost page (a date column, a model column, a cost column). Nothing
here reaches the network: a reconciliation that fetched its own truth would
need a billing credential on the pod.

The export is ONE provider's bill, so the estimate side is scoped to match.
``--provider`` is inferred from the export's model column when the pricing
catalog places it unambiguously, and required when it cannot: comparing an
all-provider estimate against a single-provider bill measures the pod's model
mix, not its pricing accuracy.

The command is thin. ``cost_reconcile`` (analyzer) owns the parse, the
comparison and the on-disk record, so the weekly receipt and this CLI can
never disagree about what a reconcile said.
"""
from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console

from .config import DEFAULT_NETWORK_CONFIG, load_network

console = Console()


@click.group("cost")
def cost_group() -> None:
    """Cost accuracy: reconcile Evolve's estimate against the provider bill."""


def _network_path(ctx: click.Context) -> Path:
    obj = ctx.obj or {}
    return Path(obj.get("network_path") or DEFAULT_NETWORK_CONFIG)


def _shared_dir(ctx: click.Context) -> Path:
    from .config import CANONICAL_SHARED_DIR
    try:
        network = load_network(_network_path(ctx))
    except Exception:  # noqa: BLE001 — a missing network.json is not fatal here
        network = {}
    return Path(network.get("sharedDir", CANONICAL_SHARED_DIR))


def _ratio_style(ratio: float | None, out_of_band: bool) -> str:
    if ratio is None:
        return "dim"
    return "yellow" if out_of_band else "green"


@cost_group.command("reconcile")
@click.option("--console-csv", "console_csv", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="The provider console's daily cost export (CSV).")
@click.option("--provider", "provider", default=None,
              help="The provider this export bills (anthropic, openai, xai, "
                   "…). Default: inferred from the export's model column via "
                   "the pricing catalog.")
@click.option("--bot", "bot_id", default=None,
              help="Reconcile one bot's turns only. Default: the whole pod.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the reconcile document instead of the table.")
@click.option("--no-write", is_flag=True, default=False,
              help="Print the comparison without writing "
                   "{shared_dir}/cost/reconcile-<date>.json.")
@click.pass_context
def cost_reconcile_cmd(
    ctx: click.Context, console_csv: Path, provider: str | None,
    bot_id: str | None, as_json: bool, no_write: bool,
) -> None:
    """Compare Evolve's per-day estimate with the provider console's figure."""
    from cost_reconcile import (  # type: ignore[import]
        ConsoleCsvError, ProviderScopeError, ratio_out_of_band, reconcile,
        write_reconcile,
    )

    shared_dir = _shared_dir(ctx)
    try:
        result = reconcile(
            console_csv=console_csv, shared_dir=shared_dir, provider=provider,
            bot_id=bot_id, network_path=_network_path(ctx),
        )
    except (ConsoleCsvError, ProviderScopeError) as exc:
        raise click.ClickException(str(exc)) from exc

    if not no_write:
        write_reconcile(shared_dir, result)

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return

    scope = bot_id or "pod-wide"
    console.print(
        f"[bold]Cost reconcile[/] — {scope}, {result.provider} turns only, "
        f"from {console_csv}"
    )
    console.print(
        f"  {'date':<12}{'estimate':>12}{'console':>12}{'delta':>12}{'ratio':>9}"
    )
    for day in result.days:
        drifting = ratio_out_of_band(day.ratio)
        ratio_text = "n/a" if day.ratio is None else f"{day.ratio:.2f}"
        console.print(
            f"  {day.date_iso:<12}"
            f"{day.estimate_usd:>11.2f} "
            f"{day.console_usd:>11.2f} "
            f"{day.delta_usd:>11.2f} "
            f"[{_ratio_style(day.ratio, drifting)}]{ratio_text:>8}[/]"
        )
        if day.turns_seen == 0:
            console.print(
                "    [dim]no turns read for this day — the estimate is "
                "\"did not measure\", not $0.00, so there is nothing to "
                "compare.[/]"
            )
        if day.unpriced_turns:
            console.print(
                f"    [yellow]can't price {day.unpriced_turns} turn(s)[/] — "
                f"the estimate for this day is a floor, so a low ratio may be "
                f"missing turns rather than a wrong rate."
            )
        if day.repriced_turns:
            console.print(
                f"    [dim]{day.repriced_turns} turn(s) re-priced from the "
                f"catalog on read (the recorded cost was a fallback "
                f"estimate).[/]"
            )
        if day.provider_guess_turns:
            console.print(
                f"    [yellow]{day.provider_guess_turns} turn(s) priced at a "
                f"provider-level guess[/] — no published price for that "
                f"model, so this much of the estimate is a mid-range rate for "
                f"some other {result.provider} model."
            )
        if day.excluded_turns:
            console.print(
                f"    [dim]{day.excluded_turns} turn(s) on other providers "
                f"(~${day.excluded_usd:.2f}) excluded — this export is "
                f"{result.provider}'s bill.[/]"
            )
    total_ratio = result.ratio
    total_ratio_text = "n/a" if total_ratio is None else f"{total_ratio:.2f}"
    total_delta = round(result.estimate_usd - result.console_usd, 6)
    console.print(
        f"  {'total':<12}"
        f"{result.estimate_usd:>11.2f} "
        f"{result.console_usd:>11.2f} "
        f"{total_delta:>11.2f} "
        f"{total_ratio_text:>8}"
    )
    if ratio_out_of_band(total_ratio):
        console.print(
            "[yellow]⚠️  Outside the 0.9–1.1 band.[/] The weekly receipt "
            "raises this as cost.estimate_drift; the daily cap, the 80% "
            "warning and the checkpoint message all run on the estimate."
        )
    if not no_write:
        console.print(
            f"[dim]Written to {shared_dir / 'cost'}/reconcile-<date>.json — "
            f"the weekly receipt cites the most recent one.[/]"
        )
