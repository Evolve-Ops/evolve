#!/usr/bin/env python3
"""
dossier_edition.py — the weekly writer that accrues the pod dossier's spine.

Briefs: ``internal/dispatch/done/dossier-edition-zero.md`` (the raw-metric
editions) and ``internal/dispatch/done/dossier-modules.md`` (the synthesis
layer). Each run writes TWO files for its week: the edition — numbers only,
sealed once its window is complete — and the module set beside it, which says
in plain English what those numbers mean. See
``packages/analyzer/dossier/__init__.py`` for the shape and the laws both
obey (tri-state nulls, sealed-edition immutability, explicit windows,
trends that compare editions rather than recomputing history).

The reader is the Pod Intelligence page, which renders module sets and
reads earlier ones for its trend lines — it never writes here.

Which week gets written
-----------------------
  (default)          the most recently COMPLETED ISO week. This is what the
                     scheduled Monday run writes: every daily producer the
                     edition reads has already rolled the week over, so the
                     measurement is final and the edition seals.
  --now              the ISO week containing right now — still open, so the
                     edition is written UNSEALED and will be overwritten by
                     the next run for the same week. This exists so the spine
                     starts at merge instead of next Monday.
  --week 2026-W35    an explicit week (backfill / re-measure).

  --modules-only     re-synthesize the module set for a week from the
                     edition ALREADY on disk. Touches no measurement, so it
                     works on a sealed week without ``--force`` — the way
                     improved wording reaches weeks already recorded.

A run writes exactly one edition and one module set. A sealed edition is
never overwritten without ``--force``; an unsealed one is overwritten
idempotently, which is what makes re-running mid-week safe.

Usage:
    python3 dossier_edition.py --network /path/to/network.json
    python3 dossier_edition.py --shared-dir /path/to/shared --now
    python3 dossier_edition.py --shared-dir /path/to/shared --week 2026-W34
    python3 dossier_edition.py --shared-dir /path/to/shared --now --report
    python3 dossier_edition.py --shared-dir /path/to/shared --now --dry-run
    python3 dossier_edition.py --shared-dir /path/to/shared --week 2026-W34 \\
        --modules-only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR, load_config

from dossier import modules as mod, store, window as win
from dossier.edition import build_edition

LOG_PREFIX = "[dossier-edition]"


def resolve_week(
    network: dict[str, Any],
    now: datetime,
    *,
    week: str | None = None,
    use_now: bool = False,
) -> tuple[int, int]:
    """Which ISO week this invocation is about, in the pod's timezone."""
    tz = win.resolve_timezone(network)
    if week:
        return win.parse_edition_id(week)
    if use_now:
        return win.current_week(now, tz)
    return win.previous_week(now, tz)


def run_edition(
    shared_dir: Path,
    network: dict[str, Any],
    *,
    now: datetime | None = None,
    week: str | None = None,
    use_now: bool = False,
    force: bool = False,
    dry_run: bool = False,
    prune: bool = True,
    modules: bool = True,
) -> dict[str, Any]:
    """Build (and unless ``dry_run``, write) one edition. Returns the payload.

    ``modules=True`` writes the synthesis layer for the same week beside the
    edition, in the same run. It happens AFTER the edition write on purpose:
    the module set is derived from the edition, and a module set on disk
    whose edition failed to write would be a headline with no measurement
    behind it.
    """
    now = now or datetime.now(timezone.utc)
    tz = win.resolve_timezone(network)
    iso_year, iso_week = resolve_week(network, now, week=week, use_now=use_now)

    edition_window = win.window_for(iso_year, iso_week, tz, now=now)
    payload = build_edition(shared_dir, network, edition_window, now=now)
    store.write_edition(shared_dir, payload, force=force, dry_run=dry_run)
    if modules:
        run_modules(shared_dir, payload, now=now, dry_run=dry_run)
    if prune and not dry_run:
        dropped = store.prune_editions(shared_dir)
        store.prune_modules(shared_dir)
        if dropped:
            print(f"{LOG_PREFIX} pruned {len(dropped)} edition(s) past retention: "
                  f"{', '.join(dropped)}")
    return payload


