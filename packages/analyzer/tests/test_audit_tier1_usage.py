"""Tests for cascade.audit_tier1_usage.

Validates:
  - tier1 filter: only ``cascade.tier_used == "tier1"`` spans are kept
  - --bot filter scopes correctly
  - --days windowing honors since/until
  - Summary aggregation: per-bot, per-driver, totals
  - Detail formatter produces single-line, scan-friendly output
  - 100%-default-driver heuristic produces the attribution-bug warning
  - Empty span dir produces a clean "no tier1" message (not a crash)

These tests synthesize spans on disk under a tmp shared_dir so they
exercise the real ``iter_turn_spans`` reader.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from cascade.audit_tier1_usage import (  # noqa: E402
    _format_detail_row,
    _load_tier1_spans,
    _print_summary,
    _summarize,
    main,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _write_span(
    shared: Path,
    bot_id: str,
    *,
    day: str,
    tier_used: str = "tier1",
    chosen_by: str = "default",
    cost: float = 0.50,
    model: str = "anthropic/claude-opus-4-8",
    preflight_layer: str | None = None,
    preflight_reason: str | None = None,
    tier_intended: str = "tier2",
    start_time: str | None = None,
) -> None:
    """Append a synthetic cascade-telemetry span to the bot's daily file."""
    spans_dir = shared / bot_id / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    path = spans_dir / f"spans-{day}.jsonl"
    attrs: dict = {
        "cascade.tier_used": tier_used,
        "cascade.tier_intended": tier_intended,
        "cascade.tier_chosen_by": chosen_by,
    }
    if preflight_layer is not None:
        attrs["cascade.preflight.layer"] = preflight_layer
    if preflight_reason is not None:
        attrs["cascade.preflight.reason"] = preflight_reason
    span = {
        "name": "bot_session_turn",
        "trace_id": f"trace-{bot_id}-{day}-{tier_used}",
        "start_time": start_time or f"{day}T12:00:00Z",
        "end_time": start_time or f"{day}T12:00:30Z",
        "bot_id": bot_id,
        "model": model,
        "total_cost": cost,
        "usage": {"input_tokens": 1000, "output_tokens": 500},
        "producer": "cascade_telemetry",
        "attributes": attrs,
    }
    with open(path, "a") as f:
        f.write(json.dumps(span) + "\n")


# ── Span loader ─────────────────────────────────────────────────────────────

def test_load_tier1_spans_filters_to_tier1_only(tmp_path: Path):
    # Use a recent-but-past timestamp (1 hour ago) so the span lands in
    # any reasonable window — "today at noon UTC" can be in the future
    # if the test runs before noon UTC.
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    today = one_hour_ago.strftime("%Y-%m-%d")
    start_time = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_span(tmp_path, "bot_a", day=today, tier_used="tier1")
    _write_span(tmp_path, "bot_a", day=today, tier_used="tier2")
    _write_span(tmp_path, "bot_a", day=today, tier_used="tier3")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    until = datetime.now(timezone.utc) + timedelta(days=1)
    spans = _load_tier1_spans(tmp_path, since=since, until=until)
    assert len(spans) == 1
    assert spans[0]["attributes"]["cascade.tier_used"] == "tier1"


def test_load_tier1_spans_honors_bot_filter(tmp_path: Path):
    # Use a recent-but-past timestamp (1 hour ago) so the span lands in
    # any reasonable window — "today at noon UTC" can be in the future
    # if the test runs before noon UTC.
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    today = one_hour_ago.strftime("%Y-%m-%d")
    start_time = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_span(tmp_path, "bot_a", day=today, tier_used="tier1")
    _write_span(tmp_path, "bot_b", day=today, tier_used="tier1")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    until = datetime.now(timezone.utc) + timedelta(days=1)
    spans = _load_tier1_spans(tmp_path, since=since, until=until, bot_id="bot_a")
    assert len(spans) == 1
    assert spans[0]["bot_id"] == "bot_a"


def test_load_tier1_spans_empty_when_window_excludes(tmp_path: Path):
    # Old span — outside the window
    _write_span(tmp_path, "bot_a", day="2025-01-01", tier_used="tier1",
                start_time="2025-01-01T12:00:00Z")
    # Window = last 7 days
    since = datetime.now(timezone.utc) - timedelta(days=7)
    until = datetime.now(timezone.utc)
    spans = _load_tier1_spans(tmp_path, since=since, until=until)
    assert spans == []


