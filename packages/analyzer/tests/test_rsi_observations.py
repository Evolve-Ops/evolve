"""tests/test_rsi_observations.py — Observation access layer (L1 shape)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from observations import TupleAccessNotYetAvailable, window  # noqa: E402


def _seed_annotations(shared_dir: Path, bot_id: str, date_iso: str, records: list):
    annotations_dir = shared_dir / "annotations" / bot_id
    annotations_dir.mkdir(parents=True, exist_ok=True)
    path = annotations_dir / f"{date_iso}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_tuples_yields_empty_when_no_data(tmp_path):
    """L3: tuples() no longer raises; it just returns an empty iterator when
    the bot has no tuple files yet."""
    w = window("team_bot_a", days=1, shared_dir=tmp_path)
    assert list(w.tuples()) == []


def test_clusters_yields_empty_view_when_no_data(tmp_path):
    """L3: clusters() returns an empty ClusterView when there are no tuples."""
    w = window("team_bot_a", days=1, shared_dir=tmp_path)
    view = w.clusters()
    assert len(view) == 0


def test_window_with_no_data_returns_empty(tmp_path):
    w = window("team_bot_a", days=7, shared_dir=tmp_path)
    assert list(w.session_summaries()) == []
    assert list(w.turn_annotations()) == []


def _seed_cost_events_file(shared_dir: Path, bot_id: str, date_iso: str, records: list):
    """Write to the converter's sibling cost_events-<date>.jsonl path."""
    annotations_dir = shared_dir / "annotations" / bot_id
    annotations_dir.mkdir(parents=True, exist_ok=True)
    path = annotations_dir / f"cost_events-{date_iso}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_cost_events_reads_from_converter_sibling_file(tmp_path):
    """Records written by cost_event_converter to ``cost_events-<date>.jsonl``
    must surface through ``window.cost_events()``."""
    today = datetime.now(timezone.utc)
    _seed_cost_events_file(
        tmp_path, "team_bot_a", today.date().isoformat(),
        [{
            "schema_version": 1,
            "type": "cost_event",
            "ts": today.isoformat(),
            "bot_id": "team_bot_a",
            "session_id": "abc",
            "trigger_kind": "user_turn",
            "model": "claude-sonnet-4-6",
            "provider": "anthropic",
            "cache_state": "warm",
            "input_tokens": 10,
            "output_tokens": 100,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 0,
            "cost_usd": 0.001,
        }],
    )
    events = list(window("team_bot_a", days=1, shared_dir=tmp_path).cost_events())
    assert len(events) == 1
    assert events[0]["session_id"] == "abc"


def test_cost_events_merges_converter_and_legacy_sources(tmp_path):
    """Some historical records sit in the legacy ``{date}.jsonl`` alongside
    other annotation types; the converter writes new ones to a sibling
    file. ``cost_events()`` must surface both, regardless of which file
    they live in. This test pins the contract that the migration is
    transparent to readers."""
    today = datetime.now(timezone.utc)
    iso = today.date().isoformat()

    # Legacy: cost_event mixed with a non-cost_event row in the canonical file.
    _seed_annotations(tmp_path, "team_bot_a", iso, [
        {
            "type": "cost_event",
            "ts": today.isoformat(),
            "bot_id": "team_bot_a",
            "session_id": "old-session",
            "input_tokens": 1, "output_tokens": 1,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_usd": 0.0001,
        },
        {"type": "turn_annotation", "session_id": "unrelated"},
    ])

    # New: converter-written sibling file.
    _seed_cost_events_file(tmp_path, "team_bot_a", iso, [
        {
            "schema_version": 1,
            "type": "cost_event",
            "ts": today.isoformat(),
            "bot_id": "team_bot_a",
            "session_id": "new-session",
            "input_tokens": 5, "output_tokens": 5,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_usd": 0.0005,
        },
    ])

    events = list(window("team_bot_a", days=1, shared_dir=tmp_path).cost_events())
    sessions = sorted(e["session_id"] for e in events)
    assert sessions == ["new-session", "old-session"]


def test_session_summaries_streams_from_jsonl(tmp_path):
    today = datetime.now(timezone.utc)
    _seed_annotations(
        tmp_path,
        "team_bot_a",
        today.date().isoformat(),
        [
            {"type": "session_summary", "session_id": "s1", "outcome": "ok"},
            {"type": "turn_annotation", "turn_id": "t1"},
            {"type": "session_summary", "session_id": "s2", "outcome": "meh"},
        ],
    )

    w = window("team_bot_a", days=1, shared_dir=tmp_path)
    summaries = list(w.session_summaries())
    assert len(summaries) == 2
    assert {s["session_id"] for s in summaries} == {"s1", "s2"}

    turns = list(w.turn_annotations())
    assert len(turns) == 1


