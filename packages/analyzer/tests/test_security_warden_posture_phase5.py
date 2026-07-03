"""Phase 5 tests for security_warden posture checks.

PR adds:
  - `details.deeplink` on no_pod_admins / no_primary_recorded findings
    pointing at the new /admin/identity SPA page.
  - `details.ask` + `details.allowlist_starter` on exec_full_unscoped.
  - Structured `Remediation(kind="set_exec_allowlist", …)` on the exec
    finding so the alerts UI renders a "Set allowlist" button.

The existing severity assignments (alert / warn) stay — the actual
risk hasn't changed, just the operator's path to a fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from generators.security_warden.posture import (  # noqa: E402
    check_multi_user_posture,
    _check_exec_full_unscoped,
    _check_no_pod_admins,
    _check_no_primary_recorded,
)


# ── exec_full_unscoped ──────────────────────────────────────────────────────


def test_exec_full_unscoped_carries_remediation():
    """The finding now advertises a Remediation the UI can execute."""
    spec = _check_exec_full_unscoped(
        bot_id="team_bot_a",
        oc_config={"tools": {"exec": {"security": "full", "ask": "on-miss"}}},
    )
    assert spec is not None
    rem = spec.get("remediation")
    assert rem is not None
    assert rem.kind == "set_exec_allowlist"
    assert rem.params == {"bot_id": "team_bot_a", "commands": []}
    assert rem.label  # button text exists
    assert "allowlist" in rem.confirm.lower()


def test_exec_full_unscoped_details_include_ask_and_starter():
    spec = _check_exec_full_unscoped(
        bot_id="team_bot_a",
        oc_config={"tools": {"exec": {"security": "full", "ask": "on-miss"}}},
    )
    assert spec is not None
    details = spec["details"]
    assert details["ask"] == "on-miss"
    starter = details["allowlist_starter"]
    assert isinstance(starter, list) and starter
    # Reasonable safe-default basenames
    for cmd in ("ls", "cat", "echo"):
        assert cmd in starter


def test_exec_full_unscoped_with_existing_allowlist_returns_none():
    """A non-empty allowlist means the bot is scoped — no finding."""
    spec = _check_exec_full_unscoped(
        bot_id="team_bot_a",
        oc_config={"tools": {"exec": {
            "security": "full",
            "ask": "on-miss",
            "allowlist": ["ls", "cat"],
        }}},
    )
    assert spec is None


def test_exec_full_unscoped_severity_is_info_after_phase_a():
    """Post-Phase-A (2026-05-25 exec-deny migration), exec=full is the
    documented member-bot default. The signal still fires so the
    operator sees the current posture, but at INFO severity rather than
    ALERT — the combo no longer represents a misconfiguration to be
    fixed. Re-elevation per-bot is part of the Phase B
    (manifest-derived) roadmap. The body text + Remediation continue
    to point at the allowlist mode for operators who want tighter
    scoping. See docs/diagnosis-oc-noisy-advisories-2026-06-04.md and
    the module docstring at posture.py for the policy rationale."""
    spec = _check_exec_full_unscoped(
        bot_id="team_bot_a",
        oc_config={"tools": {"exec": {"security": "full", "ask": "on-miss"}}},
    )
    assert spec is not None
    assert spec["severity"] == "info"


def test_exec_full_unscoped_body_mentions_ask():
    """The body text surfaces the ask setting so the operator sees the
    full picture rather than just 'security=full'."""
    spec = _check_exec_full_unscoped(
        bot_id="team_bot_a",
        oc_config={"tools": {"exec": {"security": "full", "ask": "on-miss"}}},
    )
    assert spec is not None
    assert "on-miss" in spec["body"]


def test_exec_full_unscoped_handles_unset_ask():
    """Old/legacy configs without an explicit ask render with '(unset)'
    rather than crashing or printing 'None'."""
    spec = _check_exec_full_unscoped(
        bot_id="team_bot_a",
        oc_config={"tools": {"exec": {"security": "full"}}},
    )
    assert spec is not None
    assert "(unset)" in spec["body"]
    assert spec["details"]["ask"] == "(unset)"


# ── no_pod_admins ──────────────────────────────────────────────────────────


def test_no_pod_admins_carries_deeplink():
    """Finding deeplinks to the Identity page so the UI can route the
    operator directly to the claim form."""
    spec = _check_no_pod_admins({"pod": {}}, "team_bot_a")
    assert spec is not None
    deeplink = spec["details"].get("deeplink")
    assert deeplink and deeplink.startswith("/admin/identity")
    assert "claim-admin" in deeplink
    assert "team_bot_a" in deeplink


def test_no_pod_admins_returns_none_when_admin_exists():
    spec = _check_no_pod_admins(
        {"pod": {"admins": {"external_ids": {"slack": ["U123"]}}}},
        "team_bot_a",
    )
    assert spec is None


# ── no_primary_recorded ────────────────────────────────────────────────────


def test_no_primary_recorded_carries_deeplink():
    spec = _check_no_primary_recorded(
        {"bots": {"team_bot_c": {"primary_user": {"external_ids": {}}}}},
        "team_bot_c",
    )
    assert spec is not None
    deeplink = spec["details"].get("deeplink")
    assert deeplink and deeplink.startswith("/admin/identity")
    assert "claim-primary" in deeplink
    assert "team_bot_c" in deeplink


def test_no_primary_recorded_returns_none_when_recorded():
    spec = _check_no_primary_recorded(
        {"bots": {"team_bot_c": {"primary_user": {"external_ids": {"slack": "U456"}}}}},
        "team_bot_c",
    )
    assert spec is None


# ── integration: check_multi_user_posture aggregation ──────────────────────


def test_posture_emits_three_specs_for_team_bot_a_baseline_state():
    """A multi-user bot with no admin / no primary / exec=full no
    allowlist should emit all three findings — matches the empirical
    team_bot_a + team_bot_c state we found on the mini."""
    network = {
        "bots": {
            "team_bot_a": {
                "multiUser": True,
                "primary_user": {"external_ids": {}},
            }
        },
        "pod": {"admins": {"external_ids": {}}},
    }
    oc = {"tools": {"exec": {"security": "full", "ask": "on-miss"}}}
    specs = check_multi_user_posture(
        bot_id="team_bot_a", network=network, oc_config=oc,
    )
    types = sorted(s["type"] for s in specs)
    assert types == [
        "multi_user_exec_full_unscoped",
        "multi_user_no_pod_admins",
        "multi_user_no_primary_recorded",
    ]


def test_posture_no_op_on_single_user():
    """Single-user bots have nothing to check — their macOS user IS the
    operator, so exec=full is intentionally fine."""
    network = {"bots": {"security_bot": {"multiUser": False}}}
    specs = check_multi_user_posture(
        bot_id="security_bot", network=network,
        oc_config={"tools": {"exec": {"security": "full"}}},
    )
    assert specs == []
