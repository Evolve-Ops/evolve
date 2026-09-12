"""Tests for the transcript_reader closure inside _make_security_warden_ctx.

The reader is the on-disk contract security_warden depends on. The
RecentTranscriptCapture plugin writes the file; this reader filters by
lookback and drops malformed entries before observe() sees them.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generator_runner import _make_security_warden_ctx  # noqa: E402


def _write_buffer(shared_dir: Path, bot_id: str, entries: list) -> Path:
    metrics = shared_dir / "metrics" / bot_id
    metrics.mkdir(parents=True, exist_ok=True)
    fp = metrics / "recent-transcripts.json"
    fp.write_text(json.dumps(entries))
    return fp


def _ctx(shared_dir: Path, bot_id: str, now: datetime):
    return _make_security_warden_ctx(shared_dir, {}, bot_id, {}, now)


def test_reader_returns_empty_when_file_missing(tmp_path):
    ctx = _ctx(tmp_path, "admin_bot", datetime.now(timezone.utc))
    assert ctx.transcript_reader("admin_bot", 24) == []


def test_reader_returns_empty_for_malformed_json(tmp_path):
    metrics = tmp_path / "metrics" / "admin_bot"
    metrics.mkdir(parents=True)
    (metrics / "recent-transcripts.json").write_text("not-json")
    ctx = _ctx(tmp_path, "admin_bot", datetime.now(timezone.utc))
    assert ctx.transcript_reader("admin_bot", 24) == []


def test_reader_returns_empty_when_top_level_not_a_list(tmp_path):
    _write_buffer(tmp_path, "admin_bot", {"oops": "object-not-list"})  # type: ignore[arg-type]
    ctx = _ctx(tmp_path, "admin_bot", datetime.now(timezone.utc))
    assert ctx.transcript_reader("admin_bot", 24) == []


def test_reader_filters_by_lookback_hours(tmp_path):
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        # 2 hours old — kept under any sane lookback
        {"session_id": "s1", "turn_index": 1, "ts": (now - timedelta(hours=2)).isoformat(), "text": "recent"},
        # 30 hours old — kept under default 24h? no. kept under 48h yes.
        {"session_id": "s2", "turn_index": 1, "ts": (now - timedelta(hours=30)).isoformat(), "text": "older"},
        # 100 hours old — always dropped under either default
        {"session_id": "s3", "turn_index": 1, "ts": (now - timedelta(hours=100)).isoformat(), "text": "ancient"},
    ]
    _write_buffer(tmp_path, "admin_bot", entries)

    ctx = _ctx(tmp_path, "admin_bot", now)

    out_24 = ctx.transcript_reader("admin_bot", 24)
    assert [e["session_id"] for e in out_24] == ["s1"]

    out_48 = ctx.transcript_reader("admin_bot", 48)
    assert sorted(e["session_id"] for e in out_48) == ["s1", "s2"]

    out_999 = ctx.transcript_reader("admin_bot", 999)
    assert sorted(e["session_id"] for e in out_999) == ["s1", "s2", "s3"]


def test_reader_skips_entries_with_missing_or_bad_ts(tmp_path):
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        {"session_id": "good", "turn_index": 1, "ts": now.isoformat(), "text": "ok"},
        {"session_id": "no-ts", "turn_index": 1, "text": "no timestamp at all"},
        {"session_id": "bad-ts", "turn_index": 1, "ts": "not a date", "text": "garbage"},
        "completely-not-a-dict",  # sneaks past Python typing
    ]
    _write_buffer(tmp_path, "admin_bot", entries)

    ctx = _ctx(tmp_path, "admin_bot", now)
    out = ctx.transcript_reader("admin_bot", 24)
    assert [e["session_id"] for e in out] == ["good"]


def test_reader_handles_z_suffix_iso(tmp_path):
    """JS Date#toISOString emits ...Z; Python fromisoformat needs +00:00 in <3.11."""
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    z_ts = now.isoformat().replace("+00:00", "Z")
    _write_buffer(
        tmp_path,
        "admin_bot",
        [{"session_id": "z", "turn_index": 1, "ts": z_ts, "text": "from JS"}],
    )
    ctx = _ctx(tmp_path, "admin_bot", now)
    out = ctx.transcript_reader("admin_bot", 24)
    assert len(out) == 1 and out[0]["session_id"] == "z"


def test_factory_returns_none_when_bot_id_is_none(tmp_path):
    """security_warden is per-bot; pod-wide invocation should be a no-op."""
    ctx = _make_security_warden_ctx(tmp_path, {}, None, {}, datetime.now(timezone.utc))
    assert ctx is None


def test_reader_isolates_per_bot(tmp_path):
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    _write_buffer(tmp_path, "admin_bot", [{"session_id": "admin_bot-1", "turn_index": 1, "ts": now.isoformat(), "text": "admin_bot msg"}])
    _write_buffer(tmp_path, "team_bot_a",   [{"session_id": "team_bot_a-1",   "turn_index": 1, "ts": now.isoformat(), "text": "team_bot_a msg"}])

    ctx = _ctx(tmp_path, "admin_bot", now)
    admin_bot_out = ctx.transcript_reader("admin_bot", 24)
    team_bot_a_out = ctx.transcript_reader("team_bot_a", 24)
    assert [e["session_id"] for e in admin_bot_out] == ["admin_bot-1"]
    assert [e["session_id"] for e in team_bot_a_out] == ["team_bot_a-1"]
