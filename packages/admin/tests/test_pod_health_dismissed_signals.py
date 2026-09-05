"""test_pod_health_dismissed_signals.py — Pod Health surface respects Signal dismissals.

Bug: dismissing a pod_health Signal in Reports → Alerts had no effect on the Pod
Health surface.  Root cause: the two surfaces read different stores with no linkage
— health.run_health_check() built a HealthReport from live checks and returned it
without cross-referencing the Signal store.

Fix (option a): after a health scan, _apply_signal_dismissals() looks up every
FAIL/WARN check's signature in the Signal store and marks checks whose Signal is
dismissed or snoozed with check.dismissed = True.  The report summary excludes
dismissed checks from the fail/warn counts so the Pod Health surface agrees with
the Alerts page after an operator dismisses an alert there.

filter_dismissed_checks() does the same for the cached-JSON path (the
/api/pod-health/cached endpoint reads health-latest.json from disk; a dismissal
that happened since the last scan must still be reflected without a fresh scan).

Tests pin:
  - dismissed Signal → check.dismissed=True, excluded from report.failed
  - snoozed Signal  → same treatment
  - firing (not dismissed) Signal → check.dismissed=False, still counted
  - Signal from a different producer does not suppress a pod_health check
  - HealthReport.as_dict() carries dismissed flag and correct summary counts
  - filter_dismissed_checks() applies the same logic to a raw JSON dict
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.health import (  # noqa: E402
    FAIL,
    PASS,
    WARN,
    CheckResult,
    HealthReport,
    _apply_signal_dismissals,
    filter_dismissed_checks,
)
import signals.store as signals_store  # noqa: E402
from schema.signal import make_signature  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    d = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (d / "signals" / sub).mkdir(parents=True)
    return d


# ── helpers ───────────────────────────────────────────────────────────────────


def _sig_for(check: CheckResult) -> str:
    return make_signature("pod_health", f"pod_health_{check.category}", check.name)


def _observe_check(shared: Path, check: CheckResult, **kwargs) -> signals_store.Signal if False else object:
    return signals_store.observe(
        shared,
        signature=_sig_for(check),
        producer="pod_health",
        type=f"pod_health_{check.category}",
        flavor="maintenance" if check.status == FAIL else "activity",
        severity="alert" if check.status == FAIL else "warn",
        scope="pod",
        title=f"test:{check.name}",
        **kwargs,
    )


# ── _apply_signal_dismissals tests ────────────────────────────────────────────


def test_dismissed_signal_marks_check_and_excludes_from_failed(shared: Path):
    check = CheckResult(FAIL, "launchd", "launchd:ai.evolve.evolve.admin-ui", "Plist not found")
    report = HealthReport()
    report.checks = [check]
    report.shared_dir = shared

    sig = _observe_check(shared, check)
    signals_store.apply_transition(sig, "dismissed", shared, actor="user", reason="test")

    _apply_signal_dismissals(report, shared)

    assert check.dismissed is True
    assert report.failed == 0, "dismissed check must not inflate report.failed"


def test_snoozed_signal_marks_check_and_excludes_from_warned(shared: Path):
    check = CheckResult(WARN, "repo_puller", "repo-puller:stale", "Log stale")
    report = HealthReport()
    report.checks = [check]
    report.shared_dir = shared

    sig = _observe_check(shared, check)
    snoozed_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    signals_store.apply_transition(
        sig, "snoozed", shared,
        actor="user", reason="test", snoozed_until=snoozed_until,
    )

    _apply_signal_dismissals(report, shared)

    assert check.dismissed is True
    assert report.warned == 0, "snoozed check must not inflate report.warned"


def test_firing_signal_does_not_suppress_check(shared: Path):
    check = CheckResult(FAIL, "gateways", "team_bot_a:gateway", "No response")
    report = HealthReport()
    report.checks = [check]
    report.shared_dir = shared

    _observe_check(shared, check)  # firing — not dismissed/snoozed

    _apply_signal_dismissals(report, shared)

    assert check.dismissed is False
    assert report.failed == 1


def test_pass_check_is_never_dismissed(shared: Path):
    check = CheckResult(PASS, "gateways", "team_bot_a:gateway", "OK")
    report = HealthReport()
    report.checks = [check]
    report.shared_dir = shared

    _apply_signal_dismissals(report, shared)

    assert check.dismissed is False


def test_only_dismissed_check_is_suppressed_others_count(shared: Path):
    dismissed_check = CheckResult(FAIL, "launchd", "launchd:ai.evolve.evolve.admin-ui", "Missing")
    active_check = CheckResult(FAIL, "gateways", "team_bot_a:gateway", "Down")
    report = HealthReport()
    report.checks = [dismissed_check, active_check]
    report.shared_dir = shared

    sig = _observe_check(shared, dismissed_check)
    signals_store.apply_transition(sig, "dismissed", shared, actor="user", reason="test")
    _observe_check(shared, active_check)

    _apply_signal_dismissals(report, shared)

    assert dismissed_check.dismissed is True
    assert active_check.dismissed is False
    assert report.failed == 1  # only active_check


def test_different_producer_signal_does_not_suppress_pod_health_check(shared: Path):
    """A dismissed Signal from another producer with a similar name must not suppress."""
    check = CheckResult(FAIL, "gateways", "team_bot_a:gateway", "Down")
    report = HealthReport()
    report.checks = [check]
    report.shared_dir = shared

    # Create a dismissed signal from a DIFFERENT producer; its signature won't match.
    other_sig = signals_store.observe(
        shared,
        signature=make_signature("other_producer", "pod_health_gateways", "team_bot_a:gateway"),
        producer="other_producer",
        type="pod_health_gateways",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id="team_bot_a",
        title="other producer signal",
    )
    signals_store.apply_transition(other_sig, "dismissed", shared, actor="user", reason="test")

    _apply_signal_dismissals(report, shared)

    assert check.dismissed is False
    assert report.failed == 1


# ── HealthReport.as_dict() tests ──────────────────────────────────────────────


def test_as_dict_carries_dismissed_flag_and_correct_summary(shared: Path):
    dismissed_check = CheckResult(FAIL, "launchd", "launchd:ai.evolve.evolve.admin-ui", "Missing")
    active_check = CheckResult(FAIL, "gateways", "team_bot_a:gateway", "Down")
    report = HealthReport()
    report.checks = [dismissed_check, active_check]
    report.shared_dir = shared

    sig = _observe_check(shared, dismissed_check)
    signals_store.apply_transition(sig, "dismissed", shared, actor="user", reason="test")
    _apply_signal_dismissals(report, shared)

    d = report.as_dict()
    assert d["summary"]["fail"] == 1  # only active_check counted
    assert d["summary"]["ok"] is False  # still one failure

    checks_by_name = {c["name"]: c for c in d["checks"]}
    assert checks_by_name["launchd:ai.evolve.evolve.admin-ui"]["dismissed"] is True
    assert checks_by_name["team_bot_a:gateway"]["dismissed"] is False


def test_as_dict_ok_true_when_all_fails_are_dismissed(shared: Path):
    check = CheckResult(FAIL, "launchd", "launchd:ai.evolve.evolve.admin-ui", "Missing")
    report = HealthReport()
    report.checks = [check]
    report.shared_dir = shared

    sig = _observe_check(shared, check)
    signals_store.apply_transition(sig, "dismissed", shared, actor="user", reason="test")
    _apply_signal_dismissals(report, shared)

    d = report.as_dict()
    assert d["summary"]["fail"] == 0
    assert d["summary"]["ok"] is True


# ── filter_dismissed_checks() — cached-JSON path ──────────────────────────────


def test_filter_dismissed_checks_marks_dict_and_adjusts_summary(shared: Path):
    check_name = "launchd:ai.evolve.evolve.admin-ui"
    check_cat = "launchd"
    sig_str = make_signature("pod_health", f"pod_health_{check_cat}", check_name)

    sig = signals_store.observe(
        shared,
        signature=sig_str,
        producer="pod_health",
        type=f"pod_health_{check_cat}",
        flavor="maintenance",
        severity="alert",
        scope="pod",
        title="test",
    )
    signals_store.apply_transition(sig, "dismissed", shared, actor="user", reason="test")

    data = {
        "summary": {"pass": 0, "warn": 0, "fail": 1, "ok": False},
        "checks": [
            {
                "status": FAIL,
                "category": check_cat,
                "name": check_name,
                "detail": "Plist not found",
                "dismissed": False,
            },
        ],
    }

    filter_dismissed_checks(data, shared)

    assert data["checks"][0]["dismissed"] is True
    assert data["summary"]["fail"] == 0
    assert data["summary"]["ok"] is True


def test_filter_dismissed_checks_ignores_non_pod_health_signals(shared: Path):
    check_name = "launchd:ai.evolve.evolve.admin-ui"
    check_cat = "launchd"

    # A signal from a different producer
    signals_store.observe(
        shared,
        signature=make_signature("other_producer", f"pod_health_{check_cat}", check_name),
        producer="other_producer",
        type=f"pod_health_{check_cat}",
        flavor="maintenance",
        severity="alert",
        scope="pod",
        title="test",
    )

    data = {
        "summary": {"pass": 0, "warn": 0, "fail": 1, "ok": False},
        "checks": [
            {"status": FAIL, "category": check_cat, "name": check_name,
             "detail": "test", "dismissed": False},
        ],
    }

    filter_dismissed_checks(data, shared)

    # Other-producer dismissal must not affect pod_health checks
    assert data["checks"][0]["dismissed"] is False
    assert data["summary"]["fail"] == 1


def test_filter_dismissed_checks_noop_when_no_dismissals(shared: Path):
    data = {
        "summary": {"pass": 0, "warn": 0, "fail": 2, "ok": False},
        "checks": [
            {"status": FAIL, "category": "gateways", "name": "team_bot_a:gateway",
             "detail": "down", "dismissed": False},
            {"status": WARN, "category": "repo_puller", "name": "repo-puller:stale",
             "detail": "stale", "dismissed": False},
        ],
    }

    filter_dismissed_checks(data, shared)

    assert data["summary"]["fail"] == 2
    assert all(not c["dismissed"] for c in data["checks"])
