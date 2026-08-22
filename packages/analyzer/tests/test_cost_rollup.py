"""Tests for cost_rollup — per-bot daily aggregator + atomic writes.

Pins:
  1. Aggregation math (totals, per-model breakdown, missing/empty model name).
  2. Both source files are read (cost_events-{date}.jsonl + legacy {date}.jsonl
     filtered to type==cost_event).
  3. Idempotency — re-running overwrites without appending.
  4. No-event days do NOT produce a rollup file. Budget Hawk's spend_reader
     treats missing files as "no data", which is the correct semantic for
     inactive bots and pre-deployment dates.
  5. Backfill range — refresh_all walks N trailing days inclusive of today.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_rollup as cr  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────


def _ev(model="claude-sonnet-4-6", **kw):
    base = {
        "schema_version": 1,
        "type": "cost_event",
        "ts": "2026-05-05T00:07:00Z",
        "model": model,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    base.update(kw)
    return base


def test_aggregate_sums_totals_and_tokens():
    events = [
        _ev(cost_usd=0.10, input_tokens=10, output_tokens=20,
            cache_read_tokens=100, cache_write_tokens=5),
        _ev(cost_usd=0.25, input_tokens=5, output_tokens=80,
            cache_read_tokens=200, cache_write_tokens=15),
    ]
    agg = cr.aggregate(events)
    assert agg["total_usd"] == pytest.approx(0.35)
    assert agg["input_tokens"] == 15
    assert agg["output_tokens"] == 100
    assert agg["cache_read_tokens"] == 300
    assert agg["cache_write_tokens"] == 20
    assert agg["event_count"] == 2


def test_aggregate_per_model_breakdown():
    events = [
        _ev(model="claude-sonnet-4-6", cost_usd=1.0, input_tokens=10),
        _ev(model="claude-haiku-4-5", cost_usd=0.05, input_tokens=2),
        _ev(model="claude-sonnet-4-6", cost_usd=0.5, input_tokens=20),
    ]
    agg = cr.aggregate(events)
    assert set(agg["by_model"].keys()) == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    sonnet = agg["by_model"]["claude-sonnet-4-6"]
    assert sonnet["cost_usd"] == pytest.approx(1.5)
    assert sonnet["event_count"] == 2
    assert sonnet["input_tokens"] == 30
    haiku = agg["by_model"]["claude-haiku-4-5"]
    assert haiku["cost_usd"] == pytest.approx(0.05)
    assert haiku["event_count"] == 1


def test_aggregate_missing_model_groups_under_unknown():
    events = [
        _ev(model=None, cost_usd=0.1),
        _ev(model="", cost_usd=0.2),
    ]
    agg = cr.aggregate(events)
    # Both empty/None models collapse to "unknown" — keeps the JSON key
    # space sane and avoids an empty-string key in by_model.
    assert "unknown" in agg["by_model"]
    assert agg["by_model"]["unknown"]["event_count"] == 2
    assert agg["by_model"]["unknown"]["cost_usd"] == pytest.approx(0.3)


def test_aggregate_empty():
    agg = cr.aggregate([])
    assert agg["total_usd"] == 0.0
    assert agg["event_count"] == 0
    assert agg["by_model"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# Reads from both sources
# ─────────────────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_iter_cost_events_reads_converter_file(tmp_path):
    bot, d = "admin_bot", date(2026, 5, 5)
    _write_jsonl(
        tmp_path / "annotations" / bot / f"cost_events-{d}.jsonl",
        [_ev(cost_usd=0.5), _ev(cost_usd=0.25)],
    )
    events = list(cr.iter_cost_events(tmp_path, bot, d))
    assert len(events) == 2


def test_iter_cost_events_reads_legacy_filtered_to_cost_event(tmp_path):
    bot, d = "admin_bot", date(2026, 4, 20)
    _write_jsonl(
        tmp_path / "annotations" / bot / f"{d}.jsonl",
        [
            _ev(cost_usd=0.1),
            {"type": "turn_annotation", "cost": 999},  # filtered out
            _ev(cost_usd=0.2),
        ],
    )
    events = list(cr.iter_cost_events(tmp_path, bot, d))
    assert len(events) == 2
    assert all(e.get("type") == "cost_event" for e in events)


def test_iter_cost_events_reads_both_sources(tmp_path):
    bot, d = "admin_bot", date(2026, 4, 21)
    _write_jsonl(
        tmp_path / "annotations" / bot / f"cost_events-{d}.jsonl",
        [_ev(cost_usd=0.5)],
    )
    _write_jsonl(
        tmp_path / "annotations" / bot / f"{d}.jsonl",
        [_ev(cost_usd=0.1), {"type": "turn_annotation"}],
    )
    events = list(cr.iter_cost_events(tmp_path, bot, d))
    assert len(events) == 2


def test_iter_cost_events_missing_files_yields_nothing(tmp_path):
    assert list(cr.iter_cost_events(tmp_path, "personal_bot", date(2026, 5, 5))) == []


# ─────────────────────────────────────────────────────────────────────────────
# write_rollup
# ─────────────────────────────────────────────────────────────────────────────


def test_write_rollup_creates_metrics_file(tmp_path):
    bot, d = "admin_bot", date(2026, 5, 5)
    _write_jsonl(
        tmp_path / "annotations" / bot / f"cost_events-{d}.jsonl",
        [_ev(cost_usd=0.5, input_tokens=4, output_tokens=89,
             cache_read_tokens=29427, cache_write_tokens=0,
             model="claude-sonnet-4-6")],
    )
    result = cr.write_rollup(tmp_path, bot, d, now=datetime(2026, 5, 5, 22, 0, tzinfo=timezone.utc))
    assert result is not None
    out_path = tmp_path / "metrics" / bot / f"cost-{d}.json"
    assert out_path.exists()
    parsed = json.loads(out_path.read_text())
    assert parsed["bot_id"] == bot
    assert parsed["date"] == "2026-05-05"
    assert parsed["total_usd"] == pytest.approx(0.5)
    assert parsed["input_tokens"] == 4
    assert parsed["generated_at"].endswith("Z")
    assert parsed["schema_version"] == cr.COST_ROLLUP_SCHEMA_VERSION
    assert "claude-sonnet-4-6" in parsed["by_model"]


def test_write_rollup_returns_none_when_no_events(tmp_path):
    # No annotations dir at all — should NOT create a metrics file.
    result = cr.write_rollup(tmp_path, "personal_bot", date(2026, 5, 5))
    assert result is None
    assert not (tmp_path / "metrics" / "personal_bot").exists()


def test_write_rollup_is_idempotent(tmp_path):
    bot, d = "admin_bot", date(2026, 5, 5)
    _write_jsonl(
        tmp_path / "annotations" / bot / f"cost_events-{d}.jsonl",
        [_ev(cost_usd=0.5)],
    )
    out_path = tmp_path / "metrics" / bot / f"cost-{d}.json"
    cr.write_rollup(tmp_path, bot, d)
    first = out_path.read_text()
    cr.write_rollup(tmp_path, bot, d)
    second = out_path.read_text()
    # Generated_at differs, but totals must match — and the file was overwritten,
    # not appended (still parses as a single JSON object).
    parsed = json.loads(second)
    assert parsed["total_usd"] == pytest.approx(0.5)
    assert parsed["event_count"] == 1


def test_write_rollup_overwrites_after_new_events(tmp_path):
    bot, d = "admin_bot", date(2026, 5, 5)
    src = tmp_path / "annotations" / bot / f"cost_events-{d}.jsonl"
    _write_jsonl(src, [_ev(cost_usd=0.5)])
    cr.write_rollup(tmp_path, bot, d)
    # Add another event and re-run; the file should reflect the new total.
    _write_jsonl(src, [_ev(cost_usd=0.5), _ev(cost_usd=0.25)])
    cr.write_rollup(tmp_path, bot, d)
    parsed = json.loads(
        (tmp_path / "metrics" / bot / f"cost-{d}.json").read_text()
    )
    assert parsed["total_usd"] == pytest.approx(0.75)
    assert parsed["event_count"] == 2


def test_write_rollup_ignores_malformed_lines(tmp_path):
    bot, d = "admin_bot", date(2026, 5, 5)
    p = tmp_path / "annotations" / bot / f"cost_events-{d}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_ev(cost_usd=0.5)) + "\n"
        + "{not valid json\n"
        + json.dumps(_ev(cost_usd=0.25)) + "\n"
    )
    result = cr.write_rollup(tmp_path, bot, d)
    assert result is not None
    assert result["event_count"] == 2
    assert result["total_usd"] == pytest.approx(0.75)


# ─────────────────────────────────────────────────────────────────────────────
# refresh_all
# ─────────────────────────────────────────────────────────────────────────────


def test_refresh_all_walks_trailing_days_per_bot(tmp_path):
    today = date(2026, 5, 5)
    # admin_bot: events on day 0 and day 2
    for d in (today, today.replace(day=3)):
        _write_jsonl(
            tmp_path / "annotations" / "admin_bot" / f"cost_events-{d}.jsonl",
            [_ev(cost_usd=0.5)],
        )
    # personal_bot: nothing
    results = cr.refresh_all(
        tmp_path, ["admin_bot", "personal_bot"], days=5, today=today
    )
    # 2 bots × 5 days = 10 attempts
    assert len(results) == 10
    written = [(b, d) for b, d, r in results if r is not None]
    assert (("admin_bot", date(2026, 5, 5)) in written)
    assert (("admin_bot", date(2026, 5, 3)) in written)
    # personal_bot has no events on any day → no writes
    assert all(b != "personal_bot" for b, _, _ in [(b, d, r) for b, d, r in results if r is not None])


def test_refresh_all_skips_writes_for_no_event_days(tmp_path):
    today = date(2026, 5, 5)
    cr.refresh_all(tmp_path, ["personal_bot"], days=3, today=today)
    # No events, no rollup files.
    assert not (tmp_path / "metrics" / "personal_bot").exists() or \
        list((tmp_path / "metrics" / "personal_bot").glob("cost-*.json")) == []


def test_refresh_all_isolates_per_bot_failures(tmp_path, monkeypatch):
    """One bot's broken state (e.g. permission error on its metrics
    dir, the personal_bot regression of 2026-05-07) must NOT abort the rest
    of the pass. Before this fix, the exception bubbled out of the
    per-bot loop and silently killed every bot iterated after the
    failing one — for 10 days nobody saw the cascade because the
    outer ``better_engine_refresh`` ``try`` caught it as "non-fatal".
    """
    today = date(2026, 5, 5)
    # All three bots have events on today's date.
    for bot in ("admin_bot", "personal_bot", "security_bot"):
        _write_jsonl(
            tmp_path / "annotations" / bot / f"cost_events-{today}.jsonl",
            [_ev(cost_usd=0.5)],
        )

    # Simulate personal_bot's write failing every time.
    real_write = cr.write_rollup

    def _flaky_write(shared_dir, bot_id, target_date, *args, **kwargs):
        if bot_id == "personal_bot":
            raise PermissionError(
                "[Errno 13] Permission denied: simulated personal_bot dir owner"
            )
        return real_write(shared_dir, bot_id, target_date, *args, **kwargs)

    monkeypatch.setattr(cr, "write_rollup", _flaky_write)

    logs: list[str] = []
    results = cr.refresh_all(
        tmp_path, ["admin_bot", "personal_bot", "security_bot"], days=1, today=today,
        log_fn=logs.append,
    )

    # 3 bots × 1 day = 3 result tuples regardless of failures.
    assert len(results) == 3

    written = [(b, d) for b, d, r in results if r is not None]
    # admin_bot AND security_bot both wrote — security_bot iterates AFTER the failing
    # bot (personal_bot), so this proves the failure didn't kill the loop.
    assert ("admin_bot", today) in written
    assert ("security_bot", today) in written
    # personal_bot returned None (write skipped after the exception).
    assert ("personal_bot", today) not in written

    # Failure was logged with bot context so operators can spot the
    # culprit from logs / alerts, not just by reading every bot's
    # metrics dir.
    assert any("personal_bot" in line and "PermissionError" in line for line in logs)


def test_refresh_all_log_fn_defaults_to_stderr(tmp_path, monkeypatch, capsys):
    """When the caller doesn't pass log_fn, failures still surface —
    just on stderr instead of through the caller's logger. Ensures
    the CLI path stays informative without forcing every caller to
    wire up a logger."""
    today = date(2026, 5, 5)
    _write_jsonl(
        tmp_path / "annotations" / "personal_bot" / f"cost_events-{today}.jsonl",
        [_ev(cost_usd=0.5)],
    )

    def _always_fail(*a, **kw):
        raise PermissionError("boom")

    monkeypatch.setattr(cr, "write_rollup", _always_fail)

    cr.refresh_all(tmp_path, ["personal_bot"], days=1, today=today)
    captured = capsys.readouterr()
    assert "personal_bot" in captured.err
    assert "PermissionError" in captured.err
