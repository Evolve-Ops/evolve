"""Housekeeping on its own receipt line — compaction-and-memory-flush-on-cheap-rung §4.

The memory flush is a model turn nobody asked for (the finding's was ~$1.90 on
the power model: ``internal/finding-cost-forensics-power-bot-2026-09-04.md``
§2). The plugin tags it ``source: "memory_flush"``; the weekly receipt shows a
bot's conversation cost on its line and housekeeping on a second line under it,
while the pod total stays every dollar.

Also pins the lever reader (``cost_levers``) and the trigger-kind pass-through
the cost_event converter needs for the tag to survive the rollup.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cost_event_converter  # noqa: E402
import cost_levers  # noqa: E402
import housekeeping_cost  # noqa: E402
import live_spend  # noqa: E402
import spend_alert  # noqa: E402
from housekeeping_cost import HousekeepingSpend, is_housekeeping_turn, receipt_bot_lines  # noqa: E402

NOW = datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc)


def _row(source: str, usd: float, hours_ago: int = 2) -> dict:
    return {
        "ts": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "source": source,
        "model": "anthropic/claude-haiku-4-5",
        "cost": usd,
        "cost_source": "provider",
        "input_tokens": 26, "output_tokens": 900,
        "cache_write_tokens": 41_000, "cache_read_tokens": 0,
    }


# ── Classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("turn,want", [
    ({"source": "memory_flush"}, True),
    ({"source": "memory"}, True),          # pre-fix rows: OC's raw trigger
    ({"source": "compaction"}, True),
    ({"trigger_kind": "memory_flush"}, True),
    ({"source": "human"}, False),
    ({"source": "heartbeat"}, False),
    ({"source": "summarizer"}, False),     # Evolve overhead, not OC housekeeping
    ({}, False),
])
def test_is_housekeeping_turn(turn, want):
    assert is_housekeeping_turn(turn) is want


def test_cost_event_trigger_kind_passes_housekeeping_through():
    infer = cost_event_converter._infer_trigger_kind
    assert infer("memory_flush", "telegram") == "memory_flush"
    assert infer("compaction", None) == "compaction"
    assert infer("memory", "telegram") == "memory_flush"
    assert infer("human", "telegram") == "user_turn"


# ── The receipt lines ───────────────────────────────────────────────────────


def test_conversation_line_excludes_housekeeping_and_a_second_line_shows_it():
    lines = receipt_bot_lines("team_bot_a", 12.71, HousekeepingSpend(usd=0.31))
    assert lines[0] == "  team_bot_a: $12.40"
    assert lines[1] == f"    {housekeeping_cost.RECEIPT_LABEL}: $0.31"


def test_no_housekeeping_keeps_one_line():
    assert receipt_bot_lines("team_bot_a", 4.2, HousekeepingSpend(usd=0.0)) == ["  team_bot_a: $4.20"]


def test_unreadable_housekeeping_says_so_and_keeps_the_total():
    lines = receipt_bot_lines("team_bot_a", 4.2, None)
    assert lines[0] == "  team_bot_a: $4.20"
    assert "unreadable" in lines[1]


def test_unreadable_bot_is_na():
    assert receipt_bot_lines("team_bot_a", None, None) == ["  team_bot_a: n/a (could not read turns)"]


def test_unpriced_housekeeping_is_labelled_a_floor():
    lines = receipt_bot_lines("team_bot_a", 3.0, HousekeepingSpend(usd=0.5, measurable=False))
    assert "floor" in lines[1]


# ── Window: the same pod-local days as the weekly total ─────────────────────


def test_housekeeping_over_local_days_sums_only_housekeeping(monkeypatch):
    rows = [_row("human", 2.00), _row("memory_flush", 0.25), _row("memory", 0.05),
            _row("memory_flush", 9.99, hours_ago=24 * 30)]  # outside the window
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *_a, **_k: list(rows))
    got = housekeeping_cost.housekeeping_over_local_days("team_bot_a", days=7, now=NOW)
    assert got == HousekeepingSpend(usd=0.30, measurable=True)


def test_housekeeping_load_failure_is_none_not_zero(monkeypatch):
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *_a, **_k: live_spend.LIVE_LOAD_FAILED)
    assert housekeeping_cost.housekeeping_over_local_days("team_bot_a", now=NOW) is None


# ── End to end through the weekly summary ───────────────────────────────────


def test_weekly_receipt_splits_housekeeping_under_its_bot(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(spend_alert, "_weekly_spend", lambda *_a, **_k: (12.71, {"team_bot_a": 12.71}))
    monkeypatch.setattr(
        housekeeping_cost, "housekeeping_over_local_days",
        lambda bot_id, **_k: HousekeepingSpend(usd=0.31),
    )
    spend_alert._maybe_send_weekly_summary(tmp_path, ["team_bot_a"], date(2026, 9, 7), 20.0, {})
    summary = next(c for c in calls if c["catalog_event"] == "cost.weekly_summary")
    assert summary["payload"]["total"] == 12.71, "the pod total stays every dollar"
    breakdown = summary["payload"]["per_bot_breakdown"]
    assert "  team_bot_a: $12.40\n" in breakdown
    assert f"    {housekeeping_cost.RECEIPT_LABEL}: $0.31" in breakdown


# ── Levers (D-CS5) ──────────────────────────────────────────────────────────


LEVER = cost_levers.HOUSEKEEPING_CHEAP_RUNG


@pytest.mark.parametrize("network,want", [
    ({}, True),
    (None, True),
    ({"cost": {"levers": {LEVER: False}}}, False),
    ({"bots": {"b": {"cost": {"levers": {LEVER: False}}}}}, False),
    ({"cost": {"levers": {LEVER: False}}, "bots": {"b": {"cost": {"levers": {LEVER: True}}}}}, True),
    ({"bots": {"b": {"cost": {"levers": {LEVER: "no"}}}}}, True),   # a typo is not an opt-out
    ({"bots": {"other": {"cost": {"levers": {LEVER: False}}}}}, True),
])
def test_lever_enabled_matches_the_plugin_rule(network, want):
    """Same cases as housekeepingCheapRung.test.mjs's leverEnabled test —
    the gateway and deploy read one file and must agree."""
    assert cost_levers.lever_enabled(network, "b", LEVER) is want


def test_levers_for_bot_lists_every_known_lever():
    assert cost_levers.levers_for_bot({}, "b") == {LEVER: True}
