"""``application draft-identity`` CLI command (AL-1.6a).

Reports design §3's draft-identity rule — *discovered apps have no ``app_id``;
they carry a ``draft_id``* — across every identity-bearing artifact on the pod.
Logic lives in ``draft_identity.py``; the body lives here rather than in
cli.py, which is no-growth capped, and is attached to the ``application`` group
via a one-line ``register_cli`` call there — the same split ``migrate-ids`` and
``migrate-specs`` use.

**Three classes, not two, are excluded from the pass column.** ``grandfathered``
(the AL-1.4a backfill population) and ``spec_identity`` (a gallery Spec holding
an ``app_id`` — which the mint writes for every detection, drafts included) are
both printed in yellow beside the green rows rather than inside them. A census
that banked either as evidence would report the draft rule green on the very
artifacts that strain it.

READ-ONLY. There is no ``--apply``, deliberately: both repairs an ``--apply``
could plausibly perform (strip the ``app_id`` off 74 discovered manifests, or
bulk-promote them) are ruled out by existing design, and offering the switch
would invite exactly the re-identification AL-1.4 spent three PRs removing.

**The headline is the vacuity line, not the pass count.** When the pod holds
zero drafts, "discovered apps have no ``app_id``" is trivially true, and a
report that leads with a green count would be the third instance of the
vacuity trap this arc has already been caught by twice. So the first thing
printed is whether the rule is exercisable at all.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import click
from rich.console import Console

from ..config import DEFAULT_SHARED_DIR, load_network
from .draft_identity import (
    CLASS_CONTAMINATED,
    CLASS_DEFINED,
    CLASS_DRAFT,
    CLASS_GRANDFATHERED,
    CLASS_ORDER,
    CLASS_SPEC_IDENTITY,
    CLASS_UNPROMOTED,
    CLASS_UNSTAMPED,
    build_report,
)

_console = Console()

_CLASS_STYLE = {
    CLASS_DRAFT: "green",
    CLASS_DEFINED: "green",
    CLASS_GRANDFATHERED: "yellow",
    CLASS_SPEC_IDENTITY: "yellow",
    CLASS_UNSTAMPED: "cyan",
    CLASS_CONTAMINATED: "red",
    CLASS_UNPROMOTED: "red",
}

_CLASS_BLURB = {
    CLASS_DRAFT: "discovered, draft_id, no app_id — design §3 conforming",
    CLASS_DEFINED: "defined, app_id conferred — design §3 conforming",
    CLASS_GRANDFATHERED: (
        "discovered WITH an app_id — the AL-1.4a backfill got there first; "
        "named, counted as neither pass nor violation"
    ),
    CLASS_SPEC_IDENTITY: (
        "a gallery Spec carrying an app_id — the mint publishes one for every "
        "detection, INCLUDING drafts, so this is not a pass either"
    ),
    CLASS_UNSTAMPED: "neither app_id nor draft_id (see reason)",
    CLASS_CONTAMINATED: "VIOLATION: a draft holding a conferred app_id",
    CLASS_UNPROMOTED: "VIOLATION: defined, draft_id, no app_id — promotion did not confer",
}


@click.command("draft-identity")
@click.option("--bot", "bot", default="",
              help="Single bot id to census; default: all pod members.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the full census as JSON instead of a table.")
@click.option("--verbose", is_flag=True, default=False,
              help="List every artifact, not just violations and drafts.")
@click.pass_context
def draft_identity_cmd(
    ctx: click.Context, bot: str, as_json: bool, verbose: bool,
) -> None:
    """Census design §3's draft-identity rule. Read-only; never writes.

    Discovered apps must have a draft_id and no app_id; identity is conferred
    by promotion, not by discovery. Artifacts that predate AL-1.4a's app_id
    backfill are reported as 'grandfathered' rather than as passes — a run
    with zero drafts proves nothing about the rule, and this command says so.
    """
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    bot_ids = [bot] if bot else list(network.get("members", []))

    report = build_report(shared_dir, bot_ids)

    if as_json:
        click.echo(_json.dumps(report.to_dict(), indent=2))
        raise SystemExit(1 if (report.violations or report.errors) else 0)

    counts = report.counts
    _console.print(
        f"[green]draft-identity[/] {len(report.entries)} artifact(s) across "
        f"{len(bot_ids)} bot(s) + gallery [dim](read-only)[/]"
    )

    if report.draft_rule_is_vacuous:
        _console.print(
            "  [yellow]VACUOUS[/]: zero drafts on this pod, so "
            "\"discovered apps have no app_id\" is trivially true here and "
            "this run is not evidence for it. The rule binds at the MINT "
            "boundary — exercise it with a new detection, not with this "
            "census."
        )
    else:
        _console.print(
            f"  [green]exercisable[/]: {len(report.drafts)} draft(s) on this "
            "pod actually carry the rule."
        )

    for klass in CLASS_ORDER:
        if counts.get(klass):
            _console.print(
                f"  [{_CLASS_STYLE[klass]}]{klass}[/]: {counts[klass]}  "
                f"[dim]{_CLASS_BLURB[klass]}[/]"
            )
    reasons = report.unstamped_reasons
    for reason, n in sorted(reasons.items()):
        _console.print(f"      [dim]unstamped/{reason}: {n}[/]")

    if report.draft_residue:
        _console.print(
            f"  [yellow]![/] {len(report.draft_residue)} promoted app(s) still "
            "carry a draft_id — design §3 says a draft id never appears in "
            "attribution, access or sharing."
        )

    shown = report.entries if verbose else (report.violations + report.drafts)
    for e in shown:
        _console.print(
            f"  [{_CLASS_STYLE.get(e.klass, 'white')}]{e.klass}[/] "
            f"{e.kind} {e.app_id or e.draft_id or e.legacy_id or '(no id)'} "
            f"[dim]({e.bot_id or 'gallery'}"
            f"{'/' + e.reason if e.reason else ''})[/]"
        )
        if e.klass in (CLASS_CONTAMINATED, CLASS_UNPROMOTED):
            _console.print(f"      [dim]{e.path}[/]", soft_wrap=True)

    if report.violations:
        _console.print(
            f"  [red]![/] {len(report.violations)} artifact(s) violate design "
            "§3. Neither state can come from the AL-1.4a backfill, so each is "
            "a defect in a write path — report it; do not repair by hand."
        )
    for err in report.errors:
        _console.print(f"  [red]error[/] {err}")

    _console.print(
        "  [dim]walk follows network.json::members, like every census here; "
        "a bot home absent from members is not walked (AL-1.5 §9.8).[/]"
    )

    if report.violations or report.errors:
        raise SystemExit(1)


def register_cli(application_group) -> None:
    """Attach ``draft-identity`` to the ``application`` click group. Called
    from cli.py via a one-line registration (keeps cli.py under its size
    cap)."""
    application_group.add_command(draft_identity_cmd)
