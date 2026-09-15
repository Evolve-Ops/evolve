"""Tests for the OpenClaw Posture Doctor (Phase 0.5 — OCP013 only).

Pinned cases:

  - OCP013 fires on the security_bot-2026-05-20 incident shape (allowlist +
    on-miss + telegram enabled).
  - OCP013 does NOT fire on a permissive (full + ask=off) bot.
  - OCP013 does NOT fire on a deny-everything bot (no exec, no ask
    flow possible).
  - OCP013 does NOT fire when only Slack is enabled (slack has an
    approval surface).
  - OCP013 distinguishes Slack-with-surface from Telegram-without:
    a bot with both enabled fires for telegram but not slack.
  - OCP013 fires on ``ask=always`` even with ``security=full``.
  - OCP013 ignores channels with ``enabled: False``.
  - OCP013 tolerates a missing ``channels`` block (returns no finding).
  - ``DoctorResult`` surfaces ``exec_security``, ``exec_ask``,
    ``enabled_channels``, ``oc_version`` for the caller's header.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.openclaw_posture import (  # noqa: E402
    APPROVAL_SURFACE_CAPABILITY,
    DoctorResult,
    Finding,
    run_doctor,
)
from evolve_admin.openclaw_posture.doctor import (  # noqa: E402
    _ask_flow_can_fire,
    _enabled_message_channels,
    iter_rules,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _config(
    *,
    exec_security: str | None = None,
    exec_ask: str | None = None,
    channels: dict | None = None,
    version: str = "2026.5.18",
) -> dict:
    """Build a minimal openclaw.json-shaped dict for tests."""
    cfg: dict = {"meta": {"lastTouchedVersion": version}}
    if exec_security is not None or exec_ask is not None:
        exec_block: dict = {}
        if exec_security is not None:
            exec_block["security"] = exec_security
        if exec_ask is not None:
            exec_block["ask"] = exec_ask
        cfg["tools"] = {"exec": exec_block}
    if channels is not None:
        cfg["channels"] = channels
    return cfg


def _ocp013(result: DoctorResult) -> Finding | None:
    """Return the OCP013 finding from a result, or None if absent."""
    for f in result.findings:
        if f.code == "OCP013":
            return f
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Channel-capability set sanity
# ─────────────────────────────────────────────────────────────────────────────


def test_approval_surface_capability_contains_slack():
    """Slack has block-team_bot_a interactive components — the only
    in-channel approval surface today."""
    assert "slack" in APPROVAL_SURFACE_CAPABILITY


def test_approval_surface_capability_excludes_telegram():
    """Telegram has no inline-button approval flow today. If/when
    Evolve or OC ships one, this test flips."""
    assert "telegram" not in APPROVAL_SURFACE_CAPABILITY


def test_approval_surface_capability_excludes_discord_matrix_etc():
    for ch in ("discord", "matrix", "irc", "signal", "imessage",
               "msteams", "feishu", "googlechat", "whatsapp"):
        assert ch not in APPROVAL_SURFACE_CAPABILITY, (
            f"{ch} should not be in APPROVAL_SURFACE_CAPABILITY until "
            f"an approval flow ships for it"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helper tests
# ─────────────────────────────────────────────────────────────────────────────


def test_ask_flow_fires_on_allowlist():
    assert _ask_flow_can_fire({"security": "allowlist"}) is True


def test_ask_flow_fires_on_on_miss():
    assert _ask_flow_can_fire({"security": "full", "ask": "on-miss"}) is True


def test_ask_flow_fires_on_always():
    assert _ask_flow_can_fire({"security": "full", "ask": "always"}) is True


def test_ask_flow_does_not_fire_on_full_off():
    assert _ask_flow_can_fire({"security": "full", "ask": "off"}) is False


def test_ask_flow_does_not_fire_on_deny():
    assert _ask_flow_can_fire({"security": "deny"}) is False


def test_ask_flow_does_not_fire_on_empty_block():
    assert _ask_flow_can_fire({}) is False


def test_ask_flow_does_not_fire_on_non_dict():
    assert _ask_flow_can_fire(None) is False  # type: ignore[arg-type]
    assert _ask_flow_can_fire("deny") is False  # type: ignore[arg-type]


def test_enabled_channels_includes_implicit():
    """OC's per-channel ``enabled`` defaults to true-when-present.
    Conservative read: only ``enabled: False`` suppresses inclusion."""
    out = _enabled_message_channels({
        "slack": {"botToken": "x"},               # no enabled key → include
        "telegram": {"enabled": True},
        "discord": {"enabled": False},            # explicit false → exclude
        "matrix": {"enabled": None},              # null → include
    })
    assert "slack" in out
    assert "telegram" in out
    assert "matrix" in out
    assert "discord" not in out


# ─────────────────────────────────────────────────────────────────────────────
# OCP013 — positive cases (rule fires)
# ─────────────────────────────────────────────────────────────────────────────


def test_ocp013_fires_on_security_bot_2026_05_20_shape():
    """The canonical motivating incident: allowlist + on-miss + Telegram."""
    cfg = _config(
        exec_security="allowlist",
        exec_ask="on-miss",
        channels={"telegram": {"botToken": "xyz", "enabled": True}},
    )
    result = run_doctor("security_bot", oc_config=cfg)
    f = _ocp013(result)
    assert f is not None, "OCP013 should fire on security_bot-2026-05-20 shape"
    assert f.severity == "fail"
    assert "telegram" in f.channels_without_surface
    assert f.bot_id == "security_bot"
    assert "security_bot" in f.title
    assert "telegram" in f.title


def test_ocp013_fires_on_ask_always_with_security_full():
    """Even with ``security=full``, ``ask=always`` routes every exec
    through the approval surface — same gap if the channel doesn't
    have one."""
    cfg = _config(
        exec_security="full",
        exec_ask="always",
        channels={"discord": {}},
    )
    f = _ocp013(run_doctor("bot", oc_config=cfg))
    assert f is not None
    assert f.severity == "fail"
    assert "discord" in f.channels_without_surface


def test_ocp013_lists_all_gap_channels():
    cfg = _config(
        exec_security="allowlist",
        channels={
            "telegram": {"enabled": True},
            "discord": {"enabled": True},
            "matrix": {"enabled": True},
        },
    )
    f = _ocp013(run_doctor("bot", oc_config=cfg))
    assert f is not None
    assert set(f.channels_without_surface) == {"telegram", "discord", "matrix"}


def test_ocp013_fires_when_both_slack_and_telegram_enabled_but_only_for_telegram():
    """A mixed-channel bot: slack has approval surface (no gap),
    telegram does not (gap). Rule fires once with telegram listed."""
    cfg = _config(
        exec_security="allowlist",
        channels={
            "slack": {"botToken": "abc", "enabled": True},
            "telegram": {"botToken": "xyz", "enabled": True},
        },
    )
    f = _ocp013(run_doctor("bot", oc_config=cfg))
    assert f is not None
    assert "telegram" in f.channels_without_surface
    assert "slack" not in f.channels_without_surface


def test_ocp013_finding_carries_field_and_values():
    """The finding's anchor metadata identifies which config fields
    are responsible — the Signal store uses these for dedup."""
    cfg = _config(
        exec_security="allowlist",
        exec_ask="on-miss",
        channels={"telegram": {}},
    )
    f = _ocp013(run_doctor("security_bot", oc_config=cfg))
    assert f is not None
    assert f.field_path == "tools.exec"
    assert f.to_value == {"security": "allowlist", "ask": "on-miss"}


# ─────────────────────────────────────────────────────────────────────────────
# OCP013 — negative cases (rule should NOT fire)
# ─────────────────────────────────────────────────────────────────────────────


def test_ocp013_quiet_on_security_full_ask_off():
    """The permissive case: no ask flow can fire → no gap to report."""
    cfg = _config(
        exec_security="full",
        exec_ask="off",
        channels={"telegram": {}, "discord": {}},
    )
    assert _ocp013(run_doctor("bot", oc_config=cfg)) is None


def test_ocp013_quiet_on_security_deny():
    """Deny is the hardest gate — no exec at all → no ask flow → no
    gap. (Today's pod state for the 6 bots we just fixed, ironically.)"""
    cfg = _config(
        exec_security="deny",
        channels={"telegram": {}, "discord": {}, "matrix": {}},
    )
    assert _ocp013(run_doctor("bot", oc_config=cfg)) is None


def test_ocp013_quiet_when_only_slack_enabled():
    """Slack is in the capability set — no gap."""
    cfg = _config(
        exec_security="allowlist",
        exec_ask="on-miss",
        channels={"slack": {"botToken": "x", "enabled": True}},
    )
    assert _ocp013(run_doctor("bot", oc_config=cfg)) is None


def test_ocp013_quiet_when_channels_block_missing():
    """A bot with no channels block configured cannot have a
    channel-context exec gap. Returns no finding."""
    cfg = _config(exec_security="allowlist", exec_ask="on-miss")
    assert _ocp013(run_doctor("bot", oc_config=cfg)) is None


def test_ocp013_ignores_disabled_channels():
    """A telegram block with ``enabled: False`` is not active —
    irrelevant to the gap analysis."""
    cfg = _config(
        exec_security="allowlist",
        exec_ask="on-miss",
        channels={
            "telegram": {"enabled": False},
            "slack": {"botToken": "x"},
        },
    )
    assert _ocp013(run_doctor("bot", oc_config=cfg)) is None


def test_ocp013_quiet_when_no_channels_enabled():
    """All channels explicitly disabled — no surface for the gap."""
    cfg = _config(
        exec_security="allowlist",
        channels={
            "telegram": {"enabled": False},
            "discord": {"enabled": False},
        },
    )
    assert _ocp013(run_doctor("bot", oc_config=cfg)) is None


# ─────────────────────────────────────────────────────────────────────────────
# DoctorResult surface-state
# ─────────────────────────────────────────────────────────────────────────────


def test_result_surfaces_exec_state():
    cfg = _config(
        exec_security="allowlist",
        exec_ask="on-miss",
        channels={"telegram": {"enabled": True}},
        version="2026.5.18",
    )
    result = run_doctor("security_bot", oc_config=cfg)
    assert result.bot_id == "security_bot"
    assert result.exec_security == "allowlist"
    assert result.exec_ask == "on-miss"
    assert result.enabled_channels == ["telegram"]
    assert result.oc_version == "2026.5.18"


def test_result_surfaces_state_even_when_no_findings():
    """The state header should populate regardless of rule outcomes —
    useful for the CLI's per-bot summary line."""
    cfg = _config(
        exec_security="full",
        exec_ask="off",
        channels={"slack": {"botToken": "x"}},
    )
    result = run_doctor("team_bot_a", oc_config=cfg)
    assert result.findings == []
    assert result.exec_security == "full"
    assert result.exec_ask == "off"
    assert result.enabled_channels == ["slack"]


def test_result_has_fail_helper():
    cfg = _config(
        exec_security="allowlist",
        channels={"telegram": {}},
    )
    result = run_doctor("security_bot", oc_config=cfg)
    assert result.has_fail() is True


def test_result_has_fail_false_when_quiet():
    cfg = _config(exec_security="deny")
    result = run_doctor("security_bot", oc_config=cfg)
    assert result.has_fail() is False


# ─────────────────────────────────────────────────────────────────────────────
# Defensive / shape contracts
# ─────────────────────────────────────────────────────────────────────────────


def test_run_doctor_rejects_non_dict():
    with pytest.raises(TypeError):
        run_doctor("bot", oc_config="not-a-dict")  # type: ignore[arg-type]


def test_run_doctor_handles_empty_config():
    """An empty openclaw.json shouldn't crash — just return no findings
    and empty state. Defensive: the upgrade migrator might leave a
    minimal config briefly."""
    result = run_doctor("bot", oc_config={})
    assert result.findings == []
    assert result.exec_security is None
    assert result.exec_ask is None
    assert result.enabled_channels == []
    assert result.oc_version is None


def test_iter_rules_reports_ocp013():
    """Phase 0.5 ships exactly one rule; the CLI's status header uses
    this count."""
    rules = list(iter_rules())
    assert "OCP013" in rules
    assert len(rules) == 1  # Phase 0.5
