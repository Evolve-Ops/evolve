"""tests/test_turn_autopsy.py — the autopsy sweep (TA-1).

The fixtures ARE the design's replay falsifier: the gateway-log and
session-record shapes below are copied from the real 2026-08-31 incident
records. If the classifier stops catching them, the sweep failed its
reason for existing.

Also pinned:
  * one dedup'd signal per (bot, cause, shape) — reruns don't multiply;
  * sweep_resolve clears a cause that stops appearing — but NOT when a
    source was unreadable (blindness must never present as clean);
  * unknown stopReasons land in the honest incomplete_turn bucket;
  * below-threshold tool repetition emits nothing (honest retries are not
    storms).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import turn_autopsy as ta  # noqa: E402
from signals import store as signals_store  # noqa: E402

_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
_SINCE = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)

# Verbatim shape from the incident (only identifiers neutralized).
_ERR_LINE = (
    "2026-08-31T20:06:47.214-07:00 [agent/embedded] incomplete turn detected: "
    "runId=94edaca2 sessionId=eddae595 provider=anthropic/claude-opus-5 "
    "stopReason=length hasLastAssistant=yes"
)
_ERR_LINE_UNKNOWN = _ERR_LINE.replace("stopReason=length", "stopReason=mystery")


def _assistant(ts: str, provider: str, tool_calls: int = 0) -> str:
    content = [{"type": "text", "text": "ok"}]
    for _ in range(tool_calls):
        content.append({
            "type": "toolCall", "id": "c1", "name": "gmail_send",
            "arguments": {"to": ["a@example.com"], "subject": "TEST",
                          "body": "Test email.", "cc": [], "bcc": []},
        })
    return json.dumps({
        "type": "message", "timestamp": ts,
        "message": {"role": "assistant", "provider": provider,
                    "model": "m", "content": content},
    })


def _bot_home(tmp_path: Path, bot: str, err_lines: list[str],
              session_lines: list[str]) -> Path:
    home = tmp_path / bot
    logs = home / ".openclaw" / "logs"
    logs.mkdir(parents=True)
    (logs / "gateway.err.log").write_text("\n".join(err_lines) + "\n")
    sess = home / ".openclaw" / "agents" / "main" / "sessions"
    sess.mkdir(parents=True)
    (sess / "eddae595-24f7-42c2-8bb9-37f05fb16c0e.jsonl").write_text(
        "\n".join(session_lines) + "\n")
    return home


def _sweep(tmp_path: Path, home: Path, bot: str = "personal-bot"):
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    autopsy = ta.collect_for_bot(bot, home, _SINCE)
    ta.emit(shared, autopsy)
    return shared, autopsy


def _firing(shared: Path) -> dict[str, dict]:
    out = {}
    for p in (shared / "signals" / "firing").glob("*.json"):
        sig = json.loads(p.read_text())
        out[sig["signature"]] = sig
    return out


def test_output_cap_hit_classified_from_real_line(tmp_path: Path):
    home = _bot_home(tmp_path, "b", [_ERR_LINE], [])
    shared, _ = _sweep(tmp_path, home)
    sigs = _firing(shared)
    key = "personal-bot|output_cap_hit|anthropic/claude-opus-5"
    assert key in sigs
    assert sigs[key]["type"] == "turn_autopsy_output_cap_hit"
    assert "maxTokens" in sigs[key]["body"]


def test_unknown_stop_reason_lands_in_honest_bucket(tmp_path: Path):
    home = _bot_home(tmp_path, "b", [_ERR_LINE_UNKNOWN], [])
    shared, _ = _sweep(tmp_path, home)
    sigs = _firing(shared)
    key = "personal-bot|incomplete_turn|anthropic/claude-opus-5|mystery"
    assert key in sigs
    assert "mystery" in sigs[key]["body"]


def test_provider_swap_detected(tmp_path: Path):
    lines = [
        _assistant("2026-08-31T18:50:00.000Z", "anthropic"),
        _assistant("2026-08-31T18:52:00.000Z", "xai"),
    ]
    home = _bot_home(tmp_path, "b", [], lines)
    shared, _ = _sweep(tmp_path, home)
    assert "personal-bot|provider_swap|anthropic->xai" in _firing(shared)


def test_tool_repeat_loop_at_threshold_only(tmp_path: Path):
    quiet = [_assistant(f"2026-08-31T18:5{i}:00.000Z", "xai", tool_calls=1)
             for i in range(ta.TOOL_REPEAT_MIN - 1)]
    home = _bot_home(tmp_path, "quiet", [], quiet)
    shared, _ = _sweep(tmp_path, home)
    assert not any("tool_repeat_loop" in s for s in _firing(shared))

    stormy = [_assistant(f"2026-08-31T18:52:{i:02d}.000Z", "xai", tool_calls=1)
              for i in range(ta.TOOL_REPEAT_MIN)]
    home2 = _bot_home(tmp_path, "stormy", [], stormy)
    shared2, _ = _sweep(tmp_path, home2)
    sigs = _firing(shared2)
    key = "personal-bot|tool_repeat_loop|gmail_send"
    assert key in sigs
    assert sigs[key]["details"]["examples"][0]["identical_calls"] >= ta.TOOL_REPEAT_MIN


def test_rerun_dedups_and_sweep_resolves_cleared_cause(tmp_path: Path):
    home = _bot_home(tmp_path, "b", [_ERR_LINE], [])
    shared, _ = _sweep(tmp_path, home)
    _sweep(tmp_path, home)  # rerun: same finding
    key = "personal-bot|output_cap_hit|anthropic/claude-opus-5"
    assert list(_firing(shared)) == [key]
    # Cause clears (empty err log) → sweep resolves it.
    (home / ".openclaw" / "logs" / "gateway.err.log").write_text("")
    autopsy = ta.collect_for_bot("personal-bot", home, _SINCE)
    ta.emit(shared, autopsy)
    assert key not in _firing(shared)


def test_unreadable_source_skips_sweep_resolve(tmp_path: Path, monkeypatch):
    home = _bot_home(tmp_path, "b", [_ERR_LINE], [])
    shared, _ = _sweep(tmp_path, home)
    key = "personal-bot|output_cap_hit|anthropic/claude-opus-5"
    assert key in _firing(shared)
    # Next run: the err log is unreadable. The old signal must SURVIVE.
    monkeypatch.setattr(ta, "_read_tail",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
    autopsy = ta.collect_for_bot("personal-bot", home, _SINCE)
    assert autopsy.sources_unreadable
    ta.emit(shared, autopsy)
    assert key in _firing(shared)


def test_old_lines_outside_window_ignored(tmp_path: Path):
    stale = _ERR_LINE.replace("2026-08-31", "2026-06-01")
    home = _bot_home(tmp_path, "b", [stale], [])
    shared, autopsy = _sweep(tmp_path, home)
    assert not autopsy.findings and not _firing(shared)
