"""Tests for turn_detail — per-turn drilldown for the Turn Audit drawer.

Pins:

  1. Annotation lookup walks day files newest → oldest, finds the matching
     turn_id, and returns the full record.
  2. cost_event join filters by (session_id + ts proximity) and survives
     malformed JSONL lines.
  3. Transcript probing finds files at one of several plausible
     ``.openclaw/`` paths and degrades gracefully when nothing matches.
  4. Redaction scrubs credential-looking strings before returning text.
  5. Per-section truncation flags ``truncated=True`` past MAX_SECTION_BYTES.
  6. Anthropic-style tool_use / tool_result blocks are paired and counted.
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

import turn_detail as td  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_layout(tmp_path: Path, *, with_transcript: bool = True) -> tuple[Path, Path, str, str]:
    """Build a fake shared+home layout with one turn worth of records."""
    bot = "testbot"
    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    ann_dir = tmp_path / "annotations" / bot
    ann_dir.mkdir(parents=True)

    annotation = {
        "type": "turn_annotation",
        "turn_id": "turn-abc",
        "session_id": "sess-xyz",
        "ts": ts,
        "bot_id": bot,
        "session_class": "productive",
        "model_selected": "claude-sonnet-4-6",
        "input_tokens": 12000,
        "output_tokens": 1500,
        "cache_read_tokens": 35000,
        "cache_write_tokens": 8000,
        "cost_estimated": 0.084,
    }
    with open(ann_dir / f"{today}.jsonl", "w") as f:
        # Mix in a session_summary record and a malformed line — the lookup
        # must skip past both without bailing out.
        f.write(json.dumps({"type": "session_summary", "session_id": "sess-xyz"}) + "\n")
        f.write("not-json\n")
        f.write(json.dumps(annotation) + "\n")

    cost_event = {
        "type": "cost_event",
        "ts": ts,
        "bot_id": bot,
        "session_id": "sess-xyz",
        "trigger_kind": "user_turn",
        "cache_state": "warm",
        "model": "claude-sonnet-4-6",
        "input_tokens": 12000,
        "output_tokens": 1500,
        "cache_read_tokens": 35000,
        "cache_write_tokens": 8000,
        "cost_usd": 0.084,
    }
    with open(ann_dir / f"cost_events-{today}.jsonl", "w") as f:
        f.write("malformed\n")
        f.write(json.dumps(cost_event) + "\n")
        # A different session that must NOT match
        other = dict(cost_event, session_id="sess-other", trigger_kind="heartbeat")
        f.write(json.dumps(other) + "\n")

    home = tmp_path / "home"
    if with_transcript:
        sess_dir = home / ".openclaw" / "agents" / "main" / "agent" / "sessions" / "sess-xyz"
        sess_dir.mkdir(parents=True)
        msgs = [
            {"role": "system", "content": "API key: sk-ant-aaaaaaaaaaaaaaaaaaaa"},
            {"role": "user", "turn_id": "turn-abc", "content": "Weather please"},
            {
                "role": "assistant",
                "turn_id": "turn-abc",
                "content": [
                    {"type": "text", "text": "Checking…"},
                    {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {"city": "NYC"}},
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "72F sunny"},
                ],
            },
        ]
        with open(sess_dir / "transcript.jsonl", "w") as f:
            for m in msgs:
                f.write(json.dumps(m) + "\n")

    return tmp_path, home, bot, "turn-abc"


# ─────────────────────────────────────────────────────────────────────────────
# Lookup
# ─────────────────────────────────────────────────────────────────────────────


def test_get_turn_detail_full(tmp_path: Path) -> None:
    shared, home, bot, turn_id = _make_layout(tmp_path)
    result = td.get_turn_detail(shared, bot, turn_id, bot_home=home)

    assert "error" not in result
    assert result["annotation"]["turn_id"] == turn_id
    assert result["annotation"]["cost_estimated"] == pytest.approx(0.084)

    # cost_event for the right session, not the noise event
    assert len(result["cost_events"]) == 1
    assert result["cost_events"][0]["trigger_kind"] == "user_turn"


def test_get_turn_detail_missing_turn(tmp_path: Path) -> None:
    shared, home, bot, _ = _make_layout(tmp_path)
    result = td.get_turn_detail(shared, bot, "does-not-exist", bot_home=home)
    assert "error" in result


def test_get_turn_detail_no_annotations_dir(tmp_path: Path) -> None:
    # No annotations dir at all — must return error, not raise.
    result = td.get_turn_detail(tmp_path, "ghost", "turn-abc", bot_home=tmp_path)
    assert "error" in result


# ─────────────────────────────────────────────────────────────────────────────
# Transcript
# ─────────────────────────────────────────────────────────────────────────────


def test_transcript_parsing_and_redaction(tmp_path: Path) -> None:
    shared, home, bot, turn_id = _make_layout(tmp_path)
    result = td.get_turn_detail(shared, bot, turn_id, bot_home=home)

    assert result["transcript_status"] == "ok"
    tx = result["transcript"]
    # System prompt redacted
    assert "[redacted-key]" in tx["system"]["text"]
    assert "sk-ant-" not in tx["system"]["text"]
    # User and assistant slices
    assert tx["user"]["text"] == "Weather please"
    assert "Checking" in tx["assistant"]["text"]
    # Tools paired correctly
    assert tx["tools_invoked"] == 1
    assert tx["tool_summary"] == {"get_weather": 1}
    assert tx["tool_calls"][0]["tool"] == "get_weather"
    assert tx["tool_calls"][0]["result"] == "72F sunny"


def test_transcript_missing_returns_status(tmp_path: Path) -> None:
    shared, home, bot, turn_id = _make_layout(tmp_path, with_transcript=False)
    result = td.get_turn_detail(shared, bot, turn_id, bot_home=home)
    assert result["transcript"] is None
    assert result["transcript_status"] == "not_found"
    # Annotation/cost_events still present so the drawer can render
    assert result["annotation"]["turn_id"] == turn_id
    assert len(result["cost_events"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_redact_patterns() -> None:
    assert td._redact("token sk-ant-1234567890abcdefghij rest") != "token sk-ant-1234567890abcdefghij rest"
    assert "[redacted-key]" in td._redact("ghp_aaaaaaaaaaaaaaaaaaaa")
    # Bearer with trailing JWT-like value
    assert "[redacted-key]" in td._redact("Bearer aaaaaaaaaaaaaaaaaaaaaa")
    # Generic key=value
    assert "[redacted]" in td._redact("api_key=ABCDEFGHIJ12345")


def test_truncate_marks_overflow() -> None:
    big = "x" * (td.MAX_SECTION_BYTES + 100)
    out = td._truncate(big)
    assert out.endswith("…[truncated]…")
    assert len(out.encode("utf-8")) <= td.MAX_SECTION_BYTES + len("…[truncated]…".encode("utf-8")) + 1


def test_section_truncation_flag() -> None:
    sec = td._section("x" * (td.MAX_SECTION_BYTES + 50))
    assert sec["truncated"] is True
    assert sec["chars"] == td.MAX_SECTION_BYTES + 50


def test_adjacent_dates_handles_bad_input() -> None:
    today = date.today().isoformat()
    out = td._adjacent_dates("not-a-date")
    assert today in out
    assert len(out) == 3
