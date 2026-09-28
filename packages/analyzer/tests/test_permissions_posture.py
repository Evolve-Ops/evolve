"""Tests for permissions.posture — rule-based composite classifier."""
from __future__ import annotations

from permissions.inventory import (
    PermissionConfig, PermissionInventory, ExecApprovals,
    ScheduledInvocations, CronJob,
)
from permissions import posture


def _inv(fields: dict, jobs: list | None = None, *, cron_present: bool = False) -> PermissionInventory:
    pc = PermissionConfig(openclaw_config_path="/x", fields=fields, field_signature="sig")
    ea = ExecApprovals(path="/y")
    si = ScheduledInvocations(path="/z", present=cron_present, jobs=jobs or [])
    return PermissionInventory(
        bot_id="b",
        observed_at="2026-05-10T00:00:00Z",
        permission_config=pc,
        exec_approvals=ea,
        scheduled_invocations=si,
    )


# ── Score classification ─────────────────────────────────────────────────────

def test_score_open_when_full_and_ask_off():
    # Sandbox is not modelled — see posture._axis_sandbox docstring. The
    # "open" classification no longer factors sandbox; full + ask=off alone
    # qualifies.
    inv = _inv({"tools.exec.security": "full", "tools.exec.ask": "off"})
    cp = posture.classify(inv)
    assert cp["score"] == "open"


def test_score_wide_today_pod_default():
    """Spec §1: today's pod is uniformly `wide`."""
    inv = _inv({
        "tools.exec.security": "full",
        "tools.exec.ask": "on-miss",
        "tools.fs.workspaceOnly": None,
        "commands.ownerAllowFrom": None,
    })
    cp = posture.classify(inv)
    assert cp["score"] == "wide"


def test_score_moderate_when_workspace_only():
    """Spec §1: team_bot_c is `moderate` because workspaceOnly=true."""
    inv = _inv({
        "tools.exec.security": "full",
        "tools.exec.ask": "on-miss",
        "tools.fs.workspaceOnly": True,
    })
    cp = posture.classify(inv)
    assert cp["score"] == "moderate"


def test_score_moderate_when_allowlist():
    inv = _inv({"tools.exec.security": "allowlist", "tools.exec.ask": "on-miss"})
    cp = posture.classify(inv)
    assert cp["score"] == "moderate"


def test_score_moderate_when_owner_allowlist():
    inv = _inv({
        "tools.exec.security": "full",
        "tools.exec.ask": "on-miss",
        "commands.ownerAllowFrom": ["telegram:1260193629"],
    })
    cp = posture.classify(inv)
    assert cp["score"] == "moderate"


def test_score_tight_when_deny():
    inv = _inv({"tools.exec.security": "deny"})
    cp = posture.classify(inv)
    assert cp["score"] == "tight"


def test_sandbox_axis_is_unmodeled():
    """Sandbox tracking was wired against an invalid OC schema path. Until
    re-modelled against agents.defaults.sandbox.mode, the axis reports
    'unmodeled' regardless of input fields."""
    for fields in [{}, {"sandbox.enabled": True}, {"sandbox.enabled": False}]:
        cp = posture.classify(_inv(fields))
        assert cp["axes"]["sandbox"] == "unmodeled"


# ── Axis classification ─────────────────────────────────────────────────────

def test_axis_execution_variants():
    cases = [
        ({"tools.exec.security": "deny"}, "deny"),
        ({"tools.exec.security": "allowlist"}, "allowlist"),
        ({"tools.exec.security": "full", "tools.exec.ask": "off"}, "full+ask-off"),
        ({"tools.exec.security": "full", "tools.exec.ask": "always"}, "full+ask-always"),
        ({"tools.exec.security": "full", "tools.exec.ask": "on-miss"}, "full+ask-on-miss"),
        ({}, "unset"),
    ]
    for fields, expected in cases:
        cp = posture.classify(_inv(fields))
        assert cp["axes"]["execution"] == expected, f"expected {expected} for {fields}, got {cp['axes']['execution']}"


def test_axis_scheduled_capped_vs_uncapped():
    capped = CronJob(id="1", name="a", enabled=True, schedule_kind="every",
                    payload_kind="agentTurn", has_turn_cap=True, has_budget_cap=True,
                    payload_summary="msg: do x", signature="s")
    uncapped = CronJob(id="2", name="b", enabled=True, schedule_kind="every",
                      payload_kind="agentTurn", has_turn_cap=False, has_budget_cap=False,
                      payload_summary="msg: do y", signature="s")
    sysevent = CronJob(id="3", name="c", enabled=True, schedule_kind="every",
                      payload_kind="systemEvent", has_turn_cap=False, has_budget_cap=False,
                      payload_summary="event: ping", signature="s")

    assert posture.classify(_inv({}, cron_present=True, jobs=[capped]))["axes"]["scheduled"] == "capped"
    assert posture.classify(_inv({}, cron_present=True, jobs=[uncapped]))["axes"]["scheduled"] == "uncapped-agent-turns"
    assert posture.classify(_inv({}, cron_present=True, jobs=[capped, uncapped]))["axes"]["scheduled"] == "uncapped-agent-turns"
    assert posture.classify(_inv({}, cron_present=True, jobs=[sysevent]))["axes"]["scheduled"] == "no-agent-turns"
    assert posture.classify(_inv({}, cron_present=False, jobs=[]))["axes"]["scheduled"] == "no-cron"


def test_axis_web_off_vs_partial_vs_open():
    cases = [
        ({"tools.web.search.enabled": False, "tools.web.fetch.enabled": False}, "off"),
        ({"tools.web.search.enabled": True, "tools.web.fetch.enabled": False}, "partial"),
        ({"tools.web.search.enabled": True, "tools.web.fetch.enabled": True}, "open"),
    ]
    for fields, expected in cases:
        cp = posture.classify(_inv(fields))
        assert cp["axes"]["web"] == expected


def test_classify_returns_rationale():
    inv = _inv({"tools.exec.security": "full", "tools.exec.ask": "off"})
    cp = posture.classify(inv)
    assert cp["rationale"]
    assert "no approval gate" in cp["rationale"].lower()


def test_annotate_writes_to_inventory():
    inv = _inv({"tools.exec.security": "deny"})
    posture.annotate(inv)
    assert inv.composite_posture["score"] == "tight"