def run_modules(
    shared_dir: Path,
    edition: dict[str, Any],
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Synthesize and write the module set for one edition. Returns it.

    The only history it reads is earlier EDITIONS (bounded by
    ``modules.MAX_TREND_LOOKBACK``) — never the producers those editions came
    from, which have since rolled over.
    """
    now = now or datetime.now(timezone.utc)
    priors = store.load_prior_editions(
        shared_dir, str(edition.get("edition_id") or ""),
        limit=mod.MAX_TREND_LOOKBACK,
    )
    payload = mod.build_modules(edition, priors, now=now)
    store.write_modules(shared_dir, payload, dry_run=dry_run)
    return payload


def _print_modules(payload: dict[str, Any] | None) -> None:
    """The module set, as the operator would hear it.

    Prints the headline of every module — including the ones that cannot be
    shown, because "we cannot show this yet, and here is why" is the output
    an operator most needs to see when a producer is missing.
    """
    if not payload:
        return
    on_record = (payload.get("based_on") or {}).get("editions_on_record")
    print(f"  modules  {len(payload.get('modules') or [])} module(s), "
          f"{on_record} week(s) on record")
    for module in payload.get("modules") or []:
        mark = "!" if module.get("critical") else ("·" if module.get("measurable")
                                                   else "?")
        print(f"    {mark} {module.get('title')}: {module.get('headline')}")


def _print_report(payload: dict[str, Any], path: Path, *, wrote: bool) -> None:
    """One line per top-level block, saying present-vs-null and how much.

    The point of the report is to make the tri-state visible: an operator
    running this by hand needs to see WHICH producers had nothing, because
    that is the difference between "quiet week" and "daemon not installed".
    """
    w = payload["window"]
    verb = "wrote" if wrote else "would write"
    print(f"{LOG_PREFIX} {verb} {path}")
    print(f"  window   {w['first_date']} .. {w['last_date']} ({w['timezone']}) "
          f"{'complete' if w['complete'] else 'OPEN'} / "
          f"{'sealed' if payload['sealed'] else 'unsealed'}")
    roster = payload["pod"]["roster"]
    print(f"  roster   {roster['members']} member(s), "
          f"active in window: {_fmt(roster['active_in_window'])}")
    for key in ("costs", "per_app", "users", "fires", "drafts", "drift",
                "signals"):
        print(f"  {key:<8} {_summarize(key, payload.get(key))}")


def _summarize(key: str, block: Any) -> str:
    if block is None:
        return "null (no producer / no data)"
    if key == "costs":
        return (f"${block['total_usd']:.4f}, {block['event_count']} events, "
                f"{len(block['by_model'])} model(s), "
                f"{block['bots_with_data']} bot(s) with data")
    if key == "per_app":
        cov = block["coverage"]["d7"]
        share = cov["unattributed_turns_share"]
        share_str = "n/a" if share is None else f"{share:.1%}"
        return (f"{len(block['apps'])} app(s) (rolling d7/d30 snapshot); "
                f"d7 coverage: {cov['attributed_turns']} attributed / "
                f"{cov['unattributed_turns']} unattributed ({share_str})")
    if key == "users":
        if block.get("available"):
            return f"{len(block.get('requesters') or [])} person/people"
        return f"null-with-schema ({block.get('note') or 'no per-person rollup'})"
    if key == "fires":
        apps = block.get("apps") or {}
        ran = sum(int(r.get("days_ran") or 0) for r in apps.values())
        missed = sum(int(r.get("days_missed") or 0) for r in apps.values())
        return (f"{len(apps)} app(s) with a run history over "
                f"{block['window']['days']} days: {ran} day(s) ran, "
                f"{missed} missed; "
                f"{len(block.get('apps_without_history') or [])} scheduled "
                f"app(s) with no record")
    if key == "drafts":
        bands = block.get("bands")
        return (f"{block['manifests']} manifest(s), "
                f"bands: {bands if bands is not None else 'null'}")
    if key == "drift":
        return f"{block['rows']} row(s) across {block['bots']} bot(s): {block['counts']}"
    if key == "signals":
        active = block["active"]
        trans = block.get("transitions")
        return (f"{active['total']} active ({active['by_severity']}), "
                f"transitions: {trans['total'] if trans else 'null'}")
    return "present"


def _fmt(value: Any) -> str:
    return "null" if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Weekly raw-metric edition of the pod dossier spine"
    )
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", help="Path to network.json")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--now", action="store_true",
        help="Write the CURRENT (still open) ISO week instead of the last "
             "completed one — how the spine starts at merge.",
    )
    group.add_argument("--week", help="Explicit ISO week id, e.g. 2026-W34")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite a sealed edition (discards a final "
                             "measurement — say so in the PR that needs it)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and report without writing")
    parser.add_argument("--modules-only", action="store_true",
                        help="Re-say the week from the edition already on "
                             "disk. Rewrites only the module set, so it "
                             "works on a sealed week without --force.")
    parser.add_argument("--report", action="store_true",
                        help="Print the per-block summary")
    parser.add_argument("--json", action="store_true",
                        help="Print the full payload to stdout")
    args = parser.parse_args(argv)

    try:
        network = load_config(args.network)
    except Exception as exc:
        print(f"{LOG_PREFIX} cannot read network config: {exc}", file=sys.stderr)
        return 1

    shared_dir = Path(args.shared_dir)
    if args.network:
        shared_dir = Path(network.get("sharedDir") or shared_dir)

    if args.modules_only:
        return _modules_only(shared_dir, network, args)

    try:
        payload = run_edition(
            shared_dir, network,
            week=args.week, use_now=args.now,
            force=args.force, dry_run=args.dry_run,
        )
    except store.SealedEditionError as exc:
        # Exit 0, loudly. The scheduled Monday job targets a COMPLETED week,
        # so any second firing for the same week lands here — and a weekly
        # writer that reports a job failure for "the measurement is already
        # recorded" is noise the operator will learn to ignore. Nothing is
        # overwritten either way; the store API still raises, so a
        # programmatic caller cannot clobber a seal by accident. The line is
        # explicit rather than silent: a run that MEANT to rewrite says so
        # in stdout and names the flag that would.
        print(f"{LOG_PREFIX} already recorded, not rewritten: {exc}")
        print(f"{LOG_PREFIX} (--modules-only re-says a sealed week without "
              f"touching its measurement)")
        return 0
    except ValueError as exc:
        print(f"{LOG_PREFIX} {exc}", file=sys.stderr)
        return 2

    path = store.edition_path(shared_dir, payload["edition_id"])
    modules = (store.load_modules(shared_dir, payload["edition_id"])
               if not args.dry_run else None)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if args.report or not args.json:
        _print_report(payload, path, wrote=not args.dry_run)
        _print_modules(modules)
    return 0


def _modules_only(shared_dir: Path, network: dict[str, Any], args: Any) -> int:
    """``--modules-only``: re-synthesize one week from the stored edition.

    Refuses rather than invents when the edition is missing: a module set
    with no edition behind it would be exactly the thing the tri-state law
    forbids — a headline whose numbers came from nowhere.
    """
    now = datetime.now(timezone.utc)
    try:
        iso_year, iso_week = resolve_week(
            network, now, week=args.week, use_now=args.now
        )
    except ValueError as exc:
        print(f"{LOG_PREFIX} {exc}", file=sys.stderr)
        return 2
    eid = win.edition_id(iso_year, iso_week)

    edition = store.load_edition(shared_dir, eid)
    if edition is None:
        print(f"{LOG_PREFIX} no edition on disk for {eid} — nothing to say "
              f"about a week that was never measured", file=sys.stderr)
        return 2

    payload = run_modules(shared_dir, edition, now=now, dry_run=args.dry_run)
    verb = "would re-say" if args.dry_run else "re-said"
    print(f"{LOG_PREFIX} {verb} {store.modules_path(shared_dir, eid)}")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_modules(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
