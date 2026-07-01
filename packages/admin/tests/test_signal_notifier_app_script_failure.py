"""Tests for the signal_notifier + catalog wiring of app_script_failure_audit.

Spec: docs/spec-agent-freelance-bypass-2026-06-05.md (the invoked-but-failed
sibling of the agent-bypass gap).

Verifies the three load-bearing wiring changes that turn the new
``app_script_failure`` Signal into operator-facing chat notifications:

1. ``app_script_failure_audit`` is NOT in the deny-list
   (``_DIRECT_DISPATCH_PRODUCERS``) — it uses ``signals.store.observe()``,
   not ``dispatcher.send()``, so under deny-list-by-default its Signals
   reach chat automatically.

2. ``_catalog_event_for_signal`` maps the producer to the bespoke
   ``system.app_script_failure`` catalog event (and doesn't regress the
   neighbouring producer mappings).

3. The catalog event itself is registered with a working body_template,
   sample_payload, and producer_source, and points at the plugin_intercept
   remediation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.alerts import catalog as cat  # noqa: E402
from evolve_admin.alerts import signal_notifier as sn  # noqa: E402


def _sig(producer: str, sig_type: str) -> SimpleNamespace:
    return SimpleNamespace(producer=producer, type=sig_type)


# ── Deny-list guard ───────────────────────────────────────────────────────


def test_app_script_failure_audit_not_in_denylist() -> None:
    """Producer uses observe(), not dispatcher.send() — must stay loud."""
    assert "app_script_failure_audit" not in sn._DIRECT_DISPATCH_PRODUCERS


# ── Catalog event mapping ─────────────────────────────────────────────────


def test_app_script_failure_routes_to_bespoke_event() -> None:
    sig = _sig("app_script_failure_audit", "app_script_failure")
    assert sn._catalog_event_for_signal(sig) == "system.app_script_failure"


def test_neighbouring_producer_mappings_not_regressed() -> None:
    """The new branch must not perturb the app_structural_verifier / oc_cli
    mappings that sit beside it."""
    assert sn._catalog_event_for_signal(
        _sig("app_structural_verifier", "openclaw_cron_error")
    ) == "system.app_scheduled_work_failure"
    assert sn._catalog_event_for_signal(
        _sig("app_structural_verifier", "file_missing")
    ) == "security.audit_finding"
    assert sn._catalog_event_for_signal(
        _sig("oc_cli", "cli_misinvocation")
    ) == "system.oc_cli_misinvocation"


# ── Catalog event registration ────────────────────────────────────────────


def test_app_script_failure_event_exists_and_is_registered() -> None:
    entry = cat.by_key("system.app_script_failure")
    assert entry is not None
    assert entry.producer_source == "app_script_failure_audit"
    assert entry.category is cat.Category.SYSTEM
    assert entry.label
    assert entry.description
    assert entry.default_enabled is True
    # Remediation guidance is part of the operator-facing description.
    assert "plugin_intercept" in entry.description


def test_app_script_failure_renders_from_sample_payload() -> None:
    entry = cat.by_key("system.app_script_failure")
    rendered = cat.render_event(entry, entry.sample_payload)
    assert rendered, "render_event returned empty"
    assert "personal-bot" in rendered                  # bot_id
    assert "personal-bot-task-manager" in rendered      # app_id
    assert "(agent) failed" in rendered          # the leaked chip, in the summary
    # The action breadcrumb resolves its {bot_id}/{app_id} placeholders.
    assert "Bots → personal-bot → Apps → personal-bot-task-manager" in rendered


def test_app_script_failure_producer_is_a_known_source() -> None:
    """The catalog's producer_source set must include the new producer so the
    test_alerts_catalog known-producers guard stays green."""
    assert "app_script_failure_audit" in cat.known_producer_sources()