def test_load_tier1_spans_empty_when_no_spans_dir(tmp_path: Path):
    # Brand-new pod — no bot dirs, no spans, no crash
    since = datetime.now(timezone.utc) - timedelta(days=7)
    until = datetime.now(timezone.utc)
    spans = _load_tier1_spans(tmp_path, since=since, until=until)
    assert spans == []


# ── Aggregation ─────────────────────────────────────────────────────────────

def test_summarize_counts_and_totals(tmp_path: Path):
    spans = [
        {"bot_id": "a", "total_cost": 1.0,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "user_request"}},
        {"bot_id": "a", "total_cost": 0.5,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
        {"bot_id": "b", "total_cost": 2.0,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
    ]
    s = _summarize(spans)
    assert s["total_count"] == 3
    assert s["total_cost"] == 3.5
    assert s["per_bot"]["a"] == 2
    assert s["per_bot"]["b"] == 1
    assert s["per_bot_cost"]["a"] == 1.5
    assert s["per_bot_cost"]["b"] == 2.0
    assert s["per_driver"]["default"] == 2
    assert s["per_driver"]["user_request"] == 1
    assert s["per_driver_cost"]["default"] == 2.5
    assert s["per_driver_cost"]["user_request"] == 1.0


def test_summarize_handles_missing_cost_and_driver(tmp_path: Path):
    """Defensive: a span without total_cost or chosen_by must not crash."""
    spans = [
        {"bot_id": "a", "attributes": {"cascade.tier_used": "tier1"}},
        {"bot_id": "a", "total_cost": None,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": None}},
    ]
    s = _summarize(spans)
    assert s["total_count"] == 2
    assert s["total_cost"] == 0
    assert s["per_driver"]["<null>"] == 2


# ── Detail row formatter ────────────────────────────────────────────────────

def test_format_detail_row_one_line_with_all_fields():
    span = {
        "start_time": "2026-06-08T03:05:36Z",
        "bot_id": "team_bot_a",
        "total_cost": 0.96,
        "model": "anthropic/claude-opus-4-8",
        "attributes": {
            "cascade.tier_chosen_by": "default",
            "cascade.tier_intended": "tier2",
            "cascade.preflight.layer": "regex",
            "cascade.preflight.reason": "regex:explicit_thinking_request",
        },
    }
    row = _format_detail_row(span)
    # Single line, no embedded newlines
    assert "\n" not in row
    # All the load-bearing fields appear
    assert "team_bot_a" in row
    # Cost includes the column-aligned space ("$ 0.96") — match on the
    # value and currency sigil, not exact glyph adjacency.
    assert "$" in row and "0.96" in row
    assert "chosen=default" in row
    assert "intended=tier2" in row
    assert "pf=regex" in row
    assert "regex:explicit_thinking_request" in row
    assert "claude-opus-4-8" in row


def test_format_detail_row_handles_missing_preflight_fields():
    # Spans where preflight didn't run (most heartbeats etc.) — fields
    # absent. Row should print "<null>" placeholders, not crash.
    span = {
        "start_time": "2026-06-08T03:05:36Z",
        "bot_id": "team_bot_a",
        "total_cost": 0.50,
        "model": "anthropic/claude-opus-4-7",
        "attributes": {
            "cascade.tier_chosen_by": "default",
            "cascade.tier_intended": "tier2",
        },
    }
    row = _format_detail_row(span)
    assert "pf=<null>" in row
    assert "reason=<null>" in row


# ── Summary printer ─────────────────────────────────────────────────────────

def test_print_summary_empty_message_when_no_tier1():
    s = {
        "total_count": 0,
        "total_cost": 0,
        "per_bot": {},
        "per_bot_cost": {},
        "per_driver": {},
        "per_driver_cost": {},
        "per_bot_driver": {},
    }
    buf = StringIO()
    with patch("sys.stdout", buf):
        _print_summary(s, window_str="last 14 days")
    out = buf.getvalue()
    assert "no tier1 turns" in out
    assert "well-conserved" in out


def test_print_summary_emits_attribution_bug_warning_when_100pct_default():
    """The signature of the 2026-06-08 attribution bug: every tier1 turn
    on the pod tagged chosen_by='default'. Surface a warning so the
    operator notices it without having to read the docs."""
    s = _summarize([
        {"bot_id": "a", "total_cost": 1.0,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
        {"bot_id": "b", "total_cost": 0.5,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
        {"bot_id": "c", "total_cost": 0.3,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
    ])
    buf = StringIO()
    with patch("sys.stdout", buf):
        _print_summary(s, window_str="last 14 days")
    out = buf.getvalue()
    assert "attribution bug" in out
    assert "PR #2384" in out


def test_print_summary_no_warning_when_mixed_drivers():
    """Mixed driver distribution = healthy. No warning fires."""
    s = _summarize([
        {"bot_id": "a", "total_cost": 1.0,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "user_request"}},
        {"bot_id": "a", "total_cost": 0.5,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
        {"bot_id": "b", "total_cost": 2.0,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "preflight"}},
    ])
    buf = StringIO()
    with patch("sys.stdout", buf):
        _print_summary(s, window_str="last 14 days")
    out = buf.getvalue()
    assert "attribution bug" not in out


def test_print_summary_no_warning_when_default_count_below_floor():
    """Threshold for the warning is >=3 default turns. With 2 turns,
    even at 100% default, suppress — sample too small to call a bug."""
    s = _summarize([
        {"bot_id": "a", "total_cost": 1.0,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
        {"bot_id": "b", "total_cost": 0.5,
         "attributes": {"cascade.tier_used": "tier1",
                        "cascade.tier_chosen_by": "default"}},
    ])
    buf = StringIO()
    with patch("sys.stdout", buf):
        _print_summary(s, window_str="last 14 days")
    out = buf.getvalue()
    assert "attribution bug" not in out


# ── End-to-end main() ────────────────────────────────────────────────────────

def test_main_runs_clean_on_empty_pod(tmp_path: Path, capsys):
    test_argv = [
        "audit_tier1_usage",
        "--shared-dir", str(tmp_path),
        "--days", "7",
    ]
    with patch.object(sys, "argv", test_argv):
        rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    # Empty pod produces the "no tier1" message, not a crash
    assert "no tier1 turns" in captured.out


def test_main_exits_2_when_shared_dir_missing(capsys):
    test_argv = [
        "audit_tier1_usage",
        "--shared-dir", "/does/not/exist/anywhere",
    ]
    with patch.object(sys, "argv", test_argv):
        rc = main()
    assert rc == 2
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "does not exist" in captured.err


def test_main_summary_flag_omits_detail_section(tmp_path: Path, capsys):
    # Use a recent-but-past timestamp (1 hour ago) so the span lands in
    # any reasonable window — "today at noon UTC" can be in the future
    # if the test runs before noon UTC.
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    today = one_hour_ago.strftime("%Y-%m-%d")
    start_time = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_span(tmp_path, "bot_a", day=today, tier_used="tier1", cost=1.0,
                start_time=start_time)
    test_argv = [
        "audit_tier1_usage",
        "--shared-dir", str(tmp_path),
        "--days", "7",
        "--summary",
    ]
    with patch.object(sys, "argv", test_argv):
        rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "Per-turn detail" not in captured.out
    # Summary section still present
    assert "tier1 usage report" in captured.out
    assert "by driver" in captured.out


def test_main_includes_detail_section_when_not_summary(tmp_path: Path, capsys):
    # Use a recent-but-past timestamp (1 hour ago) so the span lands in
    # any reasonable window — "today at noon UTC" can be in the future
    # if the test runs before noon UTC.
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    today = one_hour_ago.strftime("%Y-%m-%d")
    start_time = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_span(tmp_path, "bot_a", day=today, tier_used="tier1", cost=1.0,
                start_time=start_time,
                preflight_layer="regex",
                preflight_reason="regex:explicit_thinking_request")
    test_argv = [
        "audit_tier1_usage",
        "--shared-dir", str(tmp_path),
        "--days", "7",
    ]
    with patch.object(sys, "argv", test_argv):
        rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "Per-turn detail" in captured.out
    assert "regex:explicit_thinking_request" in captured.out
    assert "tier1 usage report" in captured.out
