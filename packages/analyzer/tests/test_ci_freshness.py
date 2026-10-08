"""tests/test_ci_freshness.py — tools/ci_freshness.py's Verdict classifier
(D-CS7), adopted by the secret-history-scan freshness dead-man's-switch.

Fixtures for the known-good / known-bad pair this control registers under
tests/fixtures/controls/workflow_secret-history-scan.yml_freshness/.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ANALYZER_DIR = _REPO_ROOT / "packages" / "analyzer"
_TOOLS_DIR = _REPO_ROOT / "tools"
for _p in (_ANALYZER_DIR, _TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ci_freshness import LatestRun, evaluate_freshness  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "controls" / "workflow_secret-history-scan.yml_freshness"


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_from(payload: dict, days_ago: float) -> LatestRun | None:
    lr = payload.get("latest_run")
    if lr is None:
        return None
    return LatestRun(id=lr["id"], created_at=_iso(days_ago), url=lr["url"])


def _load(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text())


def test_known_good_fixture_is_ok():
    payload = _load("known_good.json")
    v = evaluate_freshness(
        workflow="secret-history-scan.yml", job_name="Full-history secret scan",
        sched_reachable=payload["sched_reachable"], disp_reachable=payload["disp_reachable"],
        any_runs_found=payload["any_runs_found"],
        latest_run=_run_from(payload, payload["latest_run_age_days"]),
        max_age_days=16,
    )
    assert v.is_ok
    assert v.subject is not None


def test_known_bad_fixture_must_produce_broken():
    payload = _load("known_bad.json")
    v = evaluate_freshness(
        workflow="secret-history-scan.yml", job_name="Full-history secret scan",
        sched_reachable=payload["sched_reachable"], disp_reachable=payload["disp_reachable"],
        any_runs_found=payload["any_runs_found"],
        latest_run=_run_from(payload, payload["latest_run_age_days"]),
        max_age_days=16,
    )
    assert v.is_broken
    assert v.subject is not None  # the stale run IS the subject, not nothing


def test_empty_source_yields_unknown_not_ok():
    """No completed run at all, but the API itself was reachable — this is
    'broken' (we looked, nothing is there), never 'ok'."""
    v = evaluate_freshness(
        workflow="secret-history-scan.yml", job_name="Full-history secret scan",
        sched_reachable=True, disp_reachable=True, any_runs_found=False, latest_run=None,
        max_age_days=16,
    )
    assert not v.is_ok
    assert v.is_broken


def test_paginated_away_subject_is_unknown_not_never_run():
    """The #4244/#4253 shape, one field down: if only ONE of the two
    per-event queries could be read, the side that failed must never be
    treated as 'zero runs' just because the other one succeeded — that is
    the exact conflation ('not fetched' == 'does not exist') that let the
    original bug read as a dead scan for hours. Must be `unknown`, never
    `broken`/never-run, regardless of what the reachable side found."""
    payload = _load("known_bad_partial_unreachable.json")
    v = evaluate_freshness(
        workflow="secret-history-scan.yml", job_name="Full-history secret scan",
        sched_reachable=payload["sched_reachable"], disp_reachable=payload["disp_reachable"],
        any_runs_found=payload["any_runs_found"],
        latest_run=_run_from(payload, payload["latest_run_age_days"]),
        max_age_days=16,
    )
    assert v.is_unknown
    assert v.subject is None
    assert "schedule" in v.reason


def test_total_api_failure_is_unknown():
    v = evaluate_freshness(
        workflow="secret-history-scan.yml", job_name="Full-history secret scan",
        sched_reachable=False, disp_reachable=False, any_runs_found=False, latest_run=None,
        max_age_days=16,
    )
    assert v.is_unknown
    assert v.subject is None
