"""tests/test_rsi_l3_extract_session_summary.py — L3 driver coverage.

Tests the session_summary → ObservationTuple bridge and the per-bot
driver script (extract_tuples.py). These cover:

* ``is_trivial_session_summary`` filtering
* ``extract_from_session_summary`` end-to-end with a stub extractor
* The driver's per-bot extraction (annotation file → tuples on disk)
* The driver's source_hash dedupe (a re-run is a no-op)
* The driver's per-run quota cap

The driver is exercised by importing ``extract_for_bot`` directly so we
don't need to spin up a subprocess.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from extract_tuples import extract_for_bot  # noqa: E402
from observations.extract import (  # noqa: E402
    extract_from_session_summary,
    is_trivial_session_summary,
    render_session_summary_text,
    reset_extractor,
    session_summary_to_transcript,
    set_extractor,
)
from observations.tuples import read_tuples_range  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


_DAY = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _summary(
    session_id: str = "s1",
    bot_id: str = "team_bot_a",
    *,
    outcome: str = "Drafted reply to landlord; user accepted.",
    apps: list[str] | None = None,
    turn_count: int = 4,
    complexity: str = "low",
    correction_count: int = 0,
    efficiency_flag: bool = False,
    ts: str | None = None,
    session_class: str = "productive",
) -> dict:
    return {
        "type": "session_summary",
        "session_id": session_id,
        "bot_id": bot_id,
        "ts": ts or _DAY.isoformat(),
        "outcome": outcome,
        "applications_invoked": apps if apps is not None else ["email"],
        "promises_made": [],
        "complexity": complexity,
        "session_class": session_class,
        "tier": session_class,
        "tier_confidence": 0.7,
        "turn_count": turn_count,
        "correction_count": correction_count,
        "efficiency_flag": efficiency_flag,
        "first_response_resolution": True,
        "total_input_tokens": 100,
        "total_output_tokens": 200,
    }


def _stub_extractor(noun: str = "email", verb: str = "drafting", mood: str | None = "neutral", confidence: float = 0.85):
    """Build a fake LLM extractor that returns a single canned tuple."""
    def fake(transcript, vocabulary):  # noqa: ARG001
        return [
            {
                "noun": noun,
                "verb": verb,
                "mood": mood,
                "engagement": 3,
                "segment_id": "seg-0",
                "confidence": confidence,
            }
        ]
    return fake


def _silent_extractor(transcript, vocabulary):  # noqa: ARG001
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Trivial-session filtering
# ─────────────────────────────────────────────────────────────────────────────


def test_heartbeat_is_trivial():
    rec = _summary(outcome="HEARTBEAT_OK", apps=[], turn_count=1)
    assert is_trivial_session_summary(rec) is True


def test_one_turn_no_apps_is_trivial():
    rec = _summary(outcome="ok", apps=[], turn_count=1)
    assert is_trivial_session_summary(rec) is True


def test_normal_session_is_not_trivial():
    rec = _summary()
    assert is_trivial_session_summary(rec) is False


def test_one_turn_with_apps_is_not_trivial():
    rec = _summary(outcome="Logged a 5-mile run.", apps=["fitness-tracker"], turn_count=1)
    assert is_trivial_session_summary(rec) is False


# ─────────────────────────────────────────────────────────────────────────────
# render_session_summary_text + transcript synthesis
# ─────────────────────────────────────────────────────────────────────────────


def test_render_includes_outcome_and_apps():
    rec = _summary()
    text = render_session_summary_text(rec)
    assert "Outcome" in text
    assert "email" in text


def test_session_summary_to_transcript_returns_none_when_trivial():
    assert session_summary_to_transcript(_summary(outcome="HEARTBEAT_OK", apps=[], turn_count=1)) is None


def test_session_summary_to_transcript_uses_record_timestamp():
    rec = _summary(ts="2026-05-01T12:34:56+00:00")
    t = session_summary_to_transcript(rec)
    assert t is not None
    assert t.turns[0].timestamp == "2026-05-01T12:34:56+00:00"


# ─────────────────────────────────────────────────────────────────────────────
# extract_from_session_summary with a stub extractor
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_from_session_summary_returns_a_tuple():
    set_extractor(_stub_extractor())
    try:
        rec = _summary()
        tuples = extract_from_session_summary(rec)
        assert len(tuples) == 1
        t = tuples[0]
        assert t.noun == "email"
        assert t.verb == "drafting"
        assert t.mood == "neutral"
        assert t.bot_id == "team_bot_a"
        assert t.session_id == "s1"
    finally:
        reset_extractor()


def test_extract_from_session_summary_pins_timestamp_from_record():
    set_extractor(_stub_extractor())
    try:
        rec = _summary(ts="2026-05-01T09:00:00+00:00")
        tuples = extract_from_session_summary(rec)
        # The stub returns no timestamp; the bridge fills it from the record
        assert tuples[0].timestamp_start == "2026-05-01T09:00:00+00:00"
        assert tuples[0].timestamp_end == "2026-05-01T09:00:00+00:00"
    finally:
        reset_extractor()


def test_extract_from_session_summary_returns_empty_for_trivial():
    set_extractor(_stub_extractor())
    try:
        rec = _summary(outcome="HEARTBEAT_OK", apps=[], turn_count=1)
        tuples = extract_from_session_summary(rec)
        assert tuples == []
    finally:
        reset_extractor()


# ─────────────────────────────────────────────────────────────────────────────
# Driver — extract_for_bot
# ─────────────────────────────────────────────────────────────────────────────


def _seed_annotations(tmp_path: Path, bot_id: str, summaries: list[dict]) -> None:
    base = tmp_path / "annotations" / bot_id
    base.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict]] = {}
    for rec in summaries:
        day_iso = (rec.get("ts") or _DAY.isoformat())[:10]
        by_day.setdefault(day_iso, []).append(rec)
    for day_iso, recs in by_day.items():
        path = base / f"{day_iso}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")


def test_driver_extracts_and_writes_tuples(tmp_path):
    set_extractor(_stub_extractor())
    try:
        # Seed two sessions for team_bot_a, one for today, one trivial
        today = datetime.now(timezone.utc)
        ts1 = today.isoformat()
        ts2 = today.isoformat()
        _seed_annotations(
            tmp_path,
            "team_bot_a",
            [
                _summary(session_id="s1", ts=ts1, outcome="Drafted reply.", apps=["email"]),
                _summary(session_id="s2", ts=ts2, outcome="HEARTBEAT_OK", apps=[], turn_count=1),
            ],
        )
        stats = extract_for_bot(
            "team_bot_a",
            shared_dir=tmp_path,
            days=2,
            max_sessions=10,
        )
        assert stats["summaries"] == 2
        assert stats["skipped_trivial"] == 1
        assert stats["extracted"] == 1
        assert stats["tuples_written"] == 1
        # Verify tuple is on disk and readable
        loaded = list(
            read_tuples_range(
                tmp_path,
                "team_bot_a",
                start=today - timedelta(days=2),
                end=today + timedelta(days=1),
            )
        )
        assert len(loaded) == 1
        assert loaded[0].noun == "email"
        assert loaded[0].verb == "drafting"
    finally:
        reset_extractor()


def test_driver_dedupes_on_rerun(tmp_path):
    set_extractor(_stub_extractor())
    try:
        today = datetime.now(timezone.utc)
        _seed_annotations(
            tmp_path,
            "team_bot_a",
            [_summary(session_id="s1", ts=today.isoformat(), outcome="Drafted reply.", apps=["email"])],
        )
        first = extract_for_bot("team_bot_a", shared_dir=tmp_path, days=2, max_sessions=10)
        assert first["extracted"] == 1

        # Re-run: every summary should be skipped via source_hash dedup,
        # zero LLM calls, zero new tuples on disk.
        second = extract_for_bot("team_bot_a", shared_dir=tmp_path, days=2, max_sessions=10)
        assert second["skipped_dedup"] == 1
        assert second["extracted"] == 0
        assert second["tuples_written"] == 0
    finally:
        reset_extractor()


def test_driver_respects_max_sessions_quota(tmp_path):
    set_extractor(_stub_extractor())
    try:
        today = datetime.now(timezone.utc)
        # Seed five non-trivial sessions
        recs = [
            _summary(session_id=f"s{i}", ts=today.isoformat(), outcome=f"Drafted reply {i}.", apps=["email"])
            for i in range(5)
        ]
        _seed_annotations(tmp_path, "team_bot_a", recs)
        stats = extract_for_bot("team_bot_a", shared_dir=tmp_path, days=2, max_sessions=2)
        assert stats["extracted"] == 2
        assert stats["quota_hit"] is True
    finally:
        reset_extractor()


def test_driver_dry_run_makes_no_writes(tmp_path):
    set_extractor(_stub_extractor())
    try:
        today = datetime.now(timezone.utc)
        _seed_annotations(
            tmp_path,
            "team_bot_a",
            [_summary(session_id="s1", ts=today.isoformat(), outcome="Drafted reply.", apps=["email"])],
        )
        stats = extract_for_bot("team_bot_a", shared_dir=tmp_path, days=2, max_sessions=10, dry_run=True)
        assert stats["extracted"] == 1
        assert stats["tuples_written"] == 0
        # No observations file written
        obs_dir = tmp_path / "observations"
        assert not obs_dir.exists() or not list(obs_dir.glob("**/*.jsonl"))
    finally:
        reset_extractor()


def test_driver_handles_silent_extractor(tmp_path):
    """If the LLM returns no tuples, the driver still records the call count."""
    set_extractor(_silent_extractor)
    try:
        today = datetime.now(timezone.utc)
        _seed_annotations(
            tmp_path,
            "team_bot_a",
            [_summary(session_id="s1", ts=today.isoformat(), outcome="Drafted reply.", apps=["email"])],
        )
        stats = extract_for_bot("team_bot_a", shared_dir=tmp_path, days=2, max_sessions=10)
        assert stats["extracted"] == 1
        assert stats["tuples_written"] == 0
    finally:
        reset_extractor()


# ─────────────────────────────────────────────────────────────────────────────
# llm_extractor._parse_response tolerance
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_response_handles_pure_json():
    from observations.llm_extractor import _parse_response

    text = '{"tuples": [{"noun": "email", "verb": "drafting"}]}'
    out = _parse_response(text)
    assert out == [{"noun": "email", "verb": "drafting"}]


def test_parse_response_handles_markdown_fences():
    from observations.llm_extractor import _parse_response

    text = '```json\n{"tuples": [{"noun": "email", "verb": "drafting"}]}\n```'
    out = _parse_response(text)
    assert out == [{"noun": "email", "verb": "drafting"}]


def test_parse_response_handles_trailing_prose():
    from observations.llm_extractor import _parse_response

    text = '{"tuples": []}\n\n**Rationale:** the session was a heartbeat ping.'
    out = _parse_response(text)
    assert out == []


def test_parse_response_handles_fences_plus_trailing_prose():
    """The most common Haiku failure mode: fenced JSON plus an explanation."""
    from observations.llm_extractor import _parse_response

    text = (
        '```json\n{"tuples": [{"noun": "calendar", "verb": "scheduling"}]}\n```\n\n'
        '**Rationale:** scheduling a meeting.'
    )
    out = _parse_response(text)
    assert out == [{"noun": "calendar", "verb": "scheduling"}]


def test_parse_response_returns_empty_on_garbage():
    from observations.llm_extractor import _parse_response

    assert _parse_response("not json at all") == []
    assert _parse_response("") == []


def test_driver_survives_extractor_exception(tmp_path):
    def boom(transcript, vocabulary):  # noqa: ARG001
        raise RuntimeError("synthetic failure")

    set_extractor(boom)
    try:
        today = datetime.now(timezone.utc)
        _seed_annotations(
            tmp_path,
            "team_bot_a",
            [
                _summary(session_id="s1", ts=today.isoformat(), outcome="Drafted reply.", apps=["email"]),
                _summary(session_id="s2", ts=today.isoformat(), outcome="Logged a run.", apps=["fitness"]),
            ],
        )
        stats = extract_for_bot("team_bot_a", shared_dir=tmp_path, days=2, max_sessions=10)
        # Both sessions counted as extracted (call attempts), but no tuples written
        assert stats["extracted"] == 2
        assert stats["tuples_written"] == 0
    finally:
        reset_extractor()
