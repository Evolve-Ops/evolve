"""Phase C — heal.py routes its three alerts through alerts.dispatcher.

Pins the migration contract:

  - _alert_backup_drift  → catalog_event=security.config_drift
  - _alert_failure       → catalog_event=system.gateway_autorestart_failed
  - _alert_watchdog_event→ catalog_event=system.watchdog_event
  - all use source="heal" so alerts.heal.enabled gates the trio
  - subprocess.run is never called for openclaw (regression guard —
    if a future edit puts an inline subprocess back, this fails fast)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import heal  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import (
        DispatchOutcome, DispatchResult, Severity,
    )

    captured: list = []

    def fake_send(*, shared_dir, network, source, severity,
                  message=None, payload=None,
                  dedup_key=None, catalog_event=None, **_kw):
        captured.append({
            "shared_dir": shared_dir, "source": source,
            "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=DispatchResult.SENT, source=source, severity=severity,
            dedup_key=dedup_key, catalog_event=catalog_event,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    def fail_run(*a, **kw):
        raise AssertionError(
            f"heal must not subprocess.run after Phase C: args={a}"
        )
    monkeypatch.setattr(heal.subprocess, "run", fail_run)

    return {
        "captured": captured,
        "shared_dir": tmp_path / "evolve",
        "config": {
            "alerts": {"channel": "telegram", "chatId": "12345"},
            "sharedDir": str(tmp_path / "evolve"),
        },
        "Severity": Severity,
    }


def test_alert_backup_drift_routes_through_dispatcher(env):
    heal._alert_backup_drift(
        "team_bot_a", ["openclaw.json modified outside deploy"], env["config"],
    )
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "heal"
    assert call["catalog_event"] == "security.config_drift"
    # dedup_key includes a 16-hex content fingerprint of the sorted drift
    # list (see heal._alert_backup_drift) so same drift dedupes under
    # cooldown but partial fixes re-alert.
    assert call["dedup_key"].startswith("heal/config_drift/team_bot_a/")
    assert len(call["dedup_key"]) == len("heal/config_drift/team_bot_a/") + 16
    assert call["severity"] == env["Severity"].ERROR
    # Phase F2: passes payload; catalog body_template renders.
    assert call["message"] is None
    assert call["payload"] == {
        "bot_id": "team_bot_a",
        "drift_summary": "openclaw.json modified outside deploy",
    }


def test_alert_failure_routes_through_dispatcher(env):
    """Phase F4: _alert_failure now passes structured payload; catalog
    body_template renders "🔴 {bot_id}'s gateway is still down — auto-
    restart failed / heal.py couldn't bring the gateway back up on
    port N." plus the cli action."""
    heal._alert_failure("admin_bot", 19000, env["config"])
    call = env["captured"][0]
    assert call["source"] == "heal"
    assert call["catalog_event"] == "system.gateway_autorestart_failed"
    assert call["dedup_key"] == "heal/restart_failed/admin_bot"
    assert call["severity"] == env["Severity"].ERROR
    assert call["message"] is None
    assert call["payload"] == {"bot_id": "admin_bot", "port": 19000}


def test_alert_watchdog_event_routes_through_dispatcher(env):
    heal._alert_watchdog_event("🟡 Watchdog: admin_bot disk 94%", env["config"])
    call = env["captured"][0]
    assert call["source"] == "heal"
    assert call["catalog_event"] == "system.watchdog_event"
    assert call["dedup_key"].startswith("heal/watchdog/")
    # dedup_key hash is stable per message
    assert len(call["dedup_key"]) == len("heal/watchdog/") + 16
    assert call["severity"] == env["Severity"].WARNING


def test_watchdog_dedup_key_changes_with_message(env):
    """Different watchdog incidents → different dedup_keys, so distinct
    events all reach the operator (not just the first per cooldown
    window)."""
    heal._alert_watchdog_event("A", env["config"])
    heal._alert_watchdog_event("B", env["config"])
    keys = [c["dedup_key"] for c in env["captured"]]
    assert keys[0] != keys[1]


def test_dispatcher_recognizes_heal_source():
    """heal must be in the dispatcher's known sources so the operator
    can toggle alerts.heal.enabled from the Config page. Drift here
    means a new heal call site shows up as "unknown source" in the
    audit log."""
    from evolve_admin.alerts.dispatcher import known_sources
    assert "heal" in set(known_sources())
