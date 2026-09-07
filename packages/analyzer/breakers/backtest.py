"""breakers.backtest — replay turn JSONLs to validate the detector.

Per spec §7, the detector cannot ship until it passes a backtest
against the 90-day audit corpus. This module is the calibration tool.

Usage (run on the mini where turn data lives):

    python3 -m breakers.backtest \\
        --shared-dir /Users/Shared/evolve \\
        --since 2026-02-20 \\
        --until 2026-05-21 \\
        --bot team_bot_a --bot admin_bot --bot security_bot --bot team_bot_c --bot team_bot_b \\
        --window-hours 1 --step-hours 1 \\
        --output /tmp/breakers-backtest.json

What it does:
  1. For each (bot, window_end) point in the eval range, compute the
     bot's baseline as of window_end.
  2. Evaluate the candidate window with the detector.
  3. Emit one record per trip-decision (and a summary at the end).
  4. Optionally cross-reference against a known-incident list to
     compute recall and false-positive counts.

What it does NOT do:
  - Write trip state. This is observe-only. Phase 5 wires up actual
    state writes.
  - Mutate any bot's gateway or config. Read-only against turn JSONLs.

Turn data layout, per CLAUDE.md and the cost-anomaly audit:

    /Users/Shared/evolve/<bot>/turns/turns-YYYY-MM-DD.jsonl

Older bots also have a workspace-memory fallback at
``/Users/<bot>/.openclaw/workspace/memory/turns-*.jsonl``; this
module reads the canonical `/Users/Shared/evolve/<bot>/turns/`
path. The backtest is intended to run *on the mini* where both
locations exist; cross-host data sync is out of scope.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from breakers.baseline import compute_baseline
from breakers.detector import DEFAULT_CONFIG, DetectorConfig, evaluate_window


@dataclass
class BacktestRecord:
    """One window's outcome."""

    bot_id: str
    window_start: str  # ISO-8601 string for JSON serialization
    window_end: str
    trip: bool
    reason: str
    metrics: dict


@dataclass
class BacktestSummary:
    """Roll-up across the full run."""

    bots: list[str]
    eval_start: str
    eval_end: str
    window_hours: float
    step_hours: float
    total_evaluations: int
    total_trips: int
    trips_per_bot: dict[str, int] = field(default_factory=dict)


def iter_turn_files(shared_dir: Path, bot: str) -> Iterable[Path]:
    """Yield every turns-*.jsonl file for ``bot`` under ``shared_dir``."""
    bot_dir = shared_dir / bot / "turns"
    if not bot_dir.is_dir():
        return
    yield from sorted(bot_dir.glob("turns-*.jsonl"))


