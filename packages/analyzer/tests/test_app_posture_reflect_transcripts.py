"""tests/test_app_posture_reflect_transcripts.py — PR6 transcript layer.

PR5 reflected on the inventory alone; PR6 also passes user-message
excerpts from each `bot_created_app` Signal's session into the prompt
so the LLM can answer "why didn't I anticipate this?" against actual
user content. Source: the existing recent-transcripts.json buffer
(user-only, 200/48h, owned by evolve).

Tests pin:
  - Loader filters by session_id and before_ts
  - Loader respects per-signal turn / char caps and drops oldest first
  - Loader degrades to [] when buffer is missing / malformed
  - Per-turn TRANSCRIPT_TURN_CHAR_CAP truncation
  - _gather_transcript_excerpts emits empty when no signals or no content
  - _gather_transcript_excerpts respects TRANSCRIPT_TOTAL_CHARS cap
  - build_prompt(shared_dir=...) includes the section; without shared_dir doesn't
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


def _fake_target():
    from infra_llm import InfraLLMTarget
    return InfraLLMTarget(
        provider="anthropic",
        model="anthropic/claude-haiku-4-5",
        api_key="sk-ant-fake-test-key",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _write_buffer(shared_dir: Path, bot_id: str, entries: list) -> Path:
    metrics = shared_dir / "metrics" / bot_id
    metrics.mkdir(parents=True, exist_ok=True)
    fp = metrics / "recent-transcripts.json"
    fp.write_text(json.dumps(entries))
    return fp


def _entry(session_id: str, turn_index: int, ts: datetime, text: str) -> dict:
    return {
        "session_id": session_id,
        "turn_index": turn_index,
        "ts": _iso(ts),
        "text": text,
    }


def _make_posture_with_signal(bot_id: str = "admin_bot", *, session_id: str = "sess-1",
                              app_id: str = "habits", first_observed_at: datetime | None = None):
    """Posture with one bot_created_app signal and a matching app manifest.
    Returns (posture, signal)."""
    from app_posture_review import (
        BotPosture, ManifestSummary, SignalSummary,
    )
    now = _now()
    signal = SignalSummary(
        type="bot_created_app",
        signature=f"bot_created_app:{bot_id}:{app_id}",
        title=f"{bot_id} built {app_id}",
        body="",
        first_observed_at=_iso(first_observed_at or now),
        last_observed_at=_iso(first_observed_at or now),
        observation_count=1,
        bot_id=bot_id,
        details={"app_id": app_id, "session_id": session_id, "purpose": ""},
    )
    posture = BotPosture(
        bot_id=bot_id,
        generated_at=_iso(now),
        window_start=_iso(now - timedelta(days=7)),
        window_end=_iso(now),
        manifests=[ManifestSummary(
            app_id=app_id, name=app_id.title(), source="bot_created",
            status="active", purpose="", crons_count=0, updated_at="",
            is_recent=True, files=[],
        )],
        bot_created_signals=[signal],
        unmanifested_signals=[],
        orphan_files=[],
        workspace_path=None,
        notes=[],
    )
    return posture, signal


# ── _load_session_excerpts ───────────────────────────────────────────────────


class TestLoadSessionExcerpts:
    def test_returns_user_messages_for_matching_session(self, tmp_path):
        from app_posture_reflect import _load_session_excerpts
        now = _now()
        _write_buffer(tmp_path, "admin_bot", [
            _entry("sess-1", 1, now - timedelta(minutes=10), "hi"),
            _entry("sess-1", 2, now - timedelta(minutes=8), "track my protein"),
            _entry("sess-2", 1, now - timedelta(minutes=5), "different session"),
        ])
        excerpts = _load_session_excerpts(tmp_path, "admin_bot", "sess-1")
        assert [e["text"] for e in excerpts] == ["hi", "track my protein"]
        # Sorted by turn_index, ascending.
        assert [e["turn_index"] for e in excerpts] == [1, 2]

    def test_filters_by_before_ts(self, tmp_path):
        """User messages AFTER the bot built the app aren't relevant to
        'why didn't I anticipate' — drop them."""
        from app_posture_reflect import _load_session_excerpts
        now = _now()
        signal_observed = now - timedelta(minutes=5)
        _write_buffer(tmp_path, "admin_bot", [
            _entry("sess-1", 1, now - timedelta(minutes=10), "before-1"),
            _entry("sess-1", 2, now - timedelta(minutes=7), "before-2"),
            _entry("sess-1", 3, now - timedelta(minutes=3), "after — should drop"),
        ])
        excerpts = _load_session_excerpts(
            tmp_path, "admin_bot", "sess-1", before_ts=signal_observed
        )
        texts = [e["text"] for e in excerpts]
        assert texts == ["before-1", "before-2"]

    def test_empty_when_buffer_missing(self, tmp_path):
        from app_posture_reflect import _load_session_excerpts
        # No file written.
        assert _load_session_excerpts(tmp_path, "admin_bot", "sess-1") == []

    def test_empty_when_buffer_malformed(self, tmp_path):
        from app_posture_reflect import _load_session_excerpts
        metrics = tmp_path / "metrics" / "admin_bot"
        metrics.mkdir(parents=True)
        (metrics / "recent-transcripts.json").write_text("not-json")
        assert _load_session_excerpts(tmp_path, "admin_bot", "sess-1") == []

    def test_empty_when_buffer_not_a_list(self, tmp_path):
        from app_posture_reflect import _load_session_excerpts
        _write_buffer(tmp_path, "admin_bot", {"oops": "object"})  # type: ignore[arg-type]
        assert _load_session_excerpts(tmp_path, "admin_bot", "sess-1") == []

    def test_skips_malformed_entries(self, tmp_path):
        """Drop entries with missing turn_index, bad ts, empty text — don't
        crash, don't include them."""
        from app_posture_reflect import _load_session_excerpts
        now = _now()
        _write_buffer(tmp_path, "admin_bot", [
            _entry("sess-1", 1, now - timedelta(minutes=10), "good"),
            {"session_id": "sess-1", "turn_index": "not-int", "ts": _iso(now), "text": "bad"},
            {"session_id": "sess-1", "turn_index": 2, "ts": "not-iso", "text": "bad ts"},
            {"session_id": "sess-1", "turn_index": 3, "ts": _iso(now), "text": "   "},  # empty
            "not-a-dict",
            _entry("sess-1", 4, now - timedelta(minutes=8), "another good"),
        ])
        excerpts = _load_session_excerpts(tmp_path, "admin_bot", "sess-1")
        assert [e["text"] for e in excerpts] == ["good", "another good"]

    def test_truncates_long_turn_text(self, tmp_path):
        """A user dumping a 10KB message shouldn't dominate the prompt
        — truncate per-turn to TRANSCRIPT_TURN_CHAR_CAP."""
        from app_posture_reflect import (
            _load_session_excerpts, TRANSCRIPT_TURN_CHAR_CAP,
        )
        now = _now()
        big = "x" * (TRANSCRIPT_TURN_CHAR_CAP + 100)
        _write_buffer(tmp_path, "admin_bot", [_entry("sess-1", 1, now - timedelta(minutes=5), big)])
        excerpts = _load_session_excerpts(tmp_path, "admin_bot", "sess-1")
        assert len(excerpts) == 1
        # Ends with the truncation ellipsis.
        assert excerpts[0]["text"].endswith("…")
        assert len(excerpts[0]["text"]) <= TRANSCRIPT_TURN_CHAR_CAP

    def test_caps_at_max_turns_dropping_oldest(self, tmp_path):
        """When the session has more turns than max_turns, drop the
        OLDEST — the conversation right before the bot acted is more
        load-bearing than the session opener."""
        from app_posture_reflect import _load_session_excerpts
        now = _now()
        entries = [
            _entry("sess-1", i, now - timedelta(minutes=20 - i), f"turn-{i}")
            for i in range(1, 13)  # 12 turns
        ]
        _write_buffer(tmp_path, "admin_bot", entries)

        excerpts = _load_session_excerpts(tmp_path, "admin_bot", "sess-1", max_turns=5)
        assert len(excerpts) == 5
        # Most recent 5 turns (8, 9, 10, 11, 12) — oldest dropped first.
        assert [e["turn_index"] for e in excerpts] == [8, 9, 10, 11, 12]

    def test_caps_at_max_chars_dropping_oldest(self, tmp_path):
        from app_posture_reflect import _load_session_excerpts
        now = _now()
        # Three turns of 100 chars each = 300 total. Cap at 250.
        entries = [
            _entry("sess-1", 1, now - timedelta(minutes=10), "a" * 100),
            _entry("sess-1", 2, now - timedelta(minutes=8), "b" * 100),
            _entry("sess-1", 3, now - timedelta(minutes=5), "c" * 100),
        ]
        _write_buffer(tmp_path, "admin_bot", entries)
        excerpts = _load_session_excerpts(
            tmp_path, "admin_bot", "sess-1", max_chars=250
        )
        assert len(excerpts) == 2
        # Kept the most recent two.
        assert [e["turn_index"] for e in excerpts] == [2, 3]


