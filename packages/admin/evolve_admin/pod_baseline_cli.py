"""``evolve-admin pod-baseline`` — pod baseline census + seeding (META:pod-plane B1).

Spec: docs/spec-pod-plane-2026-08-15.md. Command bodies live here (not
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
        browser, context profile, model policy. Every bot either conforms,
        matches a declared exception, or is DRIFT.
        """

    @pod_baseline_group.command("census")
    @click.option("--json", "as_json", is_flag=True, default=False,
                  help="Emit the raw census report as JSON.")
    @click.pass_context
    def census_cmd(ctx: click.Context, as_json: bool) -> None:
        """Read-only census: classify every bot against the baseline."""
        from cost_profiles import load_custom_profiles
        from pod_baseline.census import classify_readings, read_pod_surfaces
        from pod_baseline.schema import (
            SURFACES,
            STATE_CONFORM,
            STATE_DRIFT,
            STATE_EXCEPTION,
            STATE_UNREADABLE,
        )
        from pod_baseline.store import baseline_path, load_baseline

        state_mark = {
            STATE_CONFORM: "ok",
            STATE_EXCEPTION: "EXCEPTION",
            STATE_DRIFT: "DRIFT",
            STATE_UNREADABLE: "UNREADABLE",
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
            f"{s}={baseline.surfaces.get(s, 'unset')}" for s in SURFACES
        ))
        click.echo("")
        by_bot: dict = {}
        for row in report.rows:
            by_bot.setdefault(row.bot_id, []).append(row)
        for bot_id in sorted(by_bot):
            click.echo(f"{bot_id}:")
            for row in by_bot[bot_id]:
                mark = state_mark.get(row.state, row.state)
                detail = ""
                if row.state == STATE_DRIFT:
                    detail = f" (want {row.expected})"
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
        click.echo("Summary: " + ", ".join(
            f"{counts.get(state, 0)} {state}"
            for state in (STATE_CONFORM, STATE_EXCEPTION, STATE_DRIFT, STATE_UNREADABLE)
        ))

    @pod_baseline_group.command("seed")
    @click.option("--force", is_flag=True, default=False,
                  help="Overwrite an existing pod-baseline.json.")
    @click.pass_context
    def seed_cmd(ctx: click.Context, force: bool) -> None:
        """Seed the baseline from the pod's current modal values.

        Prints what it chose and why. Declares no exceptions — minority
        bots will census as DRIFT until the operator declares them (edit
        pod-baseline.json's exceptions list).
        """
        from evolve_util import now_iso

        from cost_profiles import load_custom_profiles
        from pod_baseline.census import read_pod_surfaces
        from pod_baseline.seed import seed_from_majority
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
            dist = ", ".join(
                f"{value}×{n}" for value, n in
                sorted(choice.counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ) or "no readable values"
            notes = []
            if choice.tie:
                notes.append("tie — picked lexicographically; edit if wrong")
            if choice.unknown:
                notes.append(f"{choice.unknown} unreadable, excluded from vote")
            suffix = f"  [{'; '.join(notes)}]" if notes else ""
            click.echo(f"  {choice.surface:<16} -> {choice.chosen:<16} ({dist}){suffix}")

        out = save_baseline(shared_dir, baseline)
        click.echo(f"Wrote {out}")
        click.echo("Next: 'evolve-admin pod-baseline census' to see who conforms; "
                   "declare exceptions for intentional deviations.")
