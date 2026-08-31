"""Tests for the v20 signal_notifier wiring of app_structural_verifier.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §17.3 + Q32 +
PR 12 (bot-side awareness — proactive notifications via signal_notifier).

Verifies the three small but load-bearing changes that turn the new
openclaw cron status assertion into actual user-facing chat notifications:

1. ``app_structural_verifier`` is NOT in the deny-list
   (``_DIRECT_DISPATCH_PRODUCERS``) — under deny-list-by-default its Signals
   reach chat automatically without any allowlist entry.

2. ``_catalog_event_for_signal`` maps openclaw_cron_* assertions to the
   bespoke ``system.app_scheduled_work_failure`` catalog event added in
   PR 12. Other structural assertions fall through to the existing
   ``security.audit_finding`` umbrella.

3. The catalog event itself is registered with a working body_template,
   sample_payload, and producer_source — exercised by the existing
   catalog HTML-validation tests; this file adds the targeted unit
   coverage.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.alerts import catalog as cat  # noqa: E402
from evolve_admin.alerts import signal_notifier as sn  # noqa: E402


# ── Deny-list guard ───────────────────────────────────────────────────────

def test_app_structural_verifier_not_in_denylist() -> None:
    """app_structural_verifier uses signals.store.observe(), not
    dispatcher.send(). Under deny-list-by-default it is loud automatically
    — it must NOT appear in _DIRECT_DISPATCH_PRODUCERS (which would silence
    it)."""
    assert "app_structural_verifier" not in sn._DIRECT_DISPATCH_PRODUCERS


# ── Catalog event mapping ─────────────────────────────────────────────────

def _sig(producer: str, sig_type: str) -> SimpleNamespace:
    """Minimal Signal-shaped object matching what _catalog_event_for_signal
    reads. SimpleNamespace because the real Signal class has many fields
    we don't need here."""
    return SimpleNamespace(producer=producer, type=sig_type)


def test_openclaw_cron_error_routes_to_scheduled_work_failure() -> None:
    """The team-bot-a-task-worker production case. ``openclaw_cron_error``
    assertions surface as the bespoke
    ``system.app_scheduled_work_failure`` catalog event so operators
    subscribe to scheduled-work failures specifically (vs the broader
    security.audit_finding umbrella)."""
    sig = _sig("app_structural_verifier", "openclaw_cron_error")
    assert sn._catalog_event_for_signal(sig) == "system.app_scheduled_work_failure"


def test_openclaw_cron_skipped_routes_to_scheduled_work_failure() -> None:
    """personal-bot's 5-crons-all-skipped production case."""
    sig = _sig("app_structural_verifier", "openclaw_cron_skipped")
    assert sn._catalog_event_for_signal(sig) == "system.app_scheduled_work_failure"


def test_openclaw_cron_delivery_failure_routes_to_scheduled_work_failure() -> None:
    """The asymmetric 'work ok, delivery failed' case
    (team-bot-a-task-worker's actual failure mode)."""
    sig = _sig("app_structural_verifier", "openclaw_cron_delivery_failure")
    assert sn._catalog_event_for_signal(sig) == "system.app_scheduled_work_failure"


def test_other_structural_findings_route_to_audit_finding() -> None:
    """Non-cron findings (file_missing, sha drift, scheduled_action_*,
    etc.) route to the existing security.audit_finding umbrella, so
    operators who already mute that don't get sudden new noise."""
    for sig_type in ("file_missing", "file_sha_mismatch", "cron_script_missing",
                     "scheduled_action_anchor", "heartbeat_anchors_present"):
        sig = _sig("app_structural_verifier", sig_type)
        assert sn._catalog_event_for_signal(sig) == "security.audit_finding", (
            f"assertion {sig_type!r} should route to security.audit_finding"
        )


def test_unrelated_producers_still_route_correctly() -> None:
    """The new app_structural_verifier branch must not regress the
    existing producer mappings. Spot-check pod_health and oc_cli."""
    assert sn._catalog_event_for_signal(
        _sig("pod_health", "gateway_down")) == "system.gateway_state_change"
    assert sn._catalog_event_for_signal(
        _sig("oc_cli", "cli_misinvocation")) == "system.oc_cli_misinvocation"


# ── Catalog event registration ────────────────────────────────────────────

def test_app_scheduled_work_failure_event_exists() -> None:
    """The bespoke catalog event PR 12 adds is registered and discoverable
    via catalog.by_key."""
    entry = cat.by_key("system.app_scheduled_work_failure")
    assert entry is not None
    assert entry.producer_source == "app_structural_verifier"
    # Required user-facing strings are non-empty.
    assert entry.label
    assert entry.description
    # Default state — enabled, severity warning (criticals map per signal).
    assert entry.default_enabled is True


def test_app_scheduled_work_failure_renders_from_sample_payload() -> None:
    """The catalog event's body_template + action render successfully
    from the sample_payload. This is the offline guard that the catalog
    validation tests rely on; having a focused test here makes it easy
    to diagnose if the template ever drifts from the payload shape."""
    entry = cat.by_key("system.app_scheduled_work_failure")
    rendered = cat.render_event(entry, entry.sample_payload)
    assert rendered, "render_event returned empty"
    # Sanity-check the key fields appear in the rendered output.
    assert "team-bot-a" in rendered                       # bot_id
    assert "team-bot-a-task-management" in rendered       # app_id
    assert "team-bot-a-task-worker" in rendered or "Slack" in rendered  # summary content
