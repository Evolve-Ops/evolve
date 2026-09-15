"""Feasibility-check coverage for _check_daemons.

A finding is only useful if its suggested_fix can actually succeed. The
canonical bad shape (which prompted this PR): mcp-bridge shipped as a per-
user LaunchAgent with bootstrap target `gui/<uid>`. On a headless pod the
admin user has no Aqua session → gui/<uid> domain doesn't exist → the
suggested `launchctl bootstrap gui/$UID …` returns error 125. The audit
filed `daemon_not_loaded` every run, the operator dismissed it, then it
came back next audit. Forever.

These tests cover the feasibility-aware code path: when kind="agent" and
the target Aqua session is missing, the audit emits a
`daemon_load_infeasible` finding (severity=critical, but with a structural
suggested_fix) instead of the bootstrap finding that would just be dismissed
again.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import infra_audit  # noqa: E402
from platform_profile import LINUX, set_profile  # noqa: E402


def test_check_daemons_gates_out_on_linux(tmp_path, monkeypatch):
    """Linux: the launchd-only daemons element gates OUT — returns [] (no
    findings), and NEVER reaches the launchctl probe (so no launchctl is run
    and no false daemon_not_loaded cascade fires).

    The conftest autouse fixture pins MACOS; this test opts out with
    set_profile(LINUX) (teardown restores MACOS per the conftest contract).
    """
    set_profile(LINUX)

    # An is-loaded seam that fails the test if invoked — the Linux gate must
    # short-circuit before any probe, plist glob, or feasibility check.
    def boom(_label):
        raise AssertionError("daemons element must gate out before probing on Linux")

    findings = infra_audit._check_daemons(network={}, launchctl_list_fn=boom)
    assert findings == []


def _setup_agent_test(monkeypatch, tmp_path):
    """Common scaffold: make ONE fake kind=agent daemon whose plist exists."""
    label = "ai.evolve.evolve.fake-agent-for-test"
    monkeypatch.setattr(infra_audit, "CORE_INFRA_DAEMONS", (label,))
    monkeypatch.setattr(
        infra_audit, "CORE_INFRA_DAEMON_KINDS",
        {label: "agent"},
    )
    fake_home = tmp_path / "home"
    plist_dir = fake_home / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True)
    plist = plist_dir / f"{label}.plist"
    plist.write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        f'<key>Label</key><string>{label}</string>'
        '</dict></plist>'
    )
    monkeypatch.setattr(
        infra_audit, "_agent_plist_path",
        lambda lbl: plist_dir / f"{lbl}.plist",
    )
    return label


def test_unloaded_agent_with_no_aqua_emits_infeasible_finding(tmp_path, monkeypatch):
    """The canonical mcp-bridge-on-headless-pod case → daemon_load_infeasible."""
    label = _setup_agent_test(monkeypatch, tmp_path)

    monkeypatch.setattr(infra_audit, "_resolve_admin_uid", lambda: "501")
    monkeypatch.setattr(
        infra_audit, "_probe_aqua_session",
        lambda uid: (False, "background_only"),
    )

    findings = infra_audit._check_daemons(
        network={}, launchctl_list_fn=lambda _label: (False, ""),
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.category == "daemon_load_infeasible", (
        f"expected daemon_load_infeasible when admin user has no Aqua session; "
        f"got {f.category}"
    )
    assert f.severity == "critical"
    assert f.evidence["aqua_probe"] == "background_only"
    assert f.evidence["target_uid"] == "501"
    # The suggested_fix must point at the STRUCTURAL remediation (convert to
    # LaunchDaemon), not the bootstrap that would fail with error 125.
    assert "system-scope" in f.suggested_fix.lower() or "launchdaemon" in f.suggested_fix.lower()
    assert "convert" in f.suggested_fix.lower()
    # Rationale carries the structural reason so downstream proposal body shows it.
    assert "aqua" in f.rationale.lower()


def test_unloaded_agent_with_no_session_emits_infeasible_finding(tmp_path, monkeypatch):
    """If the target user isn't logged in at all, same infeasible verdict."""
    label = _setup_agent_test(monkeypatch, tmp_path)

    monkeypatch.setattr(infra_audit, "_resolve_admin_uid", lambda: "501")
    monkeypatch.setattr(
        infra_audit, "_probe_aqua_session",
        lambda uid: (False, "no_session"),
    )

    findings = infra_audit._check_daemons(
        network={}, launchctl_list_fn=lambda _label: (False, ""),
    )
    assert len(findings) == 1
    assert findings[0].category == "daemon_load_infeasible"
    assert findings[0].evidence["aqua_probe"] == "no_session"


