"""Tests for the three OpenClaw-runtime health checks.

Chip: internal/dispatch/done/oc-upgrade-is-a-guarded-change.md item 5.

After the 2026-09-07 upgrade all three of these were false on all nine bots and
no surface said so. The property each test pins is the one that makes the check
worth having: it must distinguish "we know it is broken" from "we could not
ask", because a check that reports uncertainty as health is how nine bots
looked fine while none of them were.
"""

from __future__ import annotations

import json

import pytest

from evolve_admin import health
from evolve_admin.health import (
    FAIL,
    PASS,
    WARN,
    HealthReport,
    _check_oc_channel_packages,
    _check_oc_config_valid_under_installed,
    _check_oc_version_coherence,
    category_labels,
)


def _statuses(report, name_contains):
    return [c.status for c in report.checks if name_contains in c.name]


def _detail(report, name_contains):
    return " ".join(c.detail for c in report.checks if name_contains in c.name)


# ── config valid under the installed runtime ─────────────────────────────────


def test_valid_config_passes(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json",
        lambda bot_id, network: (True, [], None),
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    assert _statuses(report, "config_valid_under_oc") == [PASS]


def test_invalid_config_warns_and_names_the_keys(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json",
        lambda bot_id, network: (False, ["meta.lastTouchedAt", "auth.cooldowns"], None),
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    assert _statuses(report, "config_valid_under_oc") == [WARN]
    assert "meta.lastTouchedAt" in _detail(report, "config_valid_under_oc")


def test_invalid_config_fix_never_suggests_unattended_doctor_fix(monkeypatch):
    # The guardrail the whole chip exists for: `doctor --fix` rewrites model
    # refs, so a health surface must never send an operator to it casually.
    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json",
        lambda bot_id, network: (False, ["x"], None),
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    fix = report.checks[0].fix_cmd or ""
    assert "doctor --fix" in fix and "Do not run" in fix, (
        "if the fix mentions doctor --fix at all it must warn against it"
    )


def test_validator_that_cannot_run_reports_NOTHING(monkeypatch):
    # "Could not ask" is not "invalid" and not "healthy" — it is news about the
    # validator, and belongs on no pod-health row.
    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json",
        lambda bot_id, network: (False, [], "binary not found"),
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    assert report.checks == []


def test_a_raising_validator_does_not_crash_the_health_run(monkeypatch):
    def boom(bot_id, network):
        raise RuntimeError("nope")

    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json", boom,
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    assert report.checks == []


# ── channel packages match the installed runtime line ────────────────────────


def test_matching_channel_packages_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.4",
    )
    monkeypatch.setattr(health, "_bot_home", lambda _b: tmp_path)
    pkg = tmp_path / ".openclaw" / "npm" / "node_modules" / "@openclaw" / "slack"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": "2026.9.1"}))
    report = HealthReport()
    _check_oc_channel_packages(report, ["team-bot-a"], {})
    assert _statuses(report, "channel_packages") == [PASS]


def test_stale_channel_package_warns_and_names_it(monkeypatch, tmp_path):
    # The 2026-09-07 symptom: a channel dead with "Package subpath
    # './plugin-sdk/channel-streaming' is not defined" and nothing saying why.
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.4",
    )
    monkeypatch.setattr(health, "_bot_home", lambda _b: tmp_path)
    pkg = tmp_path / ".openclaw" / "npm" / "node_modules" / "@openclaw" / "slack"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": "2026.7.1"}))
    report = HealthReport()
    _check_oc_channel_packages(report, ["team-bot-a"], {})
    assert _statuses(report, "channel_packages") == [WARN]
    assert "@openclaw/slack@2026.7.1" in _detail(report, "channel_packages")


def test_unknown_installed_version_reports_nothing(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: None,
    )
    report = HealthReport()
    _check_oc_channel_packages(report, ["team-bot-a"], {})
    assert report.checks == [], "cannot know the line — guessing is worse than silence"


# ── is the installed runtime one Evolve was validated against? ───────────────


class _State:
    def __init__(self, state):
        self.state = state


def test_a_tested_version_passes(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.4",
    )
    monkeypatch.setattr(
        "evolve_admin.oc_compat.validation_state", lambda v: _State("tested"),
    )
    report = HealthReport()
    _check_oc_version_coherence(report)
    assert _statuses(report, "oc_version_validated") == [PASS]


def test_an_untested_version_warns_and_says_absence_is_not_a_pass(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.4",
    )
    monkeypatch.setattr(
        "evolve_admin.oc_compat.validation_state", lambda v: _State("untested"),
    )
    report = HealthReport()
    _check_oc_version_coherence(report)
    assert _statuses(report, "oc_version_validated") == [WARN]
    detail = _detail(report, "oc_version_validated")
    assert "not been validated" in detail
    assert "not a pass" in detail, (
        "the fail-safe has to be stated, not implied — this is the sentence "
        "that stops 'no run recorded' being read as 'fine'"
    )


def test_a_failed_contract_run_warns_and_points_at_the_report(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.4",
    )
    monkeypatch.setattr(
        "evolve_admin.oc_compat.validation_state", lambda v: _State("failed"),
    )
    report = HealthReport()
    _check_oc_version_coherence(report)
    assert _statuses(report, "oc_version_validated") == [WARN]
    assert "FAILED" in _detail(report, "oc_version_validated")


def test_unknown_installed_version_is_its_own_warning(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: None,
    )
    report = HealthReport()
    _check_oc_version_coherence(report)
    assert _statuses(report, "oc_version_known") == [WARN]


def test_none_of_these_checks_ever_FAIL(monkeypatch):
    # All three are WARN by design: the pod may be running perfectly on an
    # unvalidated runtime, and a FAIL on a healthy pod trains people to ignore
    # the surface — the failure mode the contract workflow also designs against.
    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json",
        lambda bot_id, network: (False, ["x"], None),
    )
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: None,
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    _check_oc_version_coherence(report)
    assert FAIL not in [c.status for c in report.checks]


# ── the category renders ─────────────────────────────────────────────────────


def test_oc_runtime_category_has_a_label():
    # Without this the SPA and the CLI render an unlabelled section, which is
    # the drift category_labels() exists to prevent.
    assert category_labels().get("oc_runtime") == "OpenClaw Runtime"
