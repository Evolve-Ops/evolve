"""Health control "in-turn checkpoint armed" (D-CS12) and its evidence reader.

The plugin writes the status file and ledger this reads
(packages/plugin/src/breakers/InTurnCheckpoint.ts); the reader is
packages/analyzer/breakers/in_turn.py.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ADMIN = Path(__file__).resolve().parents[1]
_ANALYZER = Path(__file__).resolve().parents[2] / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve_admin import health  # noqa: E402
from breakers import in_turn  # noqa: E402

BOT = "team_bot_a"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "controls" / "health__check_in_turn_checkpoint"


def _write(shared: Path, status: dict | None, rows: list[dict] = ()) -> None:
    d = shared / "breakers" / BOT
    d.mkdir(parents=True, exist_ok=True)
    if status is not None:
        (d / in_turn.STATUS_FILENAME).write_text(json.dumps(status))
    if rows:
        (d / in_turn.LEDGER_FILENAME).write_text("".join(json.dumps(r) + "\n" for r in rows))


def _run(shared: Path) -> health.CheckResult:
    report = health.HealthReport()
    health._check_in_turn_checkpoint(report, shared, [BOT], now=NOW)
    (only,) = [c for c in report.checks if c.category == "in_turn_checkpoint"]
    return only


def _status(**over) -> dict:
    base = {"armed": True, "threshold_usd": 5, "price_source": "catalog", "spend_runs": 3,
            "multi_call_runs": 2, "evaluated_runs": 2, "unknown_evaluations": 0, "trips": 0,
            "zero_cost_calls": 0}
    return {**base, **over}


def _replay(tmp_path: Path, name: str) -> tuple[health.CheckResult, dict]:
    fx = json.loads((_FIXTURES / name).read_text())
    _write(tmp_path, fx["status"])
    return _run(tmp_path), fx["expect"]


def test_known_good_fixture_passes(tmp_path: Path) -> None:
    c, want = _replay(tmp_path, "known_good.json")
    assert c.status == health.PASS and want["detail_contains"] in c.detail


def test_known_bad_fixture_warns_unknown(tmp_path: Path) -> None:
    c, want = _replay(tmp_path, "known_bad.json")
    assert c.status == health.WARN and want["detail_contains"] in c.detail


def test_absent_status_is_unknown(tmp_path: Path) -> None:
    c = _run(tmp_path)
    assert c.status == health.WARN and "unknown" in c.detail


def test_not_registered_warns(tmp_path: Path) -> None:
    _write(tmp_path, _status(armed=False))
    assert "NOT armed" in _run(tmp_path).detail


def test_unpriced_calls_warn_and_name_the_price_source(tmp_path: Path) -> None:
    _write(tmp_path, _status(zero_cost_calls=4))
    c = _run(tmp_path)
    assert c.status == health.WARN and "priced $0 (no catalog row)" in c.detail


def test_pass_counts_this_weeks_trips_only(tmp_path: Path) -> None:
    rows = [
        {"event": "checkpoint_in_turn", "ts": "2026-09-20T20:09:10Z", "spend_usd": 5.0},
        {"event": "owner_notice", "ts": "2026-09-20T20:09:30Z", "delivery": "conversation"},
        {"event": "checkpoint_in_turn", "ts": "2026-09-01T10:00:00Z", "spend_usd": 5.2},
        "not json",
    ]
    d = tmp_path / "breakers" / BOT
    d.mkdir(parents=True)
    (d / in_turn.STATUS_FILENAME).write_text(json.dumps(_status()))
    (d / in_turn.LEDGER_FILENAME).write_text(
        "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
    c = _run(tmp_path)
    assert c.status == health.PASS
    assert "in-turn checkpoints: 1 this week" in c.detail
    assert in_turn.count_by_bot(tmp_path, [BOT, "team_bot_b"], now=NOW) == {BOT: 1}
