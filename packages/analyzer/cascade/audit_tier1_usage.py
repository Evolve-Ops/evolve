"""cascade.audit_tier1_usage — pod-wide tier1 spend + attribution report.

Quick "who's running opus and why" diagnostic. Reads cascade spans
across all bots, filters to ``tier_used == "tier1"``, and prints a
detail table + cost rollup.

Invoke:
    python3 -m cascade.audit_tier1_usage              # last 14 days, all bots
    python3 -m cascade.audit_tier1_usage --days 7
    python3 -m cascade.audit_tier1_usage --bot team_bot_a
    python3 -m cascade.audit_tier1_usage --summary    # no per-turn detail
    python3 -m cascade.audit_tier1_usage --shared-dir /custom/path

Why this exists
---------------
Tier1 is the expensive cost class (~18x sonnet rates). Every tier1
turn is intentional or a bug; an operator should be able to see the
list and the reason for each in one shot. This tool produces that.

It also catches the "attribution bug" class: if ``tier_chosen_by``
is ``"default"`` on every tier1 turn pod-wide, the audit layer's
``preflight_over_escalation`` (and related) Signals can never fire
accurately — the driver tag is the input the audit groups by, and
``"default"`` is ambiguous between many real drivers. The tool
explicitly surfaces that distribution so the operator can spot it.

Output structure
----------------
Default mode prints two sections:

1. Per-turn detail table:
   when | bot | cost | chosen_by | tier_intended | preflight layer/reason | model

2. Summary:
   total turns, total cost, per-bot breakdown, driver-of-tier1
   distribution (which sees whether ``"default"`` is dominant —
   the attribution-bug signature).

``--summary`` omits the detail table and just prints section 2.

Sample output (the 2026-06-08 audit run that motivated this tool):

    # tier1 turns last 14 days: 7
    # tier1 total cost: $1.45
    # driver distribution:
    #   default                7 (100.0%)   ← all of pod-wide tier1 is mislabeled
    #   preflight              0 (  0.0%)
    #   ...
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


# Tier1 attribution buckets the report cares about. Mirrors the
# TierChosenBy enum used in cascade telemetry spans. Listed in cost-
# attribution order (most-explicit → most-implicit) so the summary
# reads naturally.
_DRIVER_ORDER = [
    "user_request",
    "user_model_override",
    "operator_default",  # not yet emitted directly (collapses to "default")
    "cascade",
    "preflight",
    "classifier",
    "default",
    "spend_cap",
    "<null>",
]


def _load_tier1_spans(
    shared_dir: Path,
    *,
    since: datetime,
    until: datetime,
    bot_id: str | None = None,
) -> list[dict]:
    """Read cascade spans in the window; filter to tier1 turns.

    Reads via the shared ``iter_turn_spans`` helper so we honor the
    same cross-location merge logic the audit_runner uses. Filtering
    happens in Python because the iterator returns all cascade spans
    and the tier1 subset is small.
    """
    try:
        from observability.session_rollup import iter_turn_spans
    except ImportError as e:
        print(
            f"ERROR: could not import iter_turn_spans: {e}\n"
            f"This tool must run with the analyzer package on the import path. "
            f"Try: python3 -m cascade.audit_tier1_usage (run from packages/analyzer)",
            file=sys.stderr,
        )
        sys.exit(2)

    out: list[dict] = []
    for span in iter_turn_spans(
        shared_dir, since=since, until=until, bot_id=bot_id, limit=200_000,
    ):
        d = span.to_dict()
        attrs = d.get("attributes") or {}
        if attrs.get("cascade.tier_used") != "tier1":
            continue
        out.append(d)
    return out


def _format_detail_row(span: dict) -> str:
    """One line per tier1 turn — wide table format, single line each."""
    attrs = span.get("attributes") or {}
    start = (span.get("start_time") or "")[:19]
    bot = span.get("bot_id") or "?"
    cost = span.get("total_cost") or 0
    chosen_by = attrs.get("cascade.tier_chosen_by") or "<null>"
    tier_intended = attrs.get("cascade.tier_intended") or "?"
    pf_layer = attrs.get("cascade.preflight.layer") or "<null>"
    pf_reason = attrs.get("cascade.preflight.reason") or "<null>"
    model = span.get("model") or "?"
    return (
        f"{start:<19} {bot:<10} ${cost:>5.2f}  "
        f"chosen={chosen_by:<14} intended={tier_intended:<6}  "
        f"pf={pf_layer:<8} reason={pf_reason:<34} {model}"
    )


def _summarize(spans: list[dict]) -> dict:
    """Aggregate per-bot + per-driver counts and cost."""
    total_count = len(spans)
    total_cost = sum(s.get("total_cost") or 0 for s in spans)
    per_bot: Counter = Counter()
    per_bot_cost: defaultdict[str, float] = defaultdict(float)
    per_driver: Counter = Counter()
    per_driver_cost: defaultdict[str, float] = defaultdict(float)
    per_bot_driver: defaultdict[str, Counter] = defaultdict(Counter)
    for s in spans:
        attrs = s.get("attributes") or {}
        bot = s.get("bot_id") or "?"
        cost = s.get("total_cost") or 0
        driver = attrs.get("cascade.tier_chosen_by") or "<null>"
        per_bot[bot] += 1
        per_bot_cost[bot] += cost
        per_driver[driver] += 1
        per_driver_cost[driver] += cost
        per_bot_driver[bot][driver] += 1
    return {
        "total_count": total_count,
        "total_cost": total_cost,
        "per_bot": per_bot,
        "per_bot_cost": dict(per_bot_cost),
        "per_driver": per_driver,
        "per_driver_cost": dict(per_driver_cost),
        "per_bot_driver": {k: dict(v) for k, v in per_bot_driver.items()},
    }


def _print_summary(s: dict, *, window_str: str) -> None:
    print(f"# tier1 usage report — {window_str}")
    print(f"# turns:  {s['total_count']:>5,}")
    print(f"# cost:   ${s['total_cost']:>5,.2f}")
    if s["total_count"] == 0:
        print(f"# (no tier1 turns in window — system is well-conserved)")
        return
    print(f"")
    print(f"## by driver  (chosen_by tag on the span)")
    # Print in canonical order so the eye can spot anomalies; trailing
    # any drivers we don't recognize (future enum additions, typos).
    seen = set()
    for driver in _DRIVER_ORDER:
        n = s["per_driver"].get(driver, 0)
        if n == 0:
            continue
        cost = s["per_driver_cost"].get(driver, 0)
        pct = 100 * n / s["total_count"]
        cost_pct = 100 * cost / s["total_cost"] if s["total_cost"] else 0
        print(
            f"  {driver:<22}  {n:>4,} turns ({pct:>4.1f}%)  "
            f"${cost:>6,.2f} ({cost_pct:>4.1f}%)"
        )
        seen.add(driver)
    for driver, n in s["per_driver"].most_common():
        if driver in seen:
            continue
        cost = s["per_driver_cost"].get(driver, 0)
        pct = 100 * n / s["total_count"]
        cost_pct = 100 * cost / s["total_cost"] if s["total_cost"] else 0
        print(
            f"  {driver:<22}  {n:>4,} turns ({pct:>4.1f}%)  "
            f"${cost:>6,.2f} ({cost_pct:>4.1f}%)  [unknown driver]"
        )
    # Spotter: 100% "default" driver across multiple turns smells of
    # the 2026-06-08 attribution bug. Flag it.
    default_n = s["per_driver"].get("default", 0)
    if default_n >= 3 and default_n == s["total_count"]:
        print(f"")
        print(
            f"  ⚠  All tier1 turns are tagged chosen_by='default'. This is "
            f"the signature of the 2026-06-08 attribution bug (PR #2384). "
            f"With the fix deployed, real drivers (user_request, preflight, "
            f"cascade, ...) should appear here. Persistent 100% 'default' "
            f"after deploy means the fix isn't live yet."
        )
    print(f"")
    print(f"## by bot")
    for bot, n in s["per_bot"].most_common():
        cost = s["per_bot_cost"].get(bot, 0)
        # Per-bot driver mix — helps spot single-driver concentration
        mix = s["per_bot_driver"].get(bot, {})
        mix_str = ", ".join(
            f"{drv}:{cnt}" for drv, cnt in sorted(mix.items(), key=lambda x: -x[1])
        )
        print(f"  {bot:<14}  {n:>4,} turns   ${cost:>6,.2f}   ({mix_str})")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python3 -m cascade.audit_tier1_usage",
        description=(
            "Report every tier1 (opus) turn on the pod over the audit window, "
            "with chosen_by + preflight context for each. Useful for "
            "diagnosing cost-class incidents and verifying the attribution "
            "layer is working post-deploy."
        ),
    )
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod-wide shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--days", type=int, default=14,
        help="Window size in days (default: 14)",
    )
    p.add_argument(
        "--bot", default=None,
        help="Filter to a single bot ID (default: all bots)",
    )
    p.add_argument(
        "--summary", action="store_true",
        help="Omit the per-turn detail table; print summary only",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    shared = Path(args.shared_dir)
    if not shared.is_dir():
        print(f"ERROR: shared-dir does not exist: {shared}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    spans = _load_tier1_spans(shared, since=since, until=now, bot_id=args.bot)
    # Order detail by start_time ascending — easier to scan for time clusters
    spans.sort(key=lambda s: s.get("start_time") or "")

    bot_str = f" bot={args.bot}" if args.bot else ""
    window_str = f"last {args.days} days{bot_str}"

    if not args.summary and spans:
        print(f"# Per-turn detail ({len(spans)} tier1 turns)")
        for span in spans:
            print(_format_detail_row(span))
        print(f"")

    summary = _summarize(spans)
    _print_summary(summary, window_str=window_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
