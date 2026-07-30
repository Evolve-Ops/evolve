"""tests/test_sysadmin_watchdog_severity_tag.py — sysadmin_watchdog retrofit.

Verifies (vector, magnitude) on the seven Signal-kwargs factories
matches the spec at docs/spec-severity-framework-2026-05-18.md §2.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.sysadmin_watchdog import signals as sa_signals  # noqa: E402
import severity as sev  # noqa: E402


# ── Gateway down — transient vs chronic ──────────────────────────────────────


def test_gateway_down_transient_magnitude_2():
    spec = sa_signals.gateway_down_signal_kwargs(
        "team_bot_a", consecutive_failures=2, chronic=False,
    )
    assert spec["details"]["vector"] == "operations"
    assert spec["details"]["magnitude"] == 2
    assert spec["details"]["severity_active"] is True


def test_gateway_down_chronic_magnitude_3():
    spec = sa_signals.gateway_down_signal_kwargs(
        "team_bot_a", consecutive_failures=10, chronic=True,
    )
    assert spec["details"]["magnitude"] == 3


# ── Other operations-vector signals ──────────────────────────────────────────


def test_plugin_missing_magnitude_3():
    spec = sa_signals.plugin_missing_signal_kwargs("team_bot_a")
    assert spec["details"]["vector"] == "operations"
    assert spec["details"]["magnitude"] == 3
    assert spec["details"]["severity_active"] is True


def test_launchd_not_loaded_magnitude_3():
    spec = sa_signals.launchd_not_loaded_signal_kwargs("team_bot_a")
    assert spec["details"]["magnitude"] == 3


def test_openclaw_config_invalid_magnitude_3():
    spec = sa_signals.openclaw_config_invalid_signal_kwargs("team_bot_a")
    assert spec["details"]["magnitude"] == 3


def test_user_missing_magnitude_3_keeps_expected_user():
    spec = sa_signals.user_missing_signal_kwargs("team_bot_a", user="team_bot_a-bot")
    assert spec["details"]["magnitude"] == 3
    # Make sure the retrofit didn't drop the existing payload field
    assert spec["details"]["expected_user"] == "team_bot_a-bot"


def test_acl_drift_magnitude_2():
    spec = sa_signals.acl_drift_signal_kwargs("team_bot_a")
    assert spec["details"]["vector"] == "operations"
    assert spec["details"]["magnitude"] == 2
    assert spec["details"]["severity_active"] is True


def test_acl_drift_body_derives_platform_home_not_users_literal():
    """The ACL-drift alert body must derive the bot home from the platform
    seam, not a `/Users/` literal. On a Linux pod the home is /home/<bot>;
    a hardcoded macOS path made the fresh-evo-pod alert read 'lost read
    access to /Users/evo/.openclaw/' on a box where that path doesn't exist.
    """
    from platform_profile import LINUX, MACOS, get_profile, set_profile

    prior = get_profile()
    try:
        set_profile(LINUX)
        body = sa_signals.acl_drift_signal_kwargs("evo")["body"]
        # pwd.getpwnam('evo') fails on the dev box → profile fallback
        # (/home/evo under the Linux profile).
        assert "/home/evo/.openclaw/" in body
        assert "/Users/" not in body

        set_profile(MACOS)
        body_mac = sa_signals.acl_drift_signal_kwargs("evo")["body"]
        assert "/Users/evo/.openclaw/" in body_mac
    finally:
        set_profile(prior)


def test_version_behind_magnitude_1_not_active():
    spec = sa_signals.version_behind_signal_kwargs("team_bot_a", days_behind=20)
    assert spec["details"]["vector"] == "operations"
    assert spec["details"]["magnitude"] == 1
    # Release lag is not an active outage — bot still functions
    assert spec["details"].get("severity_active") is None
    # Existing payload field preserved
    assert spec["details"]["days_behind"] == 20


# ── Resolver end-to-end ──────────────────────────────────────────────────────


def test_gateway_chronic_resolves_to_operations_magnitude_3():
    spec = sa_signals.gateway_down_signal_kwargs(
        "team_bot_a", consecutive_failures=12, chronic=True,
    )
    rating = sev.resolve_severity(spec)
    assert rating.vector == "operations"
    assert rating.magnitude == 3


def test_gateway_chronic_active_clears_lead_when_pod_weight_set():
    """A chronic gateway down on one bot won't lead by default
    (mag 3 × bot × active = 3.9) but should clear lead when the operator
    sets operations weight ≥ 1.8 (3.9 × 1.8 = 7.02)."""
    spec = sa_signals.gateway_down_signal_kwargs(
        "team_bot_a", consecutive_failures=12, chronic=True,
    )
    rating = sev.resolve_severity(spec)
    weights = {"security": 1.0, "cost": 1.0, "operations": 1.8, "quality": 1.0}
    score = sev.compose_priority(
        rating, scope="bot", is_active_outage=True, pod_weights=weights,
    )
    assert sev.priority_bucket(score) == "lead"


def test_version_behind_lands_in_small_bucket():
    """Release lag should never crowd the main narrative — mag 1, bot
    scope, not active → priority 1.0 → small."""
    spec = sa_signals.version_behind_signal_kwargs("team_bot_a", days_behind=30)
    rating = sev.resolve_severity(spec)
    score = sev.compose_priority(rating, scope="bot")
    assert sev.priority_bucket(score) == "small"
