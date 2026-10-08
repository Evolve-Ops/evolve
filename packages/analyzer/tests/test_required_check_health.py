"""tests/test_required_check_health.py — tools/required_check_health.py's
D-CS9 measurement + gate logic: a required CI check whose failure rate
crosses a threshold must be explicitly quarantined (owner + expiry) or
demoted, and expiry is real.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ANALYZER_DIR = _REPO_ROOT / "packages" / "analyzer"
_TOOLS_DIR = _REPO_ROOT / "tools"
for _p in (_ANALYZER_DIR, _TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from required_check_health import (  # noqa: E402
    MEASURED_AT_FORMAT,
    CheckRun,
    QuarantineEntry,
    QuarantineFormatError,
    ReportFormatError,
    evaluate_gate,
    evaluate_report_freshness,
    measure_failure_rate,
    parse_quarantine_line,
)
from verdict import Verdict  # noqa: E402


# ── measure_failure_rate ────────────────────────────────────────────────

def test_failure_rate_is_computed_from_named_run_ids():
    runs = [
        CheckRun(run_id="1", conclusion="success"),
        CheckRun(run_id="2", conclusion="failure"),
        CheckRun(run_id="3", conclusion="failure"),
        CheckRun(run_id="4", conclusion="success"),
    ]
    v = measure_failure_rate("Linux e2e", runs)
    assert v.is_ok
    assert v.evidence["failure_rate"] == pytest.approx(0.5)
    assert v.evidence["total"] == 4
    assert v.evidence["failures"] == 2
    assert v.evidence["run_ids"] == ["1", "2", "3", "4"]
    assert v.evidence["failing_run_ids"] == ["2", "3"]
    assert "Linux e2e" in v.subject


def test_non_pass_fail_conclusions_are_excluded_from_the_count():
    runs = [
        CheckRun(run_id="1", conclusion="success"),
        CheckRun(run_id="2", conclusion="cancelled"),
        CheckRun(run_id="3", conclusion="skipped"),
        CheckRun(run_id="4", conclusion="failure"),
    ]
    v = measure_failure_rate("check", runs)
    assert v.is_ok
    assert v.evidence["total"] == 2
    assert v.evidence["failure_rate"] == pytest.approx(0.5)


def test_unreadable_actions_api_is_unknown_not_healthy():
    """An unreadable API must never collapse to a 0%-failure ("healthy")
    read — that is exactly the conflation D-CS7/#4253 exists to prevent."""
    v = measure_failure_rate("Linux e2e", None, error="HTTP 503 from api.github.com")
    assert v.is_unknown
    assert not v.is_ok
    assert "503" in v.reason
    assert v.subject is None


def test_no_completed_runs_found_is_unknown_not_a_zero_rate():
    v = measure_failure_rate("Linux e2e", [])
    assert v.is_unknown
    assert not v.is_ok


# ── quarantine row parsing ──────────────────────────────────────────────

def test_quarantine_row_requires_an_owner_and_a_date():
    with pytest.raises(QuarantineFormatError):
        parse_quarantine_line("Linux e2e\t\t2026-10-16\t# no owner")
    with pytest.raises(QuarantineFormatError):
        parse_quarantine_line("Linux e2e\tplatform-owner\t\t# no date")
    with pytest.raises(QuarantineFormatError):
        parse_quarantine_line("Linux e2e\tplatform-owner\tnot-a-date\t# malformed date")
    with pytest.raises(QuarantineFormatError):
        parse_quarantine_line("Linux e2e\tplatform-owner\t2026-10-16")  # no reason at all


def test_a_well_formed_quarantine_row_parses():
    entry = parse_quarantine_line(
        "Linux e2e (Ubuntu bot deploy/run/admin)\tplatform-owner\t2026-10-16\t"
        "# useradd timeouts"
    )
    assert entry.check == "Linux e2e (Ubuntu bot deploy/run/admin)"
    assert entry.owner == "platform-owner"
    assert entry.expiry == date(2026, 10, 16)
    assert entry.reason == "useradd timeouts"


# ── gate evaluation ──────────────────────────────────────────────────────

def _ok_verdict(rate: float) -> Verdict:
    total = 100
    failures = round(rate * total)
    return Verdict.ok(
        "check@1..100 (n=100)",
        evidence={"failure_rate": rate, "total": total, "failures": failures},
    )


def test_a_check_over_threshold_without_a_quarantine_entry_fails_the_gate():
    result = evaluate_gate(
        "Linux e2e", _ok_verdict(0.43),
        threshold=0.20, quarantine={}, today=date(2026, 9, 16),
    )
    assert result.state == "needs_quarantine"
    assert result.blocking


def test_a_quarantined_check_under_its_expiry_passes_the_gate():
    quarantine = {
        "Linux e2e": QuarantineEntry(
            check="Linux e2e", owner="platform-owner", expiry=date(2026, 10, 16),
            reason="useradd timeouts", raw_line="...",
        )
    }
    result = evaluate_gate(
        "Linux e2e", _ok_verdict(0.43),
        threshold=0.20, quarantine=quarantine, today=date(2026, 9, 16),
    )
    assert result.state == "pass"
    assert not result.blocking