def test_window_filters_by_timestamp_when_present(tmp_path):
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    in_window = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    out_of_window = (now - timedelta(days=10)).isoformat(timespec="seconds")

    _seed_annotations(
        tmp_path,
        "team_bot_a",
        now.date().isoformat(),
        [
            {"type": "session_summary", "session_id": "in_s", "session_end": in_window},
            {
                "type": "session_summary",
                "session_id": "out_s",
                "session_end": out_of_window,
            },
        ],
    )

    w = window(
        "team_bot_a",
        start=now - timedelta(days=1),
        end=now + timedelta(minutes=1),
        shared_dir=tmp_path,
    )
    ids = {s["session_id"] for s in w.session_summaries()}
    assert "in_s" in ids
    assert "out_s" not in ids


def test_window_filters_by_ts_key(tmp_path):
    """``ts`` is the canonical timestamp key written by every current
    producer (turn_annotation, session_summary, cost_event). Records whose
    ``ts`` falls outside the window must be excluded — historically the
    candidate list omitted ``ts`` so the record-level filter was a no-op
    and boundary-day files leaked up to ~24h of out-of-window records."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    in_window = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    out_of_window = (now - timedelta(days=10)).isoformat(timespec="seconds")

    _seed_annotations(
        tmp_path,
        "team_bot_a",
        now.date().isoformat(),
        [
            {"type": "turn_annotation", "turn_id": "in_t", "ts": in_window},
            {"type": "turn_annotation", "turn_id": "out_t", "ts": out_of_window},
            {"type": "session_summary", "session_id": "in_s", "ts": in_window},
            {"type": "session_summary", "session_id": "out_s", "ts": out_of_window},
        ],
    )

    w = window(
        "team_bot_a",
        start=now - timedelta(days=1),
        end=now + timedelta(minutes=1),
        shared_dir=tmp_path,
    )
    turn_ids = {t["turn_id"] for t in w.turn_annotations()}
    assert turn_ids == {"in_t"}
    session_ids = {s["session_id"] for s in w.session_summaries()}
    assert session_ids == {"in_s"}


def test_window_excludes_same_day_records_before_window_start(tmp_path):
    """The boundary-day leak (PR #2624): file_paths() includes the whole
    start-day file, so records earlier that same day must be cut by the
    record-level ``ts`` filter, not just the per-day file range."""
    now = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    start = now - timedelta(hours=6)  # window starts mid-day at 12:00
    early_same_day = (now - timedelta(hours=10)).isoformat(timespec="seconds")
    inside = (now - timedelta(hours=2)).isoformat(timespec="seconds")

    _seed_annotations(
        tmp_path,
        "team_bot_a",
        now.date().isoformat(),
        [
            {"type": "turn_annotation", "turn_id": "early", "ts": early_same_day},
            {"type": "turn_annotation", "turn_id": "inside", "ts": inside},
        ],
    )

    w = window("team_bot_a", start=start, end=now, shared_dir=tmp_path)
    assert {t["turn_id"] for t in w.turn_annotations()} == {"inside"}


def test_window_skips_malformed_jsonl_lines(tmp_path):
    annotations_dir = tmp_path / "annotations" / "team_bot_a"
    annotations_dir.mkdir(parents=True)
    path = annotations_dir / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"
    path.write_text('{"type":"session_summary","session_id":"ok"}\nnot-json-at-all\n')

    w = window("team_bot_a", days=1, shared_dir=tmp_path)
    ids = [s["session_id"] for s in w.session_summaries()]
    assert ids == ["ok"]


def test_window_requires_days_or_explicit_range(tmp_path):
    with pytest.raises(ValueError):
        window("team_bot_a", shared_dir=tmp_path)


def test_window_file_paths_enumerates_date_range(tmp_path):
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 3, tzinfo=timezone.utc)
    for d in ["2026-05-01", "2026-05-02", "2026-05-03"]:
        _seed_annotations(tmp_path, "team_bot_a", d, [{"type": "session_summary"}])

    w = window("team_bot_a", start=start, end=end, shared_dir=tmp_path)
    paths = w.file_paths()
    assert len(paths) == 3