def read_turns(
    shared_dir: Path,
    bot: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Read all turn records for ``bot`` in [since, until)."""
    out: list[dict] = []
    for fp in iter_turn_files(shared_dir, bot):
        # Filename date is the day the file covers. Allow a 1-day pad
        # on either side of the [since, until) window to capture turns
        # that landed in a neighboring day's file due to time-zone
        # edges. We still filter precisely by ts below.
        try:
            file_date = datetime.strptime(fp.stem.removeprefix("turns-"), "%Y-%m-%d")
            file_date = file_date.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if since and file_date < since - timedelta(days=1):
            continue
        if until and file_date > until + timedelta(days=1):
            continue

        try:
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    out.append(rec)
        except OSError:
            continue
    return out


def iter_windows(
    eval_start: datetime,
    eval_end: datetime,
    window_hours: float,
    step_hours: float,
) -> Iterable[tuple[datetime, datetime]]:
    """Yield (start, end) tuples stepping through the eval range."""
    win = timedelta(hours=window_hours)
    step = timedelta(hours=step_hours)
    cur_end = eval_start + win
    while cur_end <= eval_end:
        yield cur_end - win, cur_end
        cur_end += step


def run_backtest(
    *,
    shared_dir: Path,
    bots: list[str],
    eval_start: datetime,
    eval_end: datetime,
    window_hours: float = 1.0,
    step_hours: float = 1.0,
    config: DetectorConfig = DEFAULT_CONFIG,
    baseline_load_days: int = 14,
) -> tuple[list[BacktestRecord], BacktestSummary]:
    """Run the detector over every window in [eval_start, eval_end) per bot.

    Returns (per-trip records, summary). Only trips are included in the
    per-trip records list; full per-window data would balloon the
    output for marginal gain. The summary tracks total evaluations.

    ``baseline_load_days`` is how far back to pre-load turns so the
    7-day baseline at each window has data to compute. Default 14
    days gives a comfortable buffer.
    """
    records: list[BacktestRecord] = []
    summary = BacktestSummary(
        bots=list(bots),
        eval_start=eval_start.isoformat(),
        eval_end=eval_end.isoformat(),
        window_hours=window_hours,
        step_hours=step_hours,
        total_evaluations=0,
        total_trips=0,
    )

    for bot in bots:
        load_start = eval_start - timedelta(days=baseline_load_days)
        turns = read_turns(shared_dir, bot, since=load_start, until=eval_end)
        if not turns:
            print(f"[backtest] {bot}: no turn data in window — skipping",
                  file=sys.stderr)
            summary.trips_per_bot[bot] = 0
            continue
        print(f"[backtest] {bot}: loaded {len(turns)} turns "
              f"({load_start.date()} → {eval_end.date()})", file=sys.stderr)

        bot_trips = 0
        for win_start, win_end in iter_windows(
            eval_start, eval_end, window_hours, step_hours,
        ):
            baseline = compute_baseline(
                bot_id=bot, turns=turns, as_of=win_end,
            )
            decision = evaluate_window(
                bot_id=bot, turns=turns,
                window_start=win_start, window_end=win_end,
                baseline=baseline, config=config,
            )
            summary.total_evaluations += 1
            if decision.trip:
                bot_trips += 1
                records.append(BacktestRecord(
                    bot_id=bot,
                    window_start=decision.window_start.isoformat(),
                    window_end=decision.window_end.isoformat(),
                    trip=True,
                    reason=decision.reason,
                    metrics=decision.metrics,
                ))
        summary.trips_per_bot[bot] = bot_trips
        summary.total_trips += bot_trips
        print(f"[backtest] {bot}: {bot_trips} trip windows", file=sys.stderr)

    return records, summary


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="breakers.backtest",
        description="Replay turn JSONLs through the breaker detector. "
                    "Read-only; observe-only. See spec §7.",
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=Path("/Users/Shared/evolve"),
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    parser.add_argument(
        "--bot", action="append", dest="bots", required=True,
        help="Bot ID to evaluate (repeat to add more)",
    )
    parser.add_argument(
        "--since", type=_parse_date, required=True,
        help="Eval window start (YYYY-MM-DD, UTC)",
    )
    parser.add_argument(
        "--until", type=_parse_date, required=True,
        help="Eval window end exclusive (YYYY-MM-DD, UTC)",
    )
    parser.add_argument(
        "--window-hours", type=float, default=1.0,
        help="Length of each evaluation window in hours (default: 1.0)",
    )
    parser.add_argument(
        "--step-hours", type=float, default=1.0,
        help="Stride between windows in hours (default: 1.0)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Write JSON output here (default: stdout)",
    )
    args = parser.parse_args(argv)

    records, summary = run_backtest(
        shared_dir=args.shared_dir,
        bots=args.bots,
        eval_start=args.since,
        eval_end=args.until,
        window_hours=args.window_hours,
        step_hours=args.step_hours,
    )

    payload = {
        "summary": asdict(summary),
        "trips": [asdict(r) for r in records],
    }
    out_text = json.dumps(payload, indent=2, default=str)

    if args.output:
        args.output.write_text(out_text, encoding="utf-8")
        print(f"[backtest] wrote {args.output}", file=sys.stderr)
    else:
        print(out_text)

    print(
        f"[backtest] {summary.total_evaluations} evaluations · "
        f"{summary.total_trips} trips across {len(args.bots)} bots",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
