"""tests/test_thread_stats.py — proxy.read_thread_stats + format_session_context turn-N line.

Covers the #1402 follow-up fix for the "fresh session" confabulation:

  * ``read_thread_stats(session_id)`` reads OC's session jsonl and
    returns ``{age_seconds, user_turn_count}`` so the route handler
    can wire those values into the session-context block.

  * ``format_session_context`` renders a "This is turn N of an
    ongoing thread, started Xm ago. Scroll back …" line when
    ``user_turn_count >= 1``. This is the in-prompt signal that
    closes the bare-follow-up case (operator says "C" or "Option B"
    referring to a list evo rendered in a prior turn — without the
    explicit turn-N line, the model treats turn 2 as turn 1 and
    disavows "fresh session").

Both halves are tested here because the two pieces are useless apart:
``read_thread_stats`` produces the numbers, ``format_session_context``
turns them into the prompt line. The route handler in between is
plumbing — ``test_evo_proxy``-style coverage already pins the dict
shape that flows through.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo import proxy as P  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# read_thread_stats — reading OC's session jsonl
# ─────────────────────────────────────────────────────────────────────────────


def test_read_thread_stats_missing_file_returns_zeros(tmp_path):
    """First turn on a fresh thread — no session jsonl exists yet.
    Caller wires the values straight into session_ctx, so the function
    must return zero defaults rather than None / raise."""
    stats = P.read_thread_stats("nonexistent", sessions_dir=tmp_path)
    assert stats == {"age_seconds": 0, "user_turn_count": 0}


def test_read_thread_stats_empty_session_id_returns_zeros(tmp_path):
    """Defensive — an empty session_id can't map to any file."""
    stats = P.read_thread_stats("", sessions_dir=tmp_path)
    assert stats == {"age_seconds": 0, "user_turn_count": 0}


def _write_jsonl(tmp_path: Path, session_id: str, *records: dict) -> Path:
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_read_thread_stats_counts_user_turns(tmp_path):
    """``user_turn_count`` is the load-bearing field — it gates the
    "this is turn N of an ongoing thread" line in the session-context
    block. Counts ``message`` records with ``role == "user"``;
    assistant + toolResult records don't bump the count."""
    now_ms = int(time.time() * 1000)
    _write_jsonl(
        tmp_path, "test-1",
        # First user turn
        {"type": "message", "message": {
            "role": "user", "timestamp": now_ms - 240_000,  # 4m ago
            "content": [{"type": "text", "text": "hello"}],
        }},
        # Assistant reply — doesn't count
        {"type": "message", "message": {
            "role": "assistant", "timestamp": now_ms - 230_000,
            "content": [{"type": "text", "text": "hi"}],
        }},
        # Tool result — doesn't count
        {"type": "message", "message": {
            "role": "toolResult", "timestamp": now_ms - 220_000,
            "content": [],
        }},
        # Second user turn
        {"type": "message", "message": {
            "role": "user", "timestamp": now_ms - 60_000,  # 1m ago
            "content": [{"type": "text", "text": "follow-up"}],
        }},
    )
    stats = P.read_thread_stats("test-1", sessions_dir=tmp_path)
    assert stats["user_turn_count"] == 2
    # age_seconds anchored on the FIRST message (any role) — the
    # thread started when the first message landed, not when the
    # latest user turn did.
    assert 230 <= stats["age_seconds"] <= 260, (
        f"age_seconds = {stats['age_seconds']}; expected ~240s (4m ago)"
    )


def test_read_thread_stats_age_from_first_message(tmp_path):
    """The age clock anchors on the FIRST parseable message timestamp,
    regardless of role. The model's mental model of "how long have we
    been talking" is from the start, not from the most recent turn."""
    now_ms = int(time.time() * 1000)
    _write_jsonl(
        tmp_path, "test-2",
        # Assistant message first (synthetic — but the code shouldn't
        # care about role for age purposes)
        {"type": "message", "message": {
            "role": "assistant", "timestamp": now_ms - 600_000,  # 10m ago
            "content": [],
        }},
        {"type": "message", "message": {
            "role": "user", "timestamp": now_ms - 60_000,
            "content": [],
        }},
    )
    stats = P.read_thread_stats("test-2", sessions_dir=tmp_path)
    # Tolerate a few seconds of slack for test execution time.
    assert 590 <= stats["age_seconds"] <= 620
    assert stats["user_turn_count"] == 1