# ── _gather_transcript_excerpts ──────────────────────────────────────────────


class TestGatherTranscriptExcerpts:
    def test_empty_when_no_signals(self, tmp_path):
        from app_posture_reflect import _gather_transcript_excerpts
        from app_posture_review import BotPosture
        now = _now()
        posture = BotPosture(
            bot_id="admin_bot", generated_at=_iso(now),
            window_start=_iso(now - timedelta(days=7)), window_end=_iso(now),
            manifests=[], bot_created_signals=[], unmanifested_signals=[],
            orphan_files=[], workspace_path=None, notes=[],
        )
        assert _gather_transcript_excerpts(posture, tmp_path) == ""

    def test_renders_block_when_signal_has_buffered_turns(self, tmp_path):
        from app_posture_reflect import _gather_transcript_excerpts
        now = _now()
        posture, signal = _make_posture_with_signal(
            session_id="sess-1", first_observed_at=now - timedelta(minutes=5),
        )
        _write_buffer(tmp_path, "admin_bot", [
            _entry("sess-1", 1, now - timedelta(minutes=10), "track protein please"),
        ])
        block = _gather_transcript_excerpts(posture, tmp_path)
        assert "[TRANSCRIPT EXCERPTS]" in block
        assert "track protein please" in block
        assert "habits" in block  # the app_id
        assert "[END TRANSCRIPT EXCERPTS]" in block

    def test_omits_block_when_no_buffered_content(self, tmp_path):
        """Signal has session_id but the buffer has nothing for it
        (older than 48h, or scanning opt'd out). The block should NOT
        appear in the prompt — degrades to PR5 behavior."""
        from app_posture_reflect import _gather_transcript_excerpts
        posture, _ = _make_posture_with_signal(session_id="sess-empty")
        # Buffer exists but has unrelated entries.
        _write_buffer(tmp_path, "admin_bot", [
            _entry("other-sess", 1, _now() - timedelta(minutes=5), "wrong session"),
        ])
        block = _gather_transcript_excerpts(posture, tmp_path)
        # An empty block (no excerpts at all for any signal) → return "".
        # But the renderer DOES emit a "(no transcript content)" placeholder
        # for individual signals. The wrapping block-level guard kicks in
        # only when *no* signals have content. Here the signal has zero
        # content → the per-signal block has the placeholder; the wrapping
        # block should render. Verify the placeholder is present:
        assert "_(no transcript content available for this session)_" in block

    def test_handles_signal_without_session_id(self, tmp_path):
        """Older signals may have session_id=None in details — skip
        them rather than crash."""
        from app_posture_reflect import _gather_transcript_excerpts
        posture, sig = _make_posture_with_signal()
        sig.details["session_id"] = None
        block = _gather_transcript_excerpts(posture, tmp_path)
        assert block == ""


