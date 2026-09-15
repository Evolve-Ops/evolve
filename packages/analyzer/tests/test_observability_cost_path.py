"""tests/test_observability_cost_path.py — V1.5-1 cost-ledger path.

Verifies that cost_ledger and cost_rollup can read per-call cost from
observability spans (retiring the "wait for upstream cost events"
MVP blocker).
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from observability.opik_client import JsonlBackend, OpikSpan  # noqa: E402

import cost_ledger  # noqa: E402
import cost_rollup  # noqa: E402


def _llm_span(
    *,
    bot_id: str,
    when: datetime,
    cost: float,
    in_tokens: int = 100,
    out_tokens: int = 50,
    model: str = "claude-sonnet-4-6",
    session_id: str = "sess-1",
    trigger_kind: str = "user_turn",
) -> OpikSpan:
    return OpikSpan(
        name="llm_call",
        start_time=when,
        end_time=when,
        type="llm",
        producer="gateway",
        bot_id=bot_id,
        model=model,
        provider="anthropic",
        total_cost=cost,
        usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
        attributes={"session_id": session_id, "trigger_kind": trigger_kind, "cache_state": "miss"},
    )


def test_read_events_from_observability_yields_cost_events(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    today = datetime.now(timezone.utc).replace(microsecond=0)
    backend.record_span(_llm_span(bot_id="admin_bot", when=today, cost=0.01))
    backend.record_span(_llm_span(bot_id="admin_bot", when=today - timedelta(hours=1), cost=0.02))
    backend.record_span(_llm_span(bot_id="team_bot_a", when=today, cost=0.03))

    events = list(cost_ledger.read_events_from_observability(
        "admin_bot", days=2, shared_dir=tmp_path, client=backend,
    ))
    # Two admin_bot events; the team_bot_a event is filtered by bot_id.
    assert len(events) == 2
    for e in events:
        assert e["type"] == "cost_event"
        assert e["bot_id"] == "admin_bot"
        assert e["source"] == "observability"


def test_read_events_from_observability_skips_non_billable(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # A non-LLM span (type=general, no usage, no cost) — should not surface
    # as cost_event because span_to_cost_event returns None AND the
    # SpanFilter restricts to type=llm.
    backend.record_span(OpikSpan(
        name="some_internal_thing",
        start_time=now,
        end_time=now,
        type="general",
        bot_id="admin_bot",
    ))
    backend.record_span(_llm_span(bot_id="admin_bot", when=now, cost=0.01))
    events = list(cost_ledger.read_events_from_observability(
        "admin_bot", days=1, shared_dir=tmp_path, client=backend,
    ))
    assert len(events) == 1
    assert events[0]["cost_usd"] == pytest.approx(0.01)


def test_rollup_aggregates_observability_events(tmp_path: Path, monkeypatch):
    # Record an LLM span for today; then verify the iter_cost_events
    # rollup includes it.
    backend = JsonlBackend(tmp_path)
    today = date.today()
    when = datetime(today.year, today.month, today.day, 12, 0, 0, tzinfo=timezone.utc)
    backend.record_span(_llm_span(bot_id="admin_bot", when=when, cost=0.05, in_tokens=200, out_tokens=100))
    backend.record_span(_llm_span(bot_id="admin_bot", when=when + timedelta(minutes=5), cost=0.03, in_tokens=150, out_tokens=75))

    events = list(cost_rollup.iter_cost_events(tmp_path, "admin_bot", today))
    # Only the observability path has data (annotations/ doesn't exist),
    # so all entries should be from observability source.
    assert len(events) == 2
    total = sum(e.get("cost_usd", 0.0) for e in events)
    assert total == pytest.approx(0.08)
    in_tokens = sum(e.get("input_tokens", 0) for e in events)
    assert in_tokens == 350


def test_rollup_observability_path_no_op_when_no_spans(tmp_path: Path):
    # No spans recorded → iter_cost_events returns []
    today = date.today()
    events = list(cost_rollup.iter_cost_events(tmp_path, "admin_bot", today))
    assert events == []


def test_rollup_observability_path_filters_by_day(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    today = date.today()
    yesterday = today - timedelta(days=1)
    today_dt = datetime(today.year, today.month, today.day, 12, 0, 0, tzinfo=timezone.utc)
    yesterday_dt = datetime(yesterday.year, yesterday.month, yesterday.day, 12, 0, 0, tzinfo=timezone.utc)
    backend.record_span(_llm_span(bot_id="admin_bot", when=today_dt, cost=0.01))
    backend.record_span(_llm_span(bot_id="admin_bot", when=yesterday_dt, cost=0.02))

    today_events = list(cost_rollup.iter_cost_events(tmp_path, "admin_bot", today))
    assert len(today_events) == 1
    assert today_events[0]["cost_usd"] == pytest.approx(0.01)
    y_events = list(cost_rollup.iter_cost_events(tmp_path, "admin_bot", yesterday))
    assert len(y_events) == 1
    assert y_events[0]["cost_usd"] == pytest.approx(0.02)


def test_write_rollup_uses_observability_events(tmp_path: Path):
    # Drive the full write_rollup path end-to-end via observability.
    backend = JsonlBackend(tmp_path)
    today = date.today()
    when = datetime(today.year, today.month, today.day, 12, 0, 0, tzinfo=timezone.utc)
    backend.record_span(_llm_span(bot_id="admin_bot", when=when, cost=0.05, in_tokens=200, out_tokens=100))

    rollup = cost_rollup.write_rollup(tmp_path, "admin_bot", today)
    assert rollup is not None
    assert rollup["bot_id"] == "admin_bot"
    assert rollup["date"] == today.isoformat()
    assert rollup["total_usd"] == pytest.approx(0.05)
    assert rollup["input_tokens"] == 200
    assert rollup["output_tokens"] == 100
    # by_model contains the model we recorded
    assert "claude-sonnet-4-6" in rollup["by_model"]

    # File got written to the canonical location
    out_path = tmp_path / "metrics" / "admin_bot" / f"cost-{today.isoformat()}.json"
    assert out_path.exists()