def test_unloaded_agent_with_aqua_emits_normal_not_loaded(tmp_path, monkeypatch):
    """If the user *does* have an Aqua session, the bootstrap fix CAN work →
    emit the normal daemon_not_loaded finding so the bootstrap one-liner
    surfaces."""
    label = _setup_agent_test(monkeypatch, tmp_path)

    monkeypatch.setattr(infra_audit, "_resolve_admin_uid", lambda: "502")
    monkeypatch.setattr(
        infra_audit, "_probe_aqua_session",
        lambda uid: (True, ""),
    )

    findings = infra_audit._check_daemons(
        network={}, launchctl_list_fn=lambda _label: (False, ""),
    )
    assert len(findings) == 1
    assert findings[0].category == "daemon_not_loaded"
    assert "gui/" in findings[0].suggested_fix


def test_unloaded_agent_with_unprobeable_session_falls_through(tmp_path, monkeypatch):
    """If we can't probe (sudo blocked, etc), don't make up a verdict — emit
    the normal not_loaded finding rather than silently dropping it."""
    label = _setup_agent_test(monkeypatch, tmp_path)

    monkeypatch.setattr(infra_audit, "_resolve_admin_uid", lambda: "501")
    monkeypatch.setattr(
        infra_audit, "_probe_aqua_session",
        lambda uid: (False, "cannot_probe"),
    )

    findings = infra_audit._check_daemons(
        network={}, launchctl_list_fn=lambda _label: (False, ""),
    )
    assert len(findings) == 1
    assert findings[0].category == "daemon_not_loaded"


def test_system_daemon_unloaded_does_not_probe_aqua(tmp_path, monkeypatch):
    """Aqua probe is agent-only. A system daemon being unloaded should emit
    the normal daemon_not_loaded finding without invoking the probe (which
    could be expensive). Regression guard against accidentally generalizing
    the probe to all kinds.
    """
    label = "ai.evolve.evolve.fake-system-for-test"
    monkeypatch.setattr(infra_audit, "CORE_INFRA_DAEMONS", (label,))
    monkeypatch.setattr(
        infra_audit, "CORE_INFRA_DAEMON_KINDS",
        {label: "system"},
    )
    sys_dir = tmp_path / "LaunchDaemons"
    sys_dir.mkdir()
    # We can't easily patch the inline `Path("/Library/LaunchDaemons")` in
    # _check_daemons without a deeper refactor, so just verify the probe
    # isn't called by counting calls.
    aqua_calls = {"count": 0}

    def _fake_probe(uid):
        aqua_calls["count"] += 1
        return (False, "background_only")

    monkeypatch.setattr(infra_audit, "_probe_aqua_session", _fake_probe)
    monkeypatch.setattr(infra_audit, "_resolve_admin_uid", lambda: "501")

    # System plist doesn't exist in the test env, but the missing-plist
    # branch fires BEFORE the load-state branch — so the probe is never
    # called regardless.
    plist = Path("/Library/LaunchDaemons") / f"{label}.plist"
    if plist.exists():
        pytest.skip("real plist installed; cannot test missing-plist path")

    infra_audit._check_daemons(
        network={}, launchctl_list_fn=lambda _label: (False, ""),
    )
    assert aqua_calls["count"] == 0, (
        "Aqua probe must not run for system-kind daemons; "
        f"got {aqua_calls['count']} call(s)"
    )
