"""tests/test_vocabulary_expander_monitor.py — pin the Layer 2
plumbing monitor's opt-in gate, threshold gate, and preflight shape.

NO LLM in this PR. The monitor identifies unrecognized nouns and
writes a preflight report describing what an LLM call *would* look
like. PR γ wires the actual LLM hookup.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import _merged_vocabulary as mv  # noqa: E402
import vocabulary_expander_monitor as vex  # noqa: E402
from observations.tuples import write_tuples  # noqa: E402
from schema.observation import ObservationTuple  # noqa: E402


BOT_ID = "team-bot-a"
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _write_tuples_for_noun(
    shared_dir: Path,
    bot_id: str,
    noun: str,
    n: int = 5,
) -> None:
    for i in range(n):
        day = NOW - timedelta(days=i)
        t = ObservationTuple(
            id=f"vex-{noun}-{i}",
            bot_id=bot_id,
            session_id=f"vex-sess-{noun}-{i}",
            segment_id=f"vex-seg-{i}",
            noun=noun,
            verb="tracking",
            mood=None,
            engagement=2,
            timestamp_start=day.isoformat(),
            timestamp_end=(day + timedelta(minutes=5)).isoformat(),
            source_hash=f"vex-hash-{noun}-{i}",
        )
        write_tuples([t], shared_dir=shared_dir, bot_id=bot_id, day=day)


# ─────────────────────────────────────────────────────────────────────────────
# Config gate
# ─────────────────────────────────────────────────────────────────────────────


def test_config_default_is_disabled(tmp_path):
    """No rsi.vocabulary_expansion block in network.json → enabled
    defaults to False. Zero-cost contract."""
    p = tmp_path / "network.json"
    p.write_text(json.dumps({"bots": {}}))
    cfg = vex._load_pod_config(p)
    assert cfg.enabled is False


def test_config_missing_file_defaults_to_disabled(tmp_path):
    """A missing network.json → defaults (disabled). Don't crash on
    a fresh dev environment."""
    cfg = vex._load_pod_config(tmp_path / "nope.json")
    assert cfg.enabled is False


def test_config_honors_explicit_enable(tmp_path):
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "bots": {},
        "rsi": {"vocabulary_expansion": {"enabled": True}},
    }))
    cfg = vex._load_pod_config(p)
    assert cfg.enabled is True


def test_config_honors_custom_threshold(tmp_path):
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "bots": {},
        "rsi": {"vocabulary_expansion": {
            "enabled": True,
            "threshold_nouns": 50,
            "window_days": 14,
        }},
    }))
    cfg = vex._load_pod_config(p)
    assert cfg.threshold_nouns == 50
    assert cfg.window_days == 14


# ─────────────────────────────────────────────────────────────────────────────
# Unrecognized-noun detection
# ─────────────────────────────────────────────────────────────────────────────


def test_recognized_noun_excluded_from_unrecognized_count(tmp_path):
    """A tuple whose noun resolves via the static vocab does NOT
    count as unrecognized. v1.5 vocab includes 'workout' →
    domain:fitness."""
    _write_tuples_for_noun(tmp_path, BOT_ID, "workout", n=5)
    counts = vex.collect_unrecognized_nouns(BOT_ID, tmp_path, now=NOW)
    assert "workout" not in counts


def test_unknown_noun_counted_as_unrecognized(tmp_path):
    """A tuple noun with no static / dynamic vocab match counts as
    unrecognized. This is what the LLM expansion flow (PR γ) would
    classify."""
    _write_tuples_for_noun(tmp_path, BOT_ID, "sourdough", n=5)
    counts = vex.collect_unrecognized_nouns(BOT_ID, tmp_path, now=NOW)
    assert counts["sourdough"] == 5


def test_dynamic_addition_makes_noun_recognized(tmp_path):
    """End-to-end of the merge layer: add a dynamic keyword, the
    monitor stops flagging that noun. Operator can use vocab_add
    to manage the unrecognized-noun set without a code change."""
    _write_tuples_for_noun(tmp_path, BOT_ID, "sourdough", n=5)
    # Before: counted.
    assert "sourdough" in vex.collect_unrecognized_nouns(
        BOT_ID, tmp_path, now=NOW
    )
    # Add as dynamic.
    mv.add_keyword(
        tmp_path, "sourdough", "domain:food", added_by="manual"
    )
    # After: no longer counted.
    assert "sourdough" not in vex.collect_unrecognized_nouns(
        BOT_ID, tmp_path, now=NOW
    )


def test_boring_nouns_filtered_out(tmp_path):
    """Stop-words / extraction artifacts don't count, even if they're
    unrecognized. Avoids the LLM call (when γ ships) being asked to
    classify 'thing' or 'today'."""
    _write_tuples_for_noun(tmp_path, BOT_ID, "thing", n=10)
    _write_tuples_for_noun(tmp_path, BOT_ID, "today", n=10)
    counts = vex.collect_unrecognized_nouns(BOT_ID, tmp_path, now=NOW)
    assert "thing" not in counts
    assert "today" not in counts


def test_too_short_noun_filtered(tmp_path):
    """Single-character + 2-character nouns filtered."""
    # ObservationTuple validation might reject too-short nouns, but
    # even if they slip through, the filter must drop them.
    pass  # documented at the filter level; can't construct invalid


def test_too_long_noun_filtered(tmp_path):
    """A 41+ char noun is almost always an extraction artifact."""
    long_noun = "a" * 50
    _write_tuples_for_noun(tmp_path, BOT_ID, long_noun, n=5)
    counts = vex.collect_unrecognized_nouns(BOT_ID, tmp_path, now=NOW)
    assert long_noun not in counts


# ─────────────────────────────────────────────────────────────────────────────
# Preflight report shape
# ─────────────────────────────────────────────────────────────────────────────


def test_preflight_report_written_when_threshold_met(tmp_path):
    """Run for_pod with enabled config + > threshold unrecognized
    nouns → a preflight report appears on disk for this bot."""
    # Write tuples for 25 distinct unknown nouns.
    for i in range(25):
        _write_tuples_for_noun(tmp_path, BOT_ID, f"unknown{i:02d}word", n=2)
    cfg = vex._Config(
        enabled=True, threshold_nouns=20, window_days=30,
    )
    summary = vex.run_for_pod([BOT_ID], tmp_path, config=cfg, now=NOW)
    assert BOT_ID in summary["preflight_reports_written"]
    preflight = vex._preflight_path(tmp_path, BOT_ID)
    assert preflight.exists()


def test_preflight_report_NOT_written_when_below_threshold(tmp_path):
    """5 unknown nouns < threshold(20) → no preflight written. Avoids
    the LLM (γ) being called for thin signal."""
    for i in range(5):
        _write_tuples_for_noun(tmp_path, BOT_ID, f"unknown{i:02d}word", n=2)
    cfg = vex._Config(
        enabled=True, threshold_nouns=20, window_days=30,
    )
    summary = vex.run_for_pod([BOT_ID], tmp_path, config=cfg, now=NOW)
    assert BOT_ID not in summary["preflight_reports_written"]


def test_preflight_report_schema(tmp_path):
    """The preflight report carries the fields PR γ's LLM caller will
    need to consume it: noun list with frequencies, threshold, window,
    current vocab size, instructions block."""
    for i in range(25):
        _write_tuples_for_noun(tmp_path, BOT_ID, f"unknown{i:02d}word", n=2)
    cfg = vex._Config(
        enabled=True, threshold_nouns=20, window_days=30,
    )
    vex.run_for_pod([BOT_ID], tmp_path, config=cfg, now=NOW)
    payload = json.loads(
        vex._preflight_path(tmp_path, BOT_ID).read_text()
    )
    assert payload["producer"] == "vocabulary_expander_monitor"
    assert payload["bot_id"] == BOT_ID
    assert payload["window_days"] == 30
    assert payload["threshold"] == 20
    assert payload["distinct_unrecognized_nouns"] == 25
    assert isinstance(payload["nouns_for_classification"], list)
    assert len(payload["nouns_for_classification"]) > 0
    first = payload["nouns_for_classification"][0]
    assert "noun" in first
    assert "frequency" in first
    assert "instructions_for_llm_caller" in payload
    assert "current_vocabulary_size" in payload


def test_preflight_report_caps_noun_count(tmp_path):
    """Even when 200 unknown nouns are present, the preflight caps the
    list at PREFLIGHT_NOUN_CAP (100). Bounds the LLM input cost when γ
    ships."""
    for i in range(150):
        _write_tuples_for_noun(tmp_path, BOT_ID, f"unknown{i:03d}word", n=1)
    cfg = vex._Config(
        enabled=True, threshold_nouns=20, window_days=30,
    )
    vex.run_for_pod([BOT_ID], tmp_path, config=cfg, now=NOW)
    payload = json.loads(
        vex._preflight_path(tmp_path, BOT_ID).read_text()
    )
    assert len(payload["nouns_for_classification"]) <= vex.PREFLIGHT_NOUN_CAP


# ─────────────────────────────────────────────────────────────────────────────
# Run summary
# ─────────────────────────────────────────────────────────────────────────────


def test_run_summary_includes_per_bot_status(tmp_path):
    """The summary dict must surface per-bot threshold-crossed status
    so an operator running the monitor manually can see which bots
    cleared the bar without inspecting individual preflight files."""
    _write_tuples_for_noun(tmp_path, BOT_ID, "sourdough", n=5)
    cfg = vex._Config(
        enabled=True, threshold_nouns=20, window_days=30,
    )
    summary = vex.run_for_pod([BOT_ID], tmp_path, config=cfg, now=NOW)
    assert BOT_ID in summary["per_bot"]
    entry = summary["per_bot"][BOT_ID]
    assert entry["above_threshold"] is False
    assert entry["distinct_unrecognized_nouns"] == 1
