"""``evolve-admin pod-baseline`` — pod baseline census + seeding (META:pod-plane B1).

Spec: internal/spec-pod-plane-2026-08-15.md. Command bodies live here (not
cli.py, which is no-growth capped); cli.py attaches via ``register_cli(main)``
folded onto its registration line.

READ-ONLY surface: ``census`` writes nothing; ``seed`` writes exactly one
file, ``{shared_dir}/pod-baseline.json`` — never any bot config.
"""
from __future__ import annotations

from pathlib import Path


def _pod_context(ctx):
    """Resolve (network, shared_dir) from the global --network option."""
    from evolve_config import get_shared_dir

    from .config import load_network

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    return network, get_shared_dir(network)


def register_cli(main) -> None:  # noqa: ANN001 — main is a click.Group
    """Attach the ``pod-baseline`` group to the top-level CLI group.

    Registration runs at ``cli.py`` MODULE LOAD, so it must stay cheap:
    nothing at this scope may import ``pod_baseline``, which ships in a
    separate distribution (``packages/analyzer``). Anything raised here
    takes down the ENTIRE ``evolve-admin`` CLI — every command, not just
    this group — which is exactly what a missing ``pod_baseline`` did on
    the Linux pod. Every ``pod_baseline`` import therefore lives inside a
    command callback, where a failure is loud, scoped to the one command
    that needed it, and names the missing package.
    """
    import json

    import click

    @main.group("pod-baseline")
    def pod_baseline_group() -> None:
        """Pod-level declared config intent: census + baseline seeding.

        The pod baseline (spec-pod-plane-2026-08-15) declares desired
        values for five per-bot surfaces: exec policy, tool profile,
        browser, context profile, model policy. A surface the pod has not
        declared is UNDECLARED — never reported as conform. Every bot on a
        declared surface either conforms, matches a declared exception, or
        deviates as TIGHTENED (harder than policy — informational),
        LOOSENED (the fault state) or DIVERGENT (no safety ordering).
        """

    @pod_baseline_group.command("census")
    @click.option("--json", "as_json", is_flag=True, default=False,
                  help="Emit the raw census report as JSON.")
    @click.pass_context
    def census_cmd(ctx: click.Context, as_json: bool) -> None:
        """Read-only census: classify every bot against the baseline."""
        from cost_profiles import load_custom_profiles
        from pod_baseline.census import classify_readings, read_pod_surfaces
        from pod_baseline.ordering import has_ordering
        from pod_baseline.schema import (
            STATE_DISPLAY_ORDER,
            SURFACES,
            STATE_CONFORM,
            STATE_DIVERGENT,
            STATE_EXCEPTION,
            STATE_LOOSENED,
            STATE_TIGHTENED,
            STATE_UNDECLARED,
            STATE_UNREADABLE,
            is_drift,
        )
        from pod_baseline.store import baseline_path, load_baseline

        # Q7(a): the three drift directions read differently on purpose.
        # TIGHTENED is informational — an operator who hardens a bot must
        # not be trained to file an exception to get the census green again.
        state_mark = {
            STATE_CONFORM: "ok",
            STATE_EXCEPTION: "EXCEPTION",
            STATE_TIGHTENED: "tightened",
            STATE_LOOSENED: "LOOSENED",
            STATE_DIVERGENT: "DIVERGENT",
            STATE_UNREADABLE: "UNREADABLE",
            STATE_UNDECLARED: "undeclared",
        }

        network, shared_dir = _pod_context(ctx)
        try:
            baseline = load_baseline(shared_dir)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        if baseline is None:
            raise click.ClickException(
                f"no baseline at {baseline_path(shared_dir)} — "
                "run 'evolve-admin pod-baseline seed' first"
            )
        problems = baseline.validate()
        if problems:
            raise click.ClickException(
                "pod-baseline.json is invalid: " + "; ".join(problems)
            )

        customs = tuple(load_custom_profiles(shared_dir))
        readings = read_pod_surfaces(network, custom_cost_profiles=customs)
        report = classify_readings(baseline, readings)

        if as_json:
            click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
            return

        click.echo(f"Pod baseline census — {len(readings)} bot(s), "
                   f"baseline updated {baseline.updated_at or '(unknown)'}")
        click.echo("Baseline: " + "  ".join(
            f"{s}={baseline.surfaces[s]}" if s in baseline.surfaces
            else f"{s}=(undeclared)"
            for s in SURFACES
        ))

        # Undeclared surfaces collapse to ONE pod-level line each carrying the
        # observed distribution (Q7(b)): undeclaredness is a pod fact, so N
        # identical per-bot rows are noise, and the distribution is the
        # operator's one-step path to declaring the surface.
        # A baseline seeded BEFORE Q7(b) can still declare a sentinel as pod
        # intent — both live pods do. Seeding can no longer produce that, and
        # census must not write, so the only honest move is to say so.
        if report.declared_sentinel_surfaces:
            click.echo("")
            click.echo(
                "! Declared as pod intent, but a no-intent sentinel: "
                + ", ".join(
                    f"{s}={baseline.surfaces[s]}"
                    for s in report.declared_sentinel_surfaces
                )
            )
            click.echo(
                "  These are the absence of a declaration, not a policy "
                "position, so the rows below report conform against a value "
                "nobody chose. Re-seed with 'pod-baseline seed --force' to "
                "leave them undeclared, or replace them with real values."
            )

        distribution = report.undeclared_distribution()
        excluded = report.undeclared_excluded()
        if report.undeclared_surfaces:
            click.echo("")
            click.echo(
                f"Undeclared — no pod intent ({len(report.undeclared_surfaces)} "
                f"of {len(SURFACES)} surfaces). These rows are neither conform "
                "nor drift; the counts are what each surface reads today:"
            )
            for surface in report.undeclared_surfaces:
                observed = distribution.get(surface) or {}
                dist = ", ".join(
                    f"{n} × {value}" for value, n in
                    sorted(observed.items(), key=lambda kv: (-kv[1], kv[0]))
                )
                # Rows this line does NOT cover — an unreadable config, or a
                # bot with its own exception — are named, never implied away.
                extra = excluded.get(surface) or {}
                if extra:
                    tail = ", ".join(
                        f"{n} {state} (listed per bot)"
                        for state, n in sorted(extra.items())
                    )
                    dist = f"{dist}; {tail}" if dist else tail
                click.echo(f"  {surface:<16} {dist or 'no bots read'}")
            click.echo(
                f"  Declare one: add it to \"surfaces\" in "
                f"{baseline_path(shared_dir)} and drop it from \"undeclared\"."
            )

        click.echo("")
        by_bot: dict = {}
        for row in report.rows:
            # Undeclared rows are represented by the collapsed block above.
            # Unreadable and exception rows on an undeclared surface are NOT
            # undeclared rows and still print per bot — "we could not look"
            # and "this bot has its own declared intent" are facts the
            # pod-level line cannot carry.
            if row.state == STATE_UNDECLARED:
                continue
            by_bot.setdefault(row.bot_id, []).append(row)
        for bot_id in sorted(by_bot):
            click.echo(f"{bot_id}:")
            for row in by_bot[bot_id]:
                mark = state_mark.get(row.state, row.state)
                detail = ""
                if is_drift(row.state):
                    detail = f" (want {row.expected or 'nothing declared'})"
                    if row.state == STATE_DIVERGENT and not has_ordering(row.surface):
                        # A divergent row means two different things. On a
                        # surface with a ladder it means "these two values
                        # are not comparable"; on one without, it means no
                        # safe direction exists at all and no amount of
                        # config will ever make this row tightened.
                        detail += " — no safety ordering on this surface"
                    if row.state == STATE_TIGHTENED and not row.exception_declared:
                        # Only meaningful against the POD baseline. On a row
                        # whose expected value already IS an exception, "no
                        # exception needed" reads as advice to delete the
                        # exception the operator deliberately declared.
                        detail += " — tighter than policy; no exception needed"
                    if row.stale_exception:
                        detail += " — matches pod baseline; exception looks stale, revoke it?"
                elif row.state == STATE_EXCEPTION:
                    detail = f" (declared: {row.exception_reason or 'no reason given'})"
                elif row.state == STATE_UNREADABLE:
                    detail = f" ({row.error or 'unreadable'})"
                observed = row.observed if row.observed is not None else "?"
                click.echo(f"  {row.surface:<16} {observed:<16} {mark}{detail}")
        counts = report.counts()
        click.echo("")
        # Every state, always — a state the summary cannot name is a state
        # the operator cannot see.
        click.echo("Summary: " + ", ".join(
            f"{counts.get(state, 0)} {state}" for state in STATE_DISPLAY_ORDER
        ))

    @pod_baseline_group.command("seed")
    @click.option("--force", is_flag=True, default=False,
                  help="Overwrite an existing pod-baseline.json.")
    @click.pass_context
    def seed_cmd(ctx: click.Context, force: bool) -> None:
        """Seed the baseline from the pod's current modal values.

        Prints what it chose and why. Refuses to elect a no-intent sentinel
        ("custom"/"unset") — such a surface is seeded UNDECLARED for the
        operator to fill in, rather than handed the fleet's own silence
        back as policy (Q7(b), decided 2026-08-22). Declares no exceptions:
        minority bots census as a drift state until the operator declares
        them (edit pod-baseline.json's exceptions list).
        """
        from evolve_util import now_iso

        from cost_profiles import load_custom_profiles
        from pod_baseline.census import read_pod_surfaces
        from pod_baseline.seed import (
            TIEBREAK_LEXICOGRAPHIC,
            TIEBREAK_SAFEST,
            seed_from_majority,
        )
        from pod_baseline.store import baseline_path, save_baseline

        network, shared_dir = _pod_context(ctx)
        path = baseline_path(shared_dir)
        # Overwrite guard. Only a confirmed-absent file skips it: a
        # present-but-unreadable baseline must abort, never read as
        # "doesn't exist" and get clobbered (fail-open guard hazard).
        try:
            path.read_bytes()
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError as exc:
            raise click.ClickException(
                f"cannot read {path} ({exc}) — refusing to overwrite a "
                "baseline that may exist; fix permissions first"
            )
        if exists and not force:
            raise click.ClickException(
                f"{path} already exists — it is operator-editable; "
                "pass --force to overwrite it with fresh modal values"
            )

        customs = tuple(load_custom_profiles(shared_dir))
        readings = read_pod_surfaces(network, custom_cost_profiles=customs)
        baseline, choices = seed_from_majority(readings, generated_at=now_iso())

        click.echo(f"Seeding pod baseline from {len(readings)} bot(s):")
        for choice in choices:
            # Same spelling as the census's collapsed distribution line —
            # one command group, one format.
            dist = ", ".join(
                f"{n} × {value}" for value, n in
                sorted(choice.counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ) or "no readable values"
            notes = []
            if choice.undeclared and choice.blocking_sentinels:
                notes.append(
                    "modal reading is "
                    + "/".join(choice.blocking_sentinels)
                    + " — no declared intent, not a policy position"
                )
            elif choice.undeclared:
                notes.append("nothing readable to elect from")
            if choice.tie and choice.tie_broken_by == TIEBREAK_SAFEST:
                notes.append("tie — picked the safest observed value")
            elif choice.tie and choice.tie_broken_by == TIEBREAK_LEXICOGRAPHIC:
                notes.append(
                    "tie with no safety ordering to break it — picked "
                    "lexicographically; edit if wrong"
                )
            elif choice.tie:
                notes.append("tie")
            if choice.unknown:
                notes.append(f"{choice.unknown} unreadable, excluded from vote")
            suffix = f"  [{'; '.join(notes)}]" if notes else ""
            target = "(undeclared)" if choice.undeclared else choice.chosen
            click.echo(f"  {choice.surface:<16} -> {target:<16} ({dist}){suffix}")

        out = save_baseline(shared_dir, baseline)
        click.echo(f"Wrote {out}")
        undeclared = [c.surface for c in choices if c.undeclared]
        if undeclared:
            click.echo(
                f"{len(undeclared)} surface(s) left undeclared "
                f"({', '.join(undeclared)}) — the census reports them as "
                "undeclared, never conform, until you declare a value."
            )
        click.echo("Next: 'evolve-admin pod-baseline census' to see who conforms; "
                   "declare exceptions for intentional deviations.")