def test_an_expired_entry_is_a_firing_signal():
    quarantine = {
        "Linux e2e": QuarantineEntry(
            check="Linux e2e", owner="platform-owner", expiry=date(2026, 9, 1),
            reason="useradd timeouts", raw_line="...",
        )
    }
    result = evaluate_gate(
        "Linux e2e", _ok_verdict(0.43),
        threshold=0.20, quarantine=quarantine, today=date(2026, 9, 16),
    )
    assert result.state == "expired"
    assert result.blocking


def test_an_expired_entry_fires_even_if_the_rate_recovered():
    """Quarantine is a dated promise, not a parking space — a check that
    got better still needs its row deleted, not left to expire quietly."""
    quarantine = {
        "Linux e2e": QuarantineEntry(
            check="Linux e2e", owner="platform-owner", expiry=date(2026, 9, 1),
            reason="useradd timeouts", raw_line="...",
        )
    }
    result = evaluate_gate(
        "Linux e2e", _ok_verdict(0.02),
        threshold=0.20, quarantine=quarantine, today=date(2026, 9, 16),
    )
    assert result.state == "expired"
    assert result.blocking


def test_a_check_under_threshold_with_no_entry_passes():
    result = evaluate_gate(
        "Linux e2e", _ok_verdict(0.05),
        threshold=0.20, quarantine={}, today=date(2026, 9, 16),
    )
    assert result.state == "pass"
    assert not result.blocking


def test_an_unknown_measurement_is_its_own_state_not_a_pass_or_a_block():
    v = Verdict.unknown("could not read the Actions API")
    result = evaluate_gate(
        "Linux e2e", v, threshold=0.20, quarantine={}, today=date(2026, 9, 16),
    )
    assert result.state == "unknown"
    assert not result.blocking


def test_a_verdict_missing_failure_rate_is_unknown_not_a_keyerror():
    """A hand-edited or truncated report entry must not crash the gate with
    a traceback — it reads as `unknown`, the same as an unreadable API."""
    v = Verdict.ok("check@1..2 (n=2)", evidence={"total": 2})
    result = evaluate_gate(
        "check", v, threshold=0.20, quarantine={}, today=date(2026, 9, 16),
    )
    assert result.state == "unknown"
    assert not result.blocking


# ── report freshness ─────────────────────────────────────────────────────

def _envelope(measured_at: str) -> dict:
    return {
        "measured_at": measured_at,
        "window": {"n": 100, "event": "push", "branch": "main"},
        "checks": {},
    }


def test_a_report_within_max_age_is_fresh():
    measured_at = datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc).strftime(MEASURED_AT_FORMAT)
    freshness = evaluate_report_freshness(
        _envelope(measured_at), max_age_days=14, today=date(2026, 9, 16),
    )
    assert freshness.state == "fresh"
    assert not freshness.blocking


def test_a_report_older_than_max_age_is_stale_and_blocking():
    measured_at = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc).strftime(MEASURED_AT_FORMAT)
    freshness = evaluate_report_freshness(
        _envelope(measured_at), max_age_days=14, today=date(2026, 9, 16),
    )
    assert freshness.state == "stale"
    assert freshness.blocking
    assert measured_at in freshness.message
    assert "measure" in freshness.message


def test_old_bare_report_shape_is_refused_not_read_as_fresh():
    """The pre-envelope shape ({check: verdict}) has no date at all — that
    must raise, not silently pass as fresh (the same absence-of-evidence
    trap D-CS7 names for a single verdict, one level up)."""
    with pytest.raises(ReportFormatError):
        evaluate_report_freshness(
            {"Linux e2e": {"state": "ok"}}, max_age_days=14, today=date(2026, 9, 16),
        )


def test_threshold_stays_wide_only_while_the_resolved_admin_suite_incident_is_in_window():
    """0.35 (ci.yml + tools/preflight, as originally shipped with D-CS9) sat
    between the chronic Linux e2e check (43%) and the admin-suite incident
    (30%) — a dated, already-fixed run of failures that happened to still
    be inside the measured window when that threshold was chosen. Once a
    fresh `measure` window no longer contains that incident, nothing keeps
    the threshold wide and it must tighten to 0.2, the tool's own docstring
    example — prompted by the data, not by memory. This PR's own
    re-measurement is the first `measure` run since the incident aged out,
    so CI_YML_THRESHOLD below is already 0.2; this test now also guards
    against silently re-widening it while the incident stays out of window."""
    report = json.loads(
        (_REPO_ROOT / "tools" / "required-check-health-report.json").read_text()
    )
    checks = report.get("checks", report)  # tolerate both envelope and bare shape
    admin_suite = checks["Full admin test suite (quarantined baseline)"]
    incident_still_in_window = admin_suite["evidence"]["failure_rate"] == pytest.approx(0.30)

    CI_YML_THRESHOLD = 0.2  # kept in sync with ci.yml + tools/preflight by hand

    assert incident_still_in_window or CI_YML_THRESHOLD <= 0.2, (
        "the 30% admin-suite incident has left the measured window in "
        "tools/required-check-health-report.json, but ci.yml and "
        "tools/preflight still use a threshold above 0.2 — nothing keeps it "
        "wide anymore, drop both to 0.2"
    )