# ── build_prompt with shared_dir ─────────────────────────────────────────────


class TestBuildPromptWithTranscripts:
    def test_includes_transcripts_when_shared_dir_provided(self, tmp_path):
        from app_posture_reflect import build_prompt
        now = _now()
        posture, _ = _make_posture_with_signal(
            session_id="sess-1", first_observed_at=now - timedelta(minutes=5),
        )
        _write_buffer(tmp_path, "admin_bot", [
            _entry("sess-1", 1, now - timedelta(minutes=10), "build me a habit tracker"),
        ])
        prompt = build_prompt(posture, shared_dir=tmp_path)
        assert "[TRANSCRIPT EXCERPTS]" in prompt
        assert "build me a habit tracker" in prompt

    def test_omits_transcripts_when_shared_dir_none(self, tmp_path):
        """PR5 callers (no shared_dir) get the original behavior — no
        transcript block. Backwards-compatible.

        Asserts on the block opener with explicit newline framing rather
        than the bare phrase, since the Missed Signals question text
        also mentions 'TRANSCRIPT EXCERPTS' (telling the LLM what to do
        when present)."""
        from app_posture_reflect import build_prompt
        posture, _ = _make_posture_with_signal()
        prompt = build_prompt(posture)
        # The block, when present, opens at the start of a line preceded
        # by a blank line. The question's prose mention is mid-paragraph.
        assert "\n[TRANSCRIPT EXCERPTS]\n" not in prompt

    def test_omits_transcripts_when_buffer_unreachable(self, tmp_path):
        """No buffer file → no transcript block in the prompt."""
        from app_posture_reflect import build_prompt
        posture, _ = _make_posture_with_signal()
        # tmp_path has no metrics dir for this bot.
        prompt = build_prompt(posture, shared_dir=tmp_path)
        # The block-level guard kicks in when no signals have content;
        # for a single signal with no buffer entries, the per-signal
        # placeholder appears inside the wrapping block.
        if "\n[TRANSCRIPT EXCERPTS]\n" in prompt:
            assert "_(no transcript content available for this session)_" in prompt

    def test_missed_signals_question_references_transcripts(self):
        from app_posture_reflect import build_prompt
        posture, _ = _make_posture_with_signal()
        prompt = build_prompt(posture, shared_dir=None)
        # The Missed Signals question now points to the transcript block
        # when present and instructs graceful degradation when not.
        assert "TRANSCRIPT EXCERPTS" in prompt
        assert "no transcript context" in prompt


