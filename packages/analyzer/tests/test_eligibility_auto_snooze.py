"""tests/test_eligibility_auto_snooze.py — auto-snooze classification.

The auto-snooze path quiets low-priority, non-security, non-active
signals so they stop crowding the Home narrative. Independent of the
remediation-based tier_floor — a signal can have no remediation
(tier_floor=ask) but still auto-snooze when noisy.

Spec: docs/spec-severity-framework-2026-05-18.md (auto-snooze in the
authority axis).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import eligibility as elg  # noqa: E402


def _sig(*, vector="operations", magnitude=1, active=False, remediation=None) -> dict:
    out: dict = {
        "id": "s1",
        "severity_framework": {"vector": vector, "magnitude": magnitude},
        "details": {},
    }
    if active:
        out["details"]["severity_active"] = True
    if remediation is not None:
        out["remediation"] = remediation
    return out


# ── Core auto-snooze rule ────────────────────────────────────────────────────


def test_low_mag_non_active_non_security_is_snooze_eligible():
    """The canonical noise case: operations:1, not active, no remediation."""
    e = elg.classify_signal(_sig(vector="operations", magnitude=1, active=False))
    assert e.auto_snooze is True
    assert e.tier_floor == "ask"   # no remediation → no auto-fix path
    assert "auto-snoozable" in e.reason


def test_magnitude_0_is_snooze_eligible():
    e = elg.classify_signal(_sig(vector="operations", magnitude=0))
    assert e.auto_snooze is True


def test_magnitude_2_blocks_auto_snooze():
    """Magnitude 2+ is too important to silence — operator should see it."""
    e = elg.classify_signal(_sig(vector="operations", magnitude=2))
    assert e.auto_snooze is False


def test_security_vector_never_auto_snoozes():
    """Even magnitude 0 security stays visible — security findings always
    surface so the operator can decide."""
    e = elg.classify_signal(_sig(vector="security", magnitude=1))
    assert e.auto_snooze is False


def test_severity_active_blocks_auto_snooze():
    """Active outages stay visible even at low magnitude — if it's
    currently firing the operator wants to know."""
    e = elg.classify_signal(_sig(vector="operations", magnitude=1, active=True))
    assert e.auto_snooze is False


def test_quality_vector_low_mag_eligible():
    """bot_unused / classifier-drift style findings — quality:0 → snooze."""
    e = elg.classify_signal(_sig(vector="quality", magnitude=0))
    assert e.auto_snooze is True


def test_cost_vector_low_mag_eligible():
    """A cost:1 advisory (trending up but contained) is fine to quiet."""
    e = elg.classify_signal(_sig(vector="cost", magnitude=1))
    assert e.auto_snooze is True


# ── Interaction with remediation paths ──────────────────────────────────────


def test_high_risk_remediation_with_low_mag_still_snoozes():
    """A signal with a high-risk fix (security_warden-style) but low
    magnitude is independent: tier_floor stays 'ask' for the fix, but
    auto_snooze can still apply. Edge case kept explicit because the
    fix and the snooze are separate decisions."""
    e = elg.classify_signal(_sig(
        vector="operations", magnitude=1,
        remediation={"kind": "set_exec_security", "params": {}},
    ))
    # high-risk handler → ask (never auto-fires)
    assert e.tier_floor == "ask"
    # but the signal is still low-priority noise
    assert e.auto_snooze is True


def test_security_critical_explicit_branch_disables_auto_snooze():
    """security_critical short-circuits with auto_snooze=False even when
    other inputs happen to match — defense-in-depth on the safety rail."""
    e = elg.classify_signal({
        "id": "s1",
        "severity_framework": {"vector": "security", "magnitude": 4},
        "remediation": {"kind": "reset_baseline", "params": {}},
        "details": {},
    })
    assert e.tier_floor == "ask"
    assert e.auto_snooze is False


def test_decidable_remediation_path_unchanged():
    """An ordinary tier-c-eligible signal with a low-risk fix keeps its
    fix path; the snooze hint is independent and may also be set
    when noise criteria match."""
    e = elg.classify_signal(_sig(
        vector="operations", magnitude=1,
        remediation={"kind": "reset_baseline", "params": {}},
    ))
    assert e.tier_floor == "auto-small"   # low fix-risk + low mag
    assert e.auto_snooze is True          # also auto-snoozable
    # When both paths exist, the JS auto-act loop prefers Path A
    # (remediation) over Path B (snooze) — the snooze flag is a
    # fallback for signals with no fix available.


# ── Shape contract ──────────────────────────────────────────────────────────


def test_to_dict_includes_auto_snooze():
    e = elg.classify_signal(_sig(vector="operations", magnitude=1))
    d = e.to_dict()
    assert "auto_snooze" in d
    assert d["auto_snooze"] is True


def test_auto_snooze_default_false_in_named_tuple():
    """When no signal classification has been run, the field defaults
    to False on a bare Eligibility — keeps callers safe from None."""
    e = elg.Eligibility(fix_risk="low", decidable=True, tier_floor="auto-small")
    assert e.auto_snooze is False


def test_auto_snooze_duration_constant_is_7d():
    """The JS loop uses '7d' literally — keep this assertion to flag any
    drift between the constant here and the duration string in the
    JS auto-snooze path."""
    assert elg.AUTO_SNOOZE_DURATION == "7d"


# ── Canonical noise scenarios from the screenshots ──────────────────────────


def test_audit_stale_pattern_is_snooze_eligible():
    """audit_stale-class findings (operations:1, not active) — the
    canonical noise."""
    e = elg.classify_signal({
        "id": "s1",
        "producer": "pod_report",
        "severity": "warn",
        "severity_framework": {"vector": "operations", "magnitude": 1},
        "details": {"text": "Audit results from 90 min ago"},
    })
    assert e.auto_snooze is True


def test_bot_recovered_pattern_is_snooze_eligible():
    """bot_recovered info-tier (operations:0) — historical record,
    perfect candidate for auto-snooze."""
    e = elg.classify_signal({
        "id": "s1",
        "producer": "bot_recovery_monitor",
        "severity": "info",
        "severity_framework": {"vector": "operations", "magnitude": 0},
        "details": {"provider": "anthropic", "condition": "context_overflow"},
    })
    assert e.auto_snooze is True


def test_version_behind_pattern_is_snooze_eligible():
    """version_behind from sysadmin_watchdog: operations:1, not active."""
    e = elg.classify_signal({
        "id": "s1",
        "producer": "sysadmin_watchdog",
        "severity": "warn",
        "severity_framework": {"vector": "operations", "magnitude": 1},
        "details": {"days_behind": 20},
    })
    assert e.auto_snooze is True


def test_gateway_down_pattern_not_snooze_eligible():
    """gateway_down (operations:2-3, severity_active) — must stay visible."""
    e = elg.classify_signal({
        "id": "s1",
        "producer": "sysadmin_watchdog",
        "severity": "alert",
        "severity_framework": {"vector": "operations", "magnitude": 3},
        "details": {"chronic": True, "severity_active": True},
    })
    assert e.auto_snooze is False
