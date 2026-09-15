"""Phase 3d — cron_alert routes through alerts.dispatcher.

Pins:

  - _send_alert hands off to dispatcher.send with source="cron_alert"
  - dedup_key namespaces by cron label so stalls on different jobs
    don't collide
  - severity is WARNING (a stalled cron is not security-CRITICAL)
  - dry_run path skips both dispatcher and subprocess
  - the existing per-day flag-file dedup remains the primary idempotency;
    dispatcher cooldown is a safety net layer
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

import cron_alert  # noqa: E402


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
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=DispatchResult.SENT, source=source, severity=severity,
            dedup_key=dedup_key, channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    def fail_run(*a, **kw):
        raise AssertionError(
            f"cron_alert must not subprocess.run after Phase 3d: {a}"
        )
    monkeypatch.setattr(cron_alert.subprocess, "run", fail_run)

    return {
        "captured": captured,
        "shared_dir": tmp_path / "evolve",
        "network": {"alerts": {"channel": "telegram", "chatId": "12345"}},
        "Severity": Severity,
    }


def test_send_alert_routes_through_dispatcher(env):
    """Phase F4: _send_alert takes structured bot_id/label/reason and
    passes them as payload; catalog body_template renders the body."""
    cron_alert._send_alert(
        shared_dir=env["shared_dir"], network=env["network"],
        bot_id="team_bot_a", label="ai.evolve.team_bot_a.measure",
        reason="last run 51h ago (threshold: 2d)",
        dry_run=False,
    )
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "cron_alert"
    assert call["dedup_key"] == "cron_alert/ai.evolve.team_bot_a.measure"
    assert call["catalog_event"] == "system.stalled_cron"
    assert call["severity"] == env["Severity"].WARNING
    assert call["message"] is None
    assert call["payload"] == {
        "bot_id": "team_bot_a",
        "label": "ai.evolve.team_bot_a.measure",
        "reason": "last run 51h ago (threshold: 2d)",
    }


def test_dedup_key_is_per_label(env):
    """Different stalled jobs must produce different dedup_keys so the
    operator gets a notification for each (not just the first)."""
    cron_alert._send_alert(
        shared_dir=env["shared_dir"], network=env["network"],
        bot_id="team_bot_a", label="ai.evolve.team_bot_a.measure", reason="r1",
        dry_run=False,
    )
    cron_alert._send_alert(
        shared_dir=env["shared_dir"], network=env["network"],
        bot_id="admin_bot", label="ai.evolve.admin_bot.heal", reason="r2",
        dry_run=False,
    )
    keys = [c["dedup_key"] for c in env["captured"]]
    assert keys == [
        "cron_alert/ai.evolve.team_bot_a.measure",
        "cron_alert/ai.evolve.admin_bot.heal",
    ]


def test_dry_run_skips_dispatcher(env):
    cron_alert._send_alert(
        shared_dir=env["shared_dir"], network=env["network"],
        bot_id="team_bot_a", label="any", reason="any",
        dry_run=True,
    )
    assert env["captured"] == []


# ── §11 skip guard (delivery-monitor boundary) ───────────────────────────────


_GUARD_MANIFEST = {
    "id": "test-app",
    "display_name": "Test App",
    "status": "active",
    "scheduled_actions": [{
        "id": "calendar-action",
        "mechanism": "launchd",
        "install": {
            "plist_label": "ai.evolve.${bot_id}.test-app",
            "schedule": {"cron": {"Hour": 7, "Minute": 0}},
        },
        "outputs": [{"kind": "session_message", "channel": "primary"}],
    }],
}


def _install_guard_manifest(home):
    import json
    mdir = home / ".openclaw" / "workspace" / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "test-app.json").write_text(json.dumps(_GUARD_MANIFEST))


def test_delivery_monitored_keys_maps_labels_and_action_ids(tmp_path, monkeypatch):
    import delivery_monitor as dm
    home = tmp_path / "home"
    _install_guard_manifest(home)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    keys = cron_alert._delivery_monitored_keys({"bots": {"testbot": {}}})
    assert ("testbot", "calendar-action") in keys
    assert ("testbot", "ai.evolve.home.test-app") in keys


def test_watched_cron_covered_by_delivery_monitor_is_skipped(
    tmp_path, monkeypatch, capsys, env,
):
    """A watched cron that maps to a monitored scheduled action must not
    dead-man-page here — the delivery monitor owns it per-window, and a
    single miss must never double-page (§11)."""
    import delivery_monitor as dm
    from runtime import agent_runtime as ar

    home = tmp_path / "home"
    _install_guard_manifest(home)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)

    fake = ar.FakeRuntime()
    # Two watched jobs, both never-run (normally both would alert):
    # one is the monitored action's label, one is plain infra.
    fake.seed("testbot", cron_list=[
        {"id": "j1", "label": "ai.evolve.home.test-app"},
        {"id": "j2", "label": "ai.evolve.testbot.infra-job"},
    ])
    ar.set_runtime(fake)
    try:
        config = {
            "members": ["testbot"],
            "bots": {"testbot": {}},
            "sharedDir": str(tmp_path / "shared"),
            "alerts": {"watchedCrons": ["ai.evolve.*"],
                       "cronSilenceThresholdDays": 2},
        }
        fired = cron_alert.check_cron_alerts(config, dry_run=False)
    finally:
        ar.set_runtime(None)

    assert fired == 1  # only the infra job pages
    labels = [c["payload"]["label"] for c in env["captured"]]
    assert labels == ["ai.evolve.testbot.infra-job"]
    assert "covered by delivery monitor, skipping" in capsys.readouterr().out
