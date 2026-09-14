"""Tests for spend_alert's operator-confirmed-install exemption (2026-06-03).

When a bot has ``forge_install_exempt_from_daily_cap=True`` (the default),
turns tagged ``forge_subkind="operator_confirmed_install"`` are filtered
out of ``load_today_spend`` and ``burst_window_spend``. The operator
already saw the projected cost and confirmed; a tight ``daily_cap_usd``
should protect against background runaway, not informed action.

Tests bypass the live-turns discovery via direct monkeypatching of
``_load_live_turns`` so each scenario can shape its own turn fixture.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import spend_alert  # noqa: E402
import live_spend  # noqa: E402


def _confirmed_turn(ts: str, cost: float) -> dict:
    return {
        "ts": ts,
        "cost": cost,
        "source": "forge",
        "channel": "unknown",
        "forge_subkind": "operator_confirmed_install",
        "model": "anthropic/claude-sonnet-4-6",
    }


def _regular_turn(ts: str, cost: float) -> dict:
    return {
        "ts": ts,
        "cost": cost,
        "source": "human",
        "channel": "telegram",
        "model": "anthropic/claude-sonnet-4-6",
    }


# ── load_today_spend ────────────────────────────────────────────────────


def test_load_today_spend_includes_all_turns_when_no_exemption(monkeypatch, tmp_path):
    today = date(2026, 6, 3)
    turns = [
        _regular_turn("2026-06-03T10:00:00Z", 4.0),
        _confirmed_turn("2026-06-03T11:00:00Z", 25.0),
    ]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)
    total = spend_alert.load_today_spend(
        tmp_path, "team_bot_c", today, now=datetime(2026, 6, 3, 12, tzinfo=timezone.utc),
    )
    assert total == 29.0


def test_load_today_spend_skips_exempt_subkind(monkeypatch, tmp_path):
    today = date(2026, 6, 3)
    turns = [
        _regular_turn("2026-06-03T10:00:00Z", 4.0),
        _confirmed_turn("2026-06-03T11:00:00Z", 25.0),
    ]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)
    total = spend_alert.load_today_spend(
        tmp_path, "team_bot_c", today,
        now=datetime(2026, 6, 3, 12, tzinfo=timezone.utc),
        exempt_subkinds={"operator_confirmed_install"},
    )
    # Only the regular turn counts.
    assert total == 4.0


def test_load_today_spend_returns_none_on_discovery_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: live_spend.LIVE_LOAD_FAILED)
    assert spend_alert.load_today_spend(
        tmp_path, "team_bot_c", date(2026, 6, 3), exempt_subkinds={"operator_confirmed_install"},
    ) is None


# ── burst_window_spend ──────────────────────────────────────────────────


def test_burst_window_skips_exempt_subkind(monkeypatch):
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    turns = [
        _regular_turn("2026-06-03T11:30:00Z", 2.0),
        _confirmed_turn("2026-06-03T11:35:00Z", 20.0),
    ]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)
    total, selected = spend_alert.burst_window_spend(
        "team_bot_c", now=now, window_minutes=60,
        exempt_subkinds={"operator_confirmed_install"},
    )
    assert total == 2.0
    assert len(selected) == 1


def test_burst_window_includes_exempt_subkind_when_no_filter(monkeypatch):
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    turns = [
        _regular_turn("2026-06-03T11:30:00Z", 2.0),
        _confirmed_turn("2026-06-03T11:35:00Z", 20.0),
    ]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)
    total, selected = spend_alert.burst_window_spend(
        "team_bot_c", now=now, window_minutes=60,
    )
    assert total == 22.0
    assert len(selected) == 2
