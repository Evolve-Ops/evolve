"""Health control "no-progress gate armed and live" and its evidence reader.

The plugin writes the status file and ledger this reads
(packages/plugin/src/breakers/NoProgressGate.ts); the reader is
packages/analyzer/breakers/no_progress.py.
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
from breakers import no_progress  # noqa: E402

BOT = "team_bot_a"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "controls" / "health__check_no_progress_gate"


def _write(shared: Path, status: dict | None, rows: list[dict] = ()) -> None:
    d = shared / "breakers" / BOT
    d.mkdir(parents=True, exist_ok=True)
    if status is not None:
        (d / no_progress.STATUS_FILENAME).write_text(json.dumps(status))
    if rows:
        (d / no_progress.LEDGER_FILENAME).write_text("".join(json.dumps(r) + "\n" for r in rows))


def _run(shared: Path) -> health.CheckResult:
    report = health.HealthReport()
    health._check_no_progress_gate(report, shared, [BOT], now=NOW)
    (only,) = [c for c in report.checks if c.category == "no_progress_gate"]
    return only


def _status(**over) -> dict:
    base = {"armed": True, "identical_calls": 3, "failures": 3, "max_model_calls_per_run": None,
            "evaluated_calls": 5, "unknown_evaluations": 0, "results_observed": 5,
            "results_with_status": 2, "stops": 0}
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


def test_pass_names_stops_predicate_and_ceiling(tmp_path: Path) -> None:
    _write(tmp_path, _status(max_model_calls_per_run=40), [
        {"event": "no_progress", "ts": "2026-09-25T10:00:00Z", "tool": "subagents"},
        {"event": "no_progress", "ts": "2026-09-01T10:00:00Z", "tool": "exec"},  # outside 7d
    ])
    c = _run(tmp_path)
    assert c.status == health.PASS
    assert "1 runs stopped this week" in c.detail
    assert "failing-validation check evaluated" in c.detail
    assert "model-call ceiling 40" in c.detail


def test_results_without_status_says_not_evaluated(tmp_path: Path) -> None:
    _write(tmp_path, _status(results_with_status=0))
    assert "not evaluated" in _run(tmp_path).detail


def test_controls_by_bot(tmp_path: Path) -> None:
    _write(tmp_path, _status(results_with_status=0, max_model_calls_per_run=0))
    assert no_progress.controls_by_bot(tmp_path, [BOT, "other"]) == {
        BOT: {"failing_validation": "not_evaluated", "max_model_calls_per_run": None},
        "other": {"failing_validation": "unknown", "max_model_calls_per_run": None},
    }
