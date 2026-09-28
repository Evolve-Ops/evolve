"""cascade.discovery — baseline-distribution report for the anomaly detector.

**2026-05-27 redesign.** The previous draft of this script produced a
"cap-trip flags" report against fixed Phase 3 default density caps. The
operator's design pivot dropped those caps in favor of an anomaly
detector with origin-aware forbearance (spec § 2.6). This rewrite
produces what the anomaly detector actually needs: per-bot baseline
distributions across the dimensions the detector watches.

Per internal/spec-tier-cascade-2026-05-26.md § 7 Phase 2 (revised):

  Discovery script v2 produces baseline-distribution report — per-bot
  mean/p50/p95/max for cost-per-turn, context-tokens, sessions-per-day,
  tier-mix to seed the anomaly baseline.

Output is human-readable + JSON (machine-readable). Run before Phase 3
to validate the anomaly detector has enough baseline data on each bot
to compute meaningful thresholds, and to spot bots whose distributions
look unusual enough that the operator may want to set a custom budget.

Usage:
    python3 -m cascade.discovery --shared-dir /Users/Shared/evolve \\
                                  --bots team_bot_a,admin_bot --days 30
    python3 -m cascade.discovery --shared-dir /Users/Shared/evolve \\
                                  --all-bots --days 30 --format json

Note: this reads PRE-CASCADE annotation data (turn_annotation records
that have existed for months). Once Phase 1 cascade telemetry data
accumulates, a Phase 4 successor will read cascade span data with
trigger_kind / cost / context-token attribution as first-class fields.
This v2 uses session_class as a proxy for trigger_kind.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median, quantiles
from typing import Iterable

from observations.access import ObservationWindow


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot distribution report
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DistributionStats:
    """Five-number summary plus mean for a single metric across sessions/turns."""

    n: int = 0
    mean: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    max: float = 0.0


def _stats(values: list[float]) -> DistributionStats:
    """Compute distribution stats from a list of floats. Robust to small inputs."""
    if not values:
        return DistributionStats()
    n = len(values)
    if n == 1:
        v = float(values[0])
        return DistributionStats(n=1, mean=v, p50=v, p95=v, max=v)
    sorted_vals = sorted(values)
    try:
        cuts = quantiles(sorted_vals, n=20, method="inclusive")
        p95 = float(cuts[18])
    except (ValueError, IndexError):
        p95 = float(sorted_vals[-1])
    return DistributionStats(
        n=n,
        mean=float(mean(values)),
        p50=float(median(sorted_vals)),
        p95=p95,
        max=float(sorted_vals[-1]),
    )


@dataclass
class BotBaselineReport:
    """One row per bot. The shape the anomaly detector consumes."""

    bot_id: str
    sessions: int = 0
    turns: int = 0
    notes: str = ""

    # Per-turn distributions (what cascade Phase 1 telemetry will measure directly).
    cost_per_turn: DistributionStats = field(default_factory=DistributionStats)

    # Per-session distributions.
    cost_per_session: DistributionStats = field(default_factory=DistributionStats)
    turns_per_session: DistributionStats = field(default_factory=DistributionStats)
    duration_min_per_session: DistributionStats = field(default_factory=DistributionStats)

    # Per-day distribution (operational tempo).
    sessions_per_day: DistributionStats = field(default_factory=DistributionStats)

    # Stratified by session-class proxy for trigger_kind (until Phase 1 data lands).
    user_facing_share: float = 0.0       # fraction of sessions classified productive/ambiguous
    background_share: float = 0.0        # fraction classified maintenance
    user_facing_cost_share: float = 0.0  # fraction of $ spent on user-facing sessions
    background_cost_share: float = 0.0   # fraction of $ spent on background sessions

    # Cost rollup.
    total_cost_usd: float = 0.0


def _parse_iso(s: object) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def baseline_for_bot(
    bot_id: str,
    shared_dir: Path,
    *,
    days: int = 30,
    end: datetime | None = None,
) -> BotBaselineReport:
    """Compute one bot's baseline distributions over the lookback window.

    Reads via ObservationWindow.turn_annotations(). Groups by session_id
    so per-session metrics can be derived; per-turn metrics come from
    raw annotation records.
    """
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    window = ObservationWindow(bot_id=bot_id, start=start, end=end, shared_dir=shared_dir)

    sessions: dict[str, list[dict]] = defaultdict(list)
    cost_per_turn_values: list[float] = []
    for ann in window.turn_annotations():
        cost = ann.get("cost_estimated")
        if isinstance(cost, (int, float)):
            cost_per_turn_values.append(float(cost))
        sid = ann.get("session_id")
        if not sid:
            continue
        sessions[sid].append(ann)

    if not sessions:
        return BotBaselineReport(bot_id=bot_id, notes=f"no annotations in last {days} days")

    # Per-session aggregates.
    cost_per_session: list[float] = []
    turns_per_session: list[float] = []
    duration_min_per_session: list[float] = []
    user_facing_session_count = 0
    background_session_count = 0
    user_facing_cost_total = 0.0
    background_cost_total = 0.0
    total_cost = 0.0
    sessions_by_day: dict[str, int] = defaultdict(int)

    for sid, anns in sessions.items():
        # Turn count.
        turns_per_session.append(float(len(anns)))

        # Wall-clock from earliest to latest ts.
        timestamps = [_parse_iso(a.get("ts")) for a in anns]
        timestamps = [t for t in timestamps if t is not None]
        if len(timestamps) >= 2:
            duration = (max(timestamps) - min(timestamps)).total_seconds() / 60.0
            duration_min_per_session.append(duration)
        else:
            duration_min_per_session.append(0.0)

        # Cost.
        cost = 0.0
        for a in anns:
            c = a.get("cost_estimated")
            if isinstance(c, (int, float)):
                cost += float(c)
        cost_per_session.append(cost)
        total_cost += cost

        # Session-class proxy for trigger_kind.
        is_background = any(a.get("session_class") == "maintenance" for a in anns)
        if is_background:
            background_session_count += 1
            background_cost_total += cost
        else:
            user_facing_session_count += 1
            user_facing_cost_total += cost

        # Sessions-per-day (by start date in UTC).
        if timestamps:
            day = min(timestamps).date().isoformat()
            sessions_by_day[day] += 1

    total_sessions = len(sessions)
    sessions_per_day_values = [float(c) for c in sessions_by_day.values()]

    return BotBaselineReport(
        bot_id=bot_id,
        sessions=total_sessions,
        turns=sum(int(c) for c in turns_per_session),
        cost_per_turn=_stats(cost_per_turn_values),
        cost_per_session=_stats(cost_per_session),
        turns_per_session=_stats(turns_per_session),
        duration_min_per_session=_stats(duration_min_per_session),
        sessions_per_day=_stats(sessions_per_day_values),
        user_facing_share=user_facing_session_count / total_sessions,
        background_share=background_session_count / total_sessions,
        user_facing_cost_share=(user_facing_cost_total / total_cost) if total_cost > 0 else 0.0,
        background_cost_share=(background_cost_total / total_cost) if total_cost > 0 else 0.0,
        total_cost_usd=total_cost,
    )


def baseline_for_pod(
    bots: Iterable[str],
    shared_dir: Path,
    *,
    days: int = 30,
    end: datetime | None = None,
) -> list[BotBaselineReport]:
    """Run baseline computation for every bot, in input order.

    ``end`` anchors the lookback window for every bot (defaults to now,
    resolved per-bot in baseline_for_bot).
    """
    return [baseline_for_bot(bot, shared_dir, days=days, end=end) for bot in bots]


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_dist(d: DistributionStats, unit: str = "", precision: int = 2) -> str:
    """Compact distribution summary: n=X mean=Y p50=Z p95=W max=V."""
    if d.n == 0:
        return "(no data)"
    f = f"{{:.{precision}f}}"
    return (
        f"n={d.n} "
        f"mean={f.format(d.mean)}{unit} "
        f"p50={f.format(d.p50)}{unit} "
        f"p95={f.format(d.p95)}{unit} "
        f"max={f.format(d.max)}{unit}"
    )


def render_text(reports: list[BotBaselineReport], days: int) -> str:
    """Human-readable text report. Per-bot baseline + pod-level summary."""
    lines: list[str] = []
    lines.append(f"Cascade anomaly-detector baseline — last {days} days")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("Per-bot distributions (proxy for trigger_kind via session_class until Phase 1 data lands)")
    lines.append("=" * 100)

    pod_total_cost = 0.0
    bots_with_data = 0

    for r in reports:
        lines.append(f"\n{r.bot_id} — {r.sessions} sessions, {r.turns} turns, ${r.total_cost_usd:.2f} total")
        if r.notes:
            lines.append(f"  ({r.notes})")
            continue
        bots_with_data += 1
        pod_total_cost += r.total_cost_usd

        lines.append(f"  cost/turn:      {_fmt_dist(r.cost_per_turn, '$', 4)}")
        lines.append(f"  cost/session:   {_fmt_dist(r.cost_per_session, '$', 3)}")
        lines.append(f"  turns/session:  {_fmt_dist(r.turns_per_session, '', 1)}")
        lines.append(f"  duration/session: {_fmt_dist(r.duration_min_per_session, 'min', 1)}")
        lines.append(f"  sessions/day:   {_fmt_dist(r.sessions_per_day, '', 1)}")
        lines.append(
            f"  origin mix: user-facing {r.user_facing_share*100:.0f}% sessions "
            f"({r.user_facing_cost_share*100:.0f}% of $), "
            f"background {r.background_share*100:.0f}% sessions "
            f"({r.background_cost_share*100:.0f}% of $)"
        )

        # Highlight baseline-sufficiency for anomaly detector.
        # Per spec § 2.6: min_baseline_observations = 50 by default.
        if r.cost_per_turn.n < 50:
            lines.append(
                f"  ⚠ baseline sparse — only {r.cost_per_turn.n} turns. "
                f"Anomaly detector will use pod-median bootstrap for ≥14 days."
            )

    lines.append("")
    lines.append("=" * 100)
    lines.append(
        f"Summary: {bots_with_data}/{len(reports)} bots have data; "
        f"pod total spend over window = ${pod_total_cost:.2f}"
    )

    # Sparse-bot reminder.
    sparse_bots = [r.bot_id for r in reports if r.sessions > 0 and r.cost_per_turn.n < 50]
    if sparse_bots:
        lines.append("")
        lines.append(
            f"Bots with sparse baseline (<50 turns): {', '.join(sparse_bots)}. "
            "Phase 2 anomaly detector will bootstrap from pod-median for these."
        )

    lines.append("")
    return "\n".join(lines)


def render_json(reports: list[BotBaselineReport], days: int) -> str:
    """Machine-readable JSON. Suitable for piping into baseline-seeding tooling."""
    return json.dumps(
        {
            "schema": "cascade.baseline_distribution_report.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback_days": days,
            "reports": [asdict(r) for r in reports],
        },
        indent=2,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _list_bots_from_network(shared_dir: Path) -> list[str]:
    """Read pod bot list from network.json (authoritative per
    feedback_explicit_pod_membership)."""
    network_path = shared_dir / "network.json"
    if not network_path.exists():
        return []
    try:
        raw = json.loads(network_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pod = raw.get("pod") or {}
    bots = pod.get("bots") or []
    out: list[str] = []
    for b in bots:
        if isinstance(b, dict) and "id" in b:
            out.append(b["id"])
        elif isinstance(b, str):
            out.append(b)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
        help="Shared directory root (default: /Users/Shared/evolve)",
    )
    bot_group = parser.add_mutually_exclusive_group(required=True)
    bot_group.add_argument(
        "--bots",
        help="Comma-separated bot ids (e.g. team_bot_a,admin_bot)",
    )
    bot_group.add_argument(
        "--all-bots",
        action="store_true",
        help="Discover from every bot listed in network.json",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback window in days (default: 30)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text)",
    )
    args = parser.parse_args(argv)

    if args.all_bots:
        bots = _list_bots_from_network(args.shared_dir)
        if not bots:
            print(
                f"error: --all-bots requires {args.shared_dir}/network.json with pod.bots[]",
                file=sys.stderr,
            )
            return 2
    else:
        bots = [b.strip() for b in args.bots.split(",") if b.strip()]

    reports = baseline_for_pod(bots, args.shared_dir, days=args.days)

    if args.format == "json":
        print(render_json(reports, args.days))
    else:
        print(render_text(reports, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
