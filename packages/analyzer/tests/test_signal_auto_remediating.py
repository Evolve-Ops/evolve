"""tests/test_signal_auto_remediating.py — the auto-remediating condition class.

Falsifiable proof artifacts for the registry half of
internal/spec-delta-transient-delivery-grace-2026-06-26.md (L2). The notify-side
wiring (signal_notifier suppression) is proven in
packages/admin/tests/test_alerts_signal_notifier.py.

Proof matrix:
  * pod_perms_drift is registered with the 30-min default self-heal window.
  * an unregistered type returns None (page on the normal schedule).
  * should_suppress: a registered warn inside its window is suppressed; the
    same warn past its window is not.
  * alert/critical is NEVER suppressed, even for a registered type — the
    keystone composition invariant.
  * a None / unknown firing age fails OPEN (page).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from signals import auto_remediating as ar  # noqa: E402


def test_pod_perms_drift_is_registered_with_default_window():
    assert ar.self_heal_window("pod_perms_drift") == ar.DEFAULT_SELF_HEAL_WINDOW_SECONDS
    # And the default is at least one deploy cycle (~15 min) of headroom.
    assert ar.DEFAULT_SELF_HEAL_WINDOW_SECONDS >= 900


def test_unregistered_type_has_no_window():
    assert ar.self_heal_window("pod_health_gateways") is None
    assert ar.self_heal_window(None) is None


def test_warn_inside_window_is_suppressed():
    assert ar.should_suppress(
        type_="pod_perms_drift", severity="warn", firing_age_seconds=600,
    ) is True


def test_warn_past_window_is_not_suppressed():
    assert ar.should_suppress(
        type_="pod_perms_drift", severity="warn",
        firing_age_seconds=ar.DEFAULT_SELF_HEAL_WINDOW_SECONDS + 1,
    ) is False


def test_alert_severity_never_suppressed_even_when_registered():
    # Keystone invariant: a registered type at alert severity, fresh, still
    # pages. should_suppress must return False purely on the severity guard.
    assert ar.should_suppress(
        type_="pod_perms_drift", severity="alert", firing_age_seconds=1,
    ) is False


def test_unregistered_type_never_suppressed():
    assert ar.should_suppress(
        type_="pod_health_gateways", severity="warn", firing_age_seconds=1,
    ) is False


def test_unknown_age_fails_open():
    # A missing firing-since stamp must never silently swallow the condition.
    assert ar.should_suppress(
        type_="pod_perms_drift", severity="warn", firing_age_seconds=None,
    ) is False
