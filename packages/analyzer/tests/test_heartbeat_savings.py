"""heartbeat_savings — the idle-burn saving as a number, not an absence.

D-OH3 removes model calls from heartbeats and cron ticks with nothing due.
That saving looks exactly like a heartbeat that silently died, so the plugin
writes a decision record for every tick either way and these tests pin what
the receipt makes of them — including the two states an honest receipt has to
render that a naive one would collapse into "$0.00 saved": nothing skipped,
and nothing on this pod to price a skip against.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import heartbeat_savings as hs  # noqa: E402


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _decision(bot: str, outcome: str, day: str = "2026-09-07") -> dict:
    return {
        "schema_version": 1,
        "ts": f"{day}T09:00:00.000Z",
        "instance": bot,
        "source": "heartbeat",
        "channel": "heartbeat",
        "outcome": outcome,
        "model": None,
        "provider": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost": 0,
        "cost_source": "no_model_call",
        "session_id": None,
        "run_id": None,
        "conditions_evaluated": 3,
        "due_ids": [] if outcome == hs.SKIPPED else ["inbox"],
        "reason": "nothing due (3 conditions checked)",
    }


def _write_decisions(shared: Path, bot: str, outcomes: list[str], day: str = "2026-09-07") -> None:
    d = shared / bot / "turns"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{hs.DECISIONS_PREFIX}{day}.jsonl"
    with path.open("a") as fh:
        for outcome in outcomes:
            fh.write(json.dumps(_decision(bot, outcome, day)) + "\n")


def _turns(costs: list[float | None], source: str = "heartbeat") -> list[dict]:
    return [
        {"source": source, "channel": source, "cost": c, "model": "anthropic/claude-haiku-4-5"}
        for c in costs
    ]


# ── reading ────────────────────────────────────────────────────────────────


def test_reads_only_the_window(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED], day="2026-09-07")
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED], day="2026-08-01")
    recs = hs.read_decisions(tmp_path, "bot_a", days=7, end_date=NOW)
    assert len(recs) == 1


def test_a_truncated_line_does_not_kill_the_read(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED, hs.WOKE])
    path = tmp_path / "bot_a" / "turns" / f"{hs.DECISIONS_PREFIX}2026-09-07.jsonl"
    with path.open("a") as fh:
        fh.write('{"outcome": "skipped_noth')  # a half-written append
    recs = hs.read_decisions(tmp_path, "bot_a", days=7, end_date=NOW)
    assert [r["outcome"] for r in recs] == [hs.SKIPPED, hs.WOKE]


def test_a_bot_with_no_ledger_is_simply_absent(tmp_path: Path) -> None:
    assert hs.read_decisions(tmp_path, "never_deployed", days=7, end_date=NOW) == []


# ── measuring ──────────────────────────────────────────────────────────────


def test_skips_are_priced_at_the_median_run(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED] * 22 + [hs.WOKE] * 2)
    m = hs.measure(
        tmp_path,
        ["bot_a"],
        days=7,
        end_date=NOW,
        load_turns=lambda *_a, **_k: _turns([0.02, 0.03, 0.04]),
    )
    assert m.skipped == 22
    assert m.woke == 2
    assert m.ran == 3
    assert m.median_run_usd == pytest.approx(0.03)
    assert m.saved_usd == pytest.approx(0.66)
    assert m.bots_with_conditions == ("bot_a",)


def test_the_median_is_not_dragged_by_one_runaway_session(tmp_path: Path) -> None:
    """A 40-turn retry storm must not value every skip at storm prices."""
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED] * 10)
    m = hs.measure(
        tmp_path,
        ["bot_a"],
        days=7,
        end_date=NOW,
        load_turns=lambda *_a, **_k: _turns([0.02, 0.02, 0.03, 3.40]),
    )
    assert m.median_run_usd == pytest.approx(0.025)
    assert m.saved_usd == pytest.approx(0.25)


def test_no_priced_run_means_unknown_savings_not_zero(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED] * 5)
    m = hs.measure(
        tmp_path,
        ["bot_a"],
        days=7,
        end_date=NOW,
        load_turns=lambda *_a, **_k: _turns([None, None]),
    )
    assert m.skipped == 5
    assert m.median_run_usd is None
    assert m.saved_usd is None
    assert "price a skip against" in m.unpriced_reason


def test_nothing_skipped_is_a_real_zero(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.WOKE] * 3)
    m = hs.measure(
        tmp_path,
        ["bot_a"],
        days=7,
        end_date=NOW,
        load_turns=lambda *_a, **_k: _turns([0.02]),
    )
    assert m.skipped == 0
    assert m.saved_usd == 0.0


def test_unreadable_turns_still_report_the_skip_count(tmp_path: Path) -> None:
    def _boom(*_a, **_k):
        raise OSError("turns dir unreadable")

    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED] * 4)
    m = hs.measure(tmp_path, ["bot_a"], days=7, end_date=NOW, load_turns=_boom)
    assert m.skipped == 4
    assert m.ran is None
    assert m.saved_usd is None


def test_only_clock_fired_turns_are_counted(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED])
    turns = _turns([0.02], source="heartbeat") + _turns([3.20], source="human")
    m = hs.measure(tmp_path, ["bot_a"], days=7, end_date=NOW, load_turns=lambda *_a, **_k: turns)
    assert m.ran == 1
    assert m.median_run_usd == pytest.approx(0.02)


def test_cron_labelled_turns_count_as_scheduled() -> None:
    assert hs.is_scheduled_turn({"source": "cron", "channel": "unknown"})
    assert hs.is_scheduled_turn({"source": "unknown", "channel": "cron-event"})
    assert hs.is_scheduled_turn({"source": "cron_app", "channel": ""})
    assert not hs.is_scheduled_turn({"source": "human", "channel": "telegram"})


# ── the receipt line ───────────────────────────────────────────────────────


def test_receipt_names_the_saving(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED] * 22 + [hs.WOKE] * 2)
    (line,) = hs.receipt_lines(
        tmp_path, ["bot_a"], days=7, end_date=NOW,
        load_turns=lambda *_a, **_k: _turns([0.02, 0.03, 0.04]),
    )
    assert "22 skipped" in line
    assert "$0.66 saved" in line


def test_receipt_on_a_pod_with_no_conditions_file_says_so(tmp_path: Path) -> None:
    (line,) = hs.receipt_lines(
        tmp_path, ["bot_a", "bot_b"], days=7, end_date=NOW,
        load_turns=lambda *_a, **_k: _turns([0.02] * 24),
    )
    assert "no bot has a HEARTBEAT.json yet" in line
    assert "0 skipped" in line


def test_receipt_says_unknown_rather_than_zero_when_unpriced(tmp_path: Path) -> None:
    _write_decisions(tmp_path, "bot_a", [hs.SKIPPED] * 3)
    (line,) = hs.receipt_lines(
        tmp_path, ["bot_a"], days=7, end_date=NOW,
        load_turns=lambda *_a, **_k: _turns([None]),
    )
    assert "$ saved unknown" in line
    assert "$0.00" not in line


def test_receipt_is_always_exactly_one_line(tmp_path: Path) -> None:
    for loader in (
        lambda *_a, **_k: _turns([0.02]),
        lambda *_a, **_k: [],
    ):
        lines = hs.receipt_lines(tmp_path, ["bot_a"], days=7, end_date=NOW, load_turns=loader)
        assert len(lines) == 1