def test_read_thread_stats_skips_non_message_records(tmp_path):
    """OC's JSONL also carries ``type: custom`` and other envelope
    shapes — only ``type: message`` records contribute to the count
    or the age."""
    now_ms = int(time.time() * 1000)
    _write_jsonl(
        tmp_path, "test-3",
        # Non-message record — must be skipped entirely.
        {"type": "custom", "data": {"timestamp": now_ms - 999_999}},
        {"type": "message", "message": {
            "role": "user", "timestamp": now_ms - 60_000,
            "content": [],
        }},
    )
    stats = P.read_thread_stats("test-3", sessions_dir=tmp_path)
    assert stats["user_turn_count"] == 1
    # Age must come from the message record, not the custom record.
    assert 55 <= stats["age_seconds"] <= 75


def test_read_thread_stats_handles_malformed_json(tmp_path):
    """A truncated OC session file should degrade gracefully — count
    the parseable records, ignore the bad lines."""
    path = tmp_path / "test-4.jsonl"
    now_ms = int(time.time() * 1000)
    good = json.dumps({
        "type": "message",
        "message": {"role": "user", "timestamp": now_ms - 60_000,
                    "content": []},
    })
    path.write_text(good + "\n" + "{not valid json\n" + good + "\n")
    stats = P.read_thread_stats("test-4", sessions_dir=tmp_path)
    assert stats["user_turn_count"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# format_session_context — turn-N line
# ─────────────────────────────────────────────────────────────────────────────


def test_session_context_renders_turn_n_line_when_user_turn_count_ge_1():
    """The load-bearing case: when at least one prior user turn is on
    record, the block must include an explicit "this is turn N of an
    ongoing thread … scroll back" instruction. That's the in-prompt
    lever telling the model to use the replayed history rather than
    disavow it as a "fresh session"."""
    block = P.format_session_context({
        "user_turn_count": 2,           # 2 prior turns → current is turn 3
        "session_age_seconds": 240,     # 4m
    })
    # Turn number is user_turn_count + 1 — the route handler reads
    # the JSONL BEFORE OC writes the current turn's user message,
    # so the count covers prior turns only.
    assert "turn 3" in block, (
        f"Expected 'turn 3' in block; got:\n{block}"
    )
    assert "ongoing thread" in block.lower()
    assert "4m" in block
    # The "scroll back" phrase is load-bearing — the diagnosis (#1402)
    # specifies it explicitly. Don't paraphrase it away.
    assert "scroll back" in block.lower(), (
        "The 'Scroll back …' instruction is the explicit signal that "
        "tells the model its prior turns are in context. Don't drop it."
    )


def test_session_context_omits_turn_n_line_on_turn_1():
    """When ``user_turn_count == 0`` (first turn — no prior user
    messages on disk), the block must NOT render the turn-N line.
    Turn 1 really is a fresh session and the model treating it as
    such is correct behavior."""
    block = P.format_session_context({
        "user_turn_count": 0,
        "session_age_seconds": 0,
    })
    assert "ongoing thread" not in block.lower()
    assert "turn 1" not in block.lower()
    assert "scroll back" not in block.lower()


def test_session_context_turn_n_without_age_still_renders():
    """If for some reason the age couldn't be computed (eg a session
    file without any parseable timestamps) but the user-turn count
    is known, the "this is turn N" instruction still renders — just
    without the "started Xm ago" anchor. The teaching is still
    useful: it tells the model the thread is continuous."""
    block = P.format_session_context({
        "user_turn_count": 1,
        "session_age_seconds": 0,
    })
    assert "turn 2" in block
    assert "scroll back" in block.lower()


def test_session_context_user_turn_count_missing_falls_back_to_legacy():
    """Backwards compatibility — when ``user_turn_count`` is absent
    (eg a caller that pre-dates the #1402 follow-up), the old
    ``session_age_seconds > 0`` path still renders "this chat thread
    is N old" so we don't silently drop the temporal anchor."""
    block = P.format_session_context({
        "session_age_seconds": 720,
    })
    assert "12m old" in block
    # The new-style line is absent because user_turn_count isn't set.
    assert "ongoing thread" not in block.lower()
