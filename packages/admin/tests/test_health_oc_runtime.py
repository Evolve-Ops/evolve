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
from evolve_admin import oc_preflight_store as _pfs
from evolve_admin.health import (
    FAIL,
    PASS,
    WARN,
    HealthReport,
    _check_oc_channel_packages,
    _check_oc_config_valid_under_installed,
    _check_oc_preflight_gate_freshness,
    _check_oc_version_coherence,
    category_labels,
)
from evolve_admin.oc_preflight import bot_set_hash as _bot_set_hash


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


def test_validator_that_cannot_run_warns_it_could_not_check(monkeypatch):
    # "Could not ask" is not "invalid" and not "healthy" — D-CS7: it is its
    # own WARN, never silence, because a validator that cannot run is itself
    # news the operator needs.
    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json",
        lambda bot_id, network: (False, [], "binary not found"),
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    assert _statuses(report, "config_valid_under_oc") == [WARN]
    assert "binary not found" in _detail(report, "config_valid_under_oc")


def test_a_raising_validator_warns_instead_of_crashing_the_health_run(monkeypatch):
    def boom(bot_id, network):
        raise RuntimeError("nope")

    monkeypatch.setattr(
        "evolve_admin.openclaw_config_validator.validate_bot_openclaw_json", boom,
    )
    report = HealthReport()
    _check_oc_config_valid_under_installed(report, ["team-bot-a"], {})
    assert _statuses(report, "config_valid_under_oc") == [WARN]


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


def test_unknown_installed_version_warns_instead_of_silence(monkeypatch):
    # D-CS7: cannot know the line to compare against, but that is itself a
    # fact worth a WARN — guessing would be worse, silence is worse still.
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: None,
    )
    report = HealthReport()
    _check_oc_channel_packages(report, ["team-bot-a"], {})
    assert _statuses(report, "channel_packages") == [WARN]


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


# ── preflight gate freshness (hold-fix-4392) ─────────────────────────────────


def _iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_fresh_preflight_gate_passes(monkeypatch, tmp_path):
    network = {"bots": {"team-bot-a": {}}}
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.2",
    )
    monkeypatch.setattr(
        _pfs, "latest_cache_for_installed",
        lambda installed, shared_dir=None: {
            "checked_at": _iso_now(), "bot_set_hash": _bot_set_hash(network),
        },
    )
    report = HealthReport()
    _check_oc_preflight_gate_freshness(report, shared_dir=tmp_path, network=network)
    assert _statuses(report, "oc_preflight_gate_fresh") == [PASS]


def test_missing_preflight_gate_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.2",
    )
    monkeypatch.setattr(
        _pfs, "latest_cache_for_installed", lambda installed, shared_dir=None: None,
    )
    report = HealthReport()
    _check_oc_preflight_gate_freshness(report, shared_dir=tmp_path, network={"bots": {}})
    assert _statuses(report, "oc_preflight_gate_fresh") == [WARN]
    assert "No per-bot OpenClaw preflight" in _detail(report, "oc_preflight_gate_fresh")


def test_stale_preflight_gate_warns(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    network = {"bots": {"team-bot-a": {}}}
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.2",
    )
    monkeypatch.setattr(
        _pfs, "latest_cache_for_installed",
        lambda installed, shared_dir=None: {
            "checked_at": old, "bot_set_hash": _bot_set_hash(network),
        },
    )
    report = HealthReport()
    _check_oc_preflight_gate_freshness(report, shared_dir=tmp_path, network=network)
    assert _statuses(report, "oc_preflight_gate_fresh") == [WARN]
    assert "stale" in _detail(report, "oc_preflight_gate_fresh")


def test_a_bot_set_change_warns_distinctly(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: "2026.9.2",
    )
    monkeypatch.setattr(
        _pfs, "latest_cache_for_installed",
        lambda installed, shared_dir=None: {
            "checked_at": _iso_now(), "bot_set_hash": _bot_set_hash({"bots": {}}),
        },
    )
    report = HealthReport()
    _check_oc_preflight_gate_freshness(
        report, shared_dir=tmp_path, network={"bots": {"team-bot-a": {}}},
    )
    assert _statuses(report, "oc_preflight_gate_fresh") == [WARN]
    assert "bot was added or removed" in _detail(report, "oc_preflight_gate_fresh")


def test_unknown_installed_version_warns_without_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "evolve_admin.upstream_version.installed_package_version", lambda: None,
    )
    report = HealthReport()
    _check_oc_preflight_gate_freshness(report, shared_dir=tmp_path, network={"bots": {}})
    assert _statuses(report, "oc_preflight_gate_fresh") == [WARN]


# ── breaker notify path can start (breaker-notify-survives-openclaw- ────────
# config-validation item 5) ───────────────────────────────────────────────


def test_notify_path_ok_when_cli_validates(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.alerts.dispatcher.check_openclaw_cli_can_start",
        lambda: ("ok", "config validates"),
    )
    report = HealthReport()
    health._check_breaker_notify_path(report)
    assert _statuses(report, "breaker_notify_path_can_start") == [PASS]


def test_notify_path_fails_loudly_when_cli_refuses(monkeypatch):
    # Unlike the per-bot config checks above, this one DOES fail: a
    # confirmed-dead spend safety net deserves more than a quiet WARN among
    # many (contrast with test_none_of_these_checks_ever_FAIL).
    monkeypatch.setattr(
        "evolve_admin.alerts.dispatcher.check_openclaw_cli_can_start",
        lambda: ("broken", "meta.lastTouchedAt: Unrecognized key"),
    )
    report = HealthReport()
    health._check_breaker_notify_path(report)
    assert _statuses(report, "breaker_notify_path_can_start") == [FAIL]
    assert "lastTouchedAt" in _detail(report, "breaker_notify_path_can_start")


def test_notify_path_warns_when_cli_cannot_be_found(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.alerts.dispatcher.check_openclaw_cli_can_start",
        lambda: ("unknown", "openclaw binary not found on PATH"),
    )
    report = HealthReport()
    health._check_breaker_notify_path(report)
    assert _statuses(report, "breaker_notify_path_can_start") == [WARN]


def test_notify_path_warns_instead_of_crashing_when_the_probe_raises(monkeypatch):
    def _boom():
        raise RuntimeError("simulated probe failure")

    monkeypatch.setattr(
        "evolve_admin.alerts.dispatcher.check_openclaw_cli_can_start", _boom,
    )
    report = HealthReport()
    health._check_breaker_notify_path(report)
    assert _statuses(report, "breaker_notify_path_can_start") == [WARN]


def test_notify_path_fix_never_suggests_sudo(monkeypatch):
    # No `sudo` hints in web UI copy (CLAUDE.md guardrail).
    monkeypatch.setattr(
        "evolve_admin.alerts.dispatcher.check_openclaw_cli_can_start",
        lambda: ("broken", "meta.lastTouchedAt: Unrecognized key"),
    )
    report = HealthReport()
    health._check_breaker_notify_path(report)
    checks = [c for c in report.checks if "breaker_notify_path_can_start" in c.name]
    assert checks and "sudo" not in (checks[0].fix_cmd or "").lower()


# ── the category renders ─────────────────────────────────────────────────────


def test_oc_runtime_category_has_a_label():
    # Without this the SPA and the CLI render an unlabelled section, which is
    # the drift category_labels() exists to prevent.
    assert category_labels().get("oc_runtime") == "OpenClaw Runtime"