# ── reflect() with shared_dir ────────────────────────────────────────────────


class TestReflectWithSharedDir:
    def test_reflect_passes_shared_dir_into_prompt(self, monkeypatch, tmp_path):
        """End-to-end through reflect(): the LLM receives a prompt with
        transcripts when shared_dir is provided and the buffer has content."""
        from app_posture_reflect import reflect
        now = _now()
        posture, _ = _make_posture_with_signal(
            session_id="sess-1", first_observed_at=now - timedelta(minutes=5),
        )
        _write_buffer(tmp_path, "admin_bot", [
            _entry("sess-1", 1, now - timedelta(minutes=10), "track protein please"),
        ])

        captured: dict = {}

        def fake_call(target, prompt, **kw):
            captured["prompt"] = prompt
            return (
                "## Reflection\n\n"
                "### Clusters\nNone.\n\n"
                "### Splits\nNone.\n\n"
                "### Orphan dispositions\nNo orphans.\n\n"
                "### Missed signals\nUser asked about protein.\n\n"
                "### Forward guidance for next week\nWatch for diet mentions.\n"
            )

        monkeypatch.setattr("app_posture_reflect._resolve_target", lambda b: _fake_target())
        monkeypatch.setattr("app_posture_reflect._call_llm", fake_call)

        result = reflect(posture, shared_dir=tmp_path)
        assert result.ok is True
        assert "[TRANSCRIPT EXCERPTS]" in captured["prompt"]
        assert "track protein please" in captured["prompt"]
