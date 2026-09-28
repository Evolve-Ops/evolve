"""Tests for breakers.backtest — replay harness.

We don't replay the real 90-day corpus here — that runs on the mini.
These tests verify the harness's mechanics: window iteration, file
loading, and an end-to-end smoke run against a tiny synthetic
turn-JSONL tree.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from breakers.backtest import (
    iter_turn_files,
    iter_windows,
    read_turns,
    run_backtest,
)


class TestIterWindows:
    def test_simple_hourly(self) -> None:
        start = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 20, 3, 0, tzinfo=timezone.utc)
        windows = list(iter_windows(start, end, window_hours=1, step_hours=1))
        # Windows: (0:00→1:00), (1:00→2:00), (2:00→3:00)
        assert len(windows) == 3
        assert windows[0][1] == start + timedelta(hours=1)
        assert windows[-1][1] == end

    def test_step_smaller_than_window(self) -> None:
        """4-hour windows stepping every 1 hour — overlapping."""
        start = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
        windows = list(iter_windows(start, end, window_hours=4, step_hours=1))
        # First window ends at 4:00; last ends at 8:00; 5 windows.
        assert len(windows) == 5

    def test_window_larger_than_range_yields_nothing(self) -> None:
        start = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc)
        windows = list(iter_windows(start, end, window_hours=4, step_hours=1))
        assert windows == []


class TestReadTurns:
    def _write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_reads_all_files_in_bot_dir(self, tmp_path: Path) -> None:
        shared = tmp_path / "evolve"
        bot = "team_bot_a"
        self._write_jsonl(
            shared / bot / "turns" / "turns-2026-05-19.jsonl",
            [{"ts": "2026-05-19T12:00:00Z", "source": "heartbeat"}],
        )
        self._write_jsonl(
            shared / bot / "turns" / "turns-2026-05-20.jsonl",
            [
                {"ts": "2026-05-20T10:00:00Z", "source": "heartbeat"},
                {"ts": "2026-05-20T11:00:00Z", "source": "heartbeat"},
            ],
        )
        turns = read_turns(shared, bot)
        assert len(turns) == 3

    def test_skips_corrupt_lines(self, tmp_path: Path) -> None:
        shared = tmp_path / "evolve"
        path = shared / "team_bot_a" / "turns" / "turns-2026-05-20.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"ts": "2026-05-20T10:00:00Z"}\n'
            'not-json\n'
            '\n'
            '{"ts": "2026-05-20T11:00:00Z"}\n',
            encoding="utf-8",
        )
        turns = read_turns(shared, "team_bot_a")
        assert len(turns) == 2

    def test_missing_bot_dir_returns_empty(self, tmp_path: Path) -> None:
        assert read_turns(tmp_path, "nonexistent") == []

    def test_filename_date_filtering(self, tmp_path: Path) -> None:
        shared = tmp_path / "evolve"
        # Files: 03-01, 05-19, 05-20. Filter to >= 05-15.
        for date in ("2026-03-01", "2026-05-19", "2026-05-20"):
            self._write_jsonl(
                shared / "team_bot_a" / "turns" / f"turns-{date}.jsonl",
                [{"ts": f"{date}T12:00:00Z", "source": "heartbeat"}],
            )
        since = datetime(2026, 5, 15, tzinfo=timezone.utc)
        turns = read_turns(shared, "team_bot_a", since=since)
        # 03-01 should be filtered out by filename gate;
        # 05-19 and 05-20 should remain.
        tss = sorted(t["ts"] for t in turns)
        assert tss == ["2026-05-19T12:00:00Z", "2026-05-20T12:00:00Z"]


class TestIterTurnFiles:
    def test_returns_sorted(self, tmp_path: Path) -> None:
        shared = tmp_path / "evolve"
        bot_dir = shared / "team_bot_a" / "turns"
        bot_dir.mkdir(parents=True)
        for date in ("2026-05-20", "2026-05-18", "2026-05-19"):
            (bot_dir / f"turns-{date}.jsonl").write_text("")
        files = list(iter_turn_files(shared, "team_bot_a"))
        assert [f.name for f in files] == [
            "turns-2026-05-18.jsonl",
            "turns-2026-05-19.jsonl",
            "turns-2026-05-20.jsonl",
        ]


class TestRunBacktestSmoke:
    def _make_turn(self, ts: datetime, source: str, model: str) -> dict:
        return {
            "ts": ts.isoformat().replace("+00:00", "Z"),
            "source": source,
            "channel": "heartbeat" if source == "heartbeat" else "telegram",
            "model": model,
        }

    def test_end_to_end_detects_synthetic_spike(self, tmp_path: Path) -> None:
        """Construct a tiny turn tree with a clear spike. Verify the
        harness reports a trip in the spike window."""
        shared = tmp_path / "evolve"
        bot = "security_bot"
        bot_dir = shared / bot / "turns"
        bot_dir.mkdir(parents=True)

        # 10 days of baseline: 1 haiku heartbeat/hour.
        baseline_records: list[dict] = []
        for day_offset in range(10, 1, -1):  # 10..2 days ago
            day = datetime(2026, 5, 21, tzinfo=timezone.utc) - timedelta(days=day_offset)
            for hour in range(24):
                baseline_records.append(self._make_turn(
                    day.replace(hour=hour, minute=5),
                    "heartbeat",
                    "anthropic/claude-haiku-4-5",
                ))

        # Spike day: 2026-05-20 between 12:00 and 13:00, 30 sonnet heartbeats.
        spike_records: list[dict] = []
        spike_day = datetime(2026, 5, 20, tzinfo=timezone.utc)
        for i in range(30):
            spike_records.append(self._make_turn(
                spike_day.replace(hour=12, minute=0) + timedelta(minutes=i * 2),
                "heartbeat",
                "anthropic/claude-sonnet-4-6",
            ))

        # Group by date and write to per-day files.
        from collections import defaultdict
        by_day: dict[str, list[dict]] = defaultdict(list)
        for r in baseline_records + spike_records:
            by_day[r["ts"][:10]].append(r)
        for d, recs in by_day.items():
            with (bot_dir / f"turns-{d}.jsonl").open("w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")

        records, summary = run_backtest(
            shared_dir=shared,
            bots=[bot],
            eval_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            eval_end=datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc),
            window_hours=1,
            step_hours=1,
        )
        assert summary.total_trips >= 1
        # The trip window should include 12:00 → 13:00.
        trip_ends = [r.window_end for r in records]
        assert any("12:" in t or "13:" in t for t in trip_ends), (
            f"expected trip around the 12:00-13:00 spike window; "
            f"got trip windows: {trip_ends}"
        )

    def test_no_trips_when_no_data(self, tmp_path: Path) -> None:
        records, summary = run_backtest(
            shared_dir=tmp_path,
            bots=["ghost"],
            eval_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            eval_end=datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc),
        )
        assert records == []
        assert summary.total_trips == 0
        assert summary.trips_per_bot["ghost"] == 0
