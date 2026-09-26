"""The weekly receipt's cross-turn cache lines.

The receipt already said what the pod SPENT and how that estimate compared to
the bill. It said nothing about the mechanism, which is how a day whose every
turn re-warmed a ~45k prefix could report a 79-87% cache hit rate and pass
unremarked (``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §2).

Pins: the two lines land in the receipt, they carry the CROSS-turn number,
and an unmeasurable week says so rather than going quiet.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cache_shape  # noqa: E402
import spend_alert  # noqa: E402


TODAY = date(2026, 9, 7)
DAY = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _sparse_turns(n: int = 12) -> list[dict]:
    """The finding's shape: 18-minute gaps, 45k rewritten from cold every turn.

    ``cache_read_tokens: 0`` on a cold turn is the shipped cold-miss rule
    (``cache_shape.is_prefix_rewarm``), shared with the census so the receipt
    and the deeper report cannot count the same event by two rules.
    """
    return [
        {
            "session_id": "s",
            "ts": (DAY + timedelta(minutes=18 * i)).isoformat(),
            "cache_write_tokens": 45_000,
            "cache_read_tokens": 0,
        }
        for i in range(n)
    ]


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[dict] = []

    def _fake_dispatch(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", _fake_dispatch)
    monkeypatch.setattr(
        spend_alert, "_weekly_spend", lambda *_a, **_k: (11.5, {"bot_a": 11.5}),
    )
    return calls


def _breakdown(dispatched: list[dict]) -> str:
    summary = next(
        c for c in dispatched if c["catalog_event"] == "cost.weekly_summary"
    )
    return summary["payload"]["per_bot_breakdown"]


def test_receipt_carries_the_cross_turn_lines(tmp_path, monkeypatch, dispatched):
    monkeypatch.setattr(
        cache_shape, "load_turn_records", lambda bot_id, **kw: _sparse_turns(),
    )
    spend_alert._maybe_send_weekly_summary(tmp_path, ["bot_a"], TODAY, 20.0, {})
    breakdown = _breakdown(dispatched)
    # 0%, not the 92% a token-weighted read of the same rows would give.
    assert "cross-turn cache hit: 0%" in breakdown
    assert "prefix re-warms:" in breakdown
    assert "per active hour" in breakdown


def test_receipt_reports_a_warm_week_as_warm(tmp_path, monkeypatch, dispatched):
    rows = [
        {
            "session_id": "s",
            "ts": (DAY + timedelta(seconds=20 * i)).isoformat(),
            "cache_write_tokens": 45_000 if i == 0 else 0,
        }
        for i in range(30)
    ]
    monkeypatch.setattr(cache_shape, "load_turn_records", lambda bot_id, **kw: rows)
    spend_alert._maybe_send_weekly_summary(tmp_path, ["bot_a"], TODAY, 20.0, {})
    assert "cross-turn cache hit: 100%" in _breakdown(dispatched)


def test_unreadable_turns_say_so_rather_than_going_quiet(
    tmp_path, monkeypatch, dispatched,
):
    monkeypatch.setattr(cache_shape, "load_turn_records", lambda bot_id, **kw: None)
    spend_alert._maybe_send_weekly_summary(tmp_path, ["bot_a"], TODAY, 20.0, {})
    assert "cross-turn cache hit: turns unreadable" in _breakdown(dispatched)


def test_an_unimportable_measurer_is_named_not_dropped(
    tmp_path, monkeypatch, dispatched,
):
    """Same posture as ``_accuracy_lines``: a missing measurement is a line,
    not a silence that reads as "checked and fine"."""
    monkeypatch.setitem(sys.modules, "cache_shape", None)
    spend_alert._maybe_send_weekly_summary(tmp_path, ["bot_a"], TODAY, 20.0, {})
    assert "cross-turn cache hit: measurement unavailable" in _breakdown(dispatched)


def test_a_raising_measurer_is_named_not_dropped(
    tmp_path, monkeypatch, dispatched,
):
    def _boom(bot_id, **kw):
        raise RuntimeError("turns dir vanished")

    monkeypatch.setattr(cache_shape, "load_turn_records", _boom)
    spend_alert._maybe_send_weekly_summary(tmp_path, ["bot_a"], TODAY, 20.0, {})
    assert "cross-turn cache hit: measurement failed" in _breakdown(dispatched)


def test_the_spend_lines_still_come_first(tmp_path, monkeypatch, dispatched):
    """The cache lines are an addition to the receipt, not a replacement —
    the per-bot spend and the estimate-vs-bill line keep their places."""
    monkeypatch.setattr(
        cache_shape, "load_turn_records", lambda bot_id, **kw: _sparse_turns(),
    )
    spend_alert._maybe_send_weekly_summary(tmp_path, ["bot_a"], TODAY, 20.0, {})
    lines = _breakdown(dispatched).splitlines()
    assert lines[0].strip().startswith("bot_a:")
    assert any(ln.startswith("estimate vs provider bill:") for ln in lines)
    assert lines.index(
        next(ln for ln in lines if ln.startswith("cross-turn cache hit:"))
    ) > lines.index(
        next(ln for ln in lines if ln.startswith("estimate vs provider bill:"))
    )
