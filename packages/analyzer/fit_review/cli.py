"""fit_review.cli — run the Bite-1 targeting report for a bot.

Bite 1 is pure Python (no LLM, no proposal). This CLI is the operator / proof
surface: it reads a bot's declared purpose from ``network.json`` and its
observation tuples from the shared store, then prints the targeting report.

Examples
--------
Run targeting against a live (or copied) shared dir, reading purpose from
``network.json``::

    python3 -m fit_review.cli --shared-dir /Users/Shared/evolve \\
        --network /Users/Shared/evolve/network.json --bot team-bot-a --json

Declare a purpose anchor *in memory only* for the run (does not touch
``network.json``) — handy for a proof against real tuples before the operator
declares the anchor for real via ``PUT /api/bot/<id>/purpose``::

    python3 -m fit_review.cli --shared-dir /Users/Shared/evolve --bot team-bot-a \\
        --purpose-archetype project-manager \\
        --purpose-mission "Run the team's projects: track tasks, report status."

Persist the declaration into ``network.json`` (mirrors the admin write path)
with ``--write-purpose`` (requires ``--network``).
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). ``g['pkg_id']`` is a gallery catalog row's
# primary key rendered as a console-table column. See fit_review/__init__.py
# for why a catalog row must not be run through the manifest resolver.

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR
from fit_review.targeting import build_targeting_report_for_bot


def _load_network(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"bots": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"[fit_review] cannot read network.json at {path}: {exc}")
    return data if isinstance(data, dict) else {"bots": {}}


def _inject_purpose(
    network: dict[str, Any],
    bot_id: str,
    archetype: str,
    mission: str,
    *,
    write_to: Path | None,
) -> None:
    """Declare a purpose for the run via the shared write path. When
    ``write_to`` is set, persist network.json; otherwise mutate in memory only."""
    from bot_purpose import set_bot_purpose

    network.setdefault("bots", {}).setdefault(bot_id, {})
    set_bot_purpose(
        network,
        bot_id,
        {"archetype": archetype, "mission": mission},
        captured="declared",
        reviewed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if write_to is not None:
        write_to.write_text(json.dumps(network, indent=2) + "\n", encoding="utf-8")


def _print_human(report) -> None:
    d = report.to_dict()
    print(f"Bot:        {d['bot_id']}")
    print(f"Purpose:    archetype={d['archetype']!r} declared={d['has_declared_purpose']}")
    if d["mission"]:
        print(f"Mission:    {d['mission']}")
    print(f"Window:     {d['window_days']} days")
    print(f"Decision:   {d['decision']}")
    print(f"Reason:     {d['reason']}")
    floor = d["support_floor"]
    print(
        f"Floor:      distinct_sessions >= {floor['distinct_sessions']}, "
        f"distinct_days >= {floor['distinct_days']}"
    )
    print("\nNoun rollup (top 12):")
    print(
        f"  {'noun':<22} {'tuples':>6} {'sess':>4} {'days':>4} "
        f"{'frust':>5}  {'align':<9} floor  cand"
    )
    for r in d["rollups"][:12]:
        print(
            f"  {r['noun']:<22} {r['tuple_count']:>6} "
            f"{r['distinct_sessions']:>4} {r['distinct_days']:>4} "
            f"{r['frustration_share']:>5.2f}  {r['alignment']:<9} "
            f"{'Y' if r['above_floor'] else '-':<5}  {'Y' if r['is_candidate'] else '-'}"
        )
    print(f"\nCandidates: {', '.join(d['candidates']) or '(none)'}")
    print("Gallery shortlist (real pkg_ids):")
    if not d["shortlist"]:
        print("  (none)")
    for g in d["shortlist"]:
        print(
            f"  {g['pkg_id']}  {g['name']:<28} "
            f"covers={','.join(g['matched_domains'])} "
            f"(coverage={g['coverage_count']}, weight={g['weight']})"
        )
    print(f"\nPrimary target: {d['primary_pkg_id'] or '(none)'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fit Reviewer — Bite 1 targeting report.")
    p.add_argument("--shared-dir", type=Path, default=CANONICAL_SHARED_DIR)
    p.add_argument("--network", type=Path, default=None, help="network.json path")
    p.add_argument("--bot", required=True)
    p.add_argument("--window-days", type=int, default=None)
    p.add_argument("--purpose-archetype", default=None)
    p.add_argument("--purpose-mission", default=None)
    p.add_argument(
        "--write-purpose",
        action="store_true",
        help="Persist the declared purpose into --network (mirrors admin write).",
    )
    p.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = p.parse_args(argv)

    network = _load_network(args.network)
    if args.purpose_archetype or args.purpose_mission:
        if not (args.purpose_archetype and args.purpose_mission):
            raise SystemExit(
                "[fit_review] --purpose-archetype and --purpose-mission "
                "must be given together"
            )
        _inject_purpose(
            network,
            args.bot,
            args.purpose_archetype,
            args.purpose_mission,
            write_to=args.network if args.write_purpose else None,
        )

    kwargs: dict[str, Any] = {"network": network, "shared_dir": args.shared_dir}
    if args.window_days is not None:
        kwargs["window_days"] = args.window_days
    report = build_targeting_report_for_bot(args.bot, **kwargs)
    report.generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
