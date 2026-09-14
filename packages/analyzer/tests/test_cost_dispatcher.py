"""Phase C — cost.py routes key-sync staleness through dispatcher AND
retires the spend-threshold path (which duplicated spend_alert.py).

Pins:

  _check_key_sync_staleness:
    - when stale bots exist, routes through dispatcher with
      catalog_event=security.key_rotation_overdue, source="cost"
    - dedup_key includes a hash of the stale-bots set so a CHANGE in
      the set (more bots stale or some recovered) produces a fresh push
    - when no stale bots, no dispatch at all (no message storm on
      healthy state)

  Retirement of _alert_spend_threshold:
    - the function is gone (importing it raises AttributeError)
    - _check_and_propose no longer pushes a duplicate spend alert
      when the report's anomaly_reason is "exceeds threshold"
    - spend_alert.py remains the canonical owner of cost.daily_threshold
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

import cost  # noqa: E402


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
            dedup_key=dedup_key, catalog_event=catalog_event,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    shared = tmp_path / "evolve"
    shared.mkdir()
    return {
        "captured": captured,
        "shared_dir": shared,
        "config": {
            "alerts": {"channel": "telegram", "chatId": "12345"},
            "members": ["team_bot_a", "admin_bot"],
            "sharedDir": str(shared),
        },
        "Severity": Severity,
    }


# ── _check_key_sync_staleness migration ────────────────────────────────────


def _seed_sync_log(shared_dir, log: dict):
    """Write a sync log that the Keystore class loads."""
    keystore_dir = shared_dir / "keystore"
    keystore_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    # Path comes from Keystore.__init__ — kept as sync-log.json (hyphen).
    (keystore_dir / "sync-log.json").write_text(_json.dumps(log))


def test_key_sync_routes_through_dispatcher_when_bots_stale(env, monkeypatch):
    """A stale-bot fingerprint must route to security.key_rotation_overdue."""
    from datetime import datetime, timezone, timedelta
    # Team_bot_a synced 30 days ago (stale, threshold is 7d); admin_bot never synced (also stale).
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _seed_sync_log(env["shared_dir"], {"team_bot_a": stale_ts})
    # admin_bot omitted → treated as stale

    cost._check_key_sync_staleness(env["shared_dir"], env["config"])

    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "cost"
    assert call["catalog_event"] == "security.key_rotation_overdue"
    assert call["severity"] == env["Severity"].WARNING
    assert call["dedup_key"].startswith("cost/key_rotation/")
    # Phase F2: passes payload; catalog body_template + cli action render.
    assert call["message"] is None
    assert "team_bot_a" in call["payload"]["stale_bots"]
    assert "admin_bot" in call["payload"]["stale_bots"]
    assert call["payload"]["threshold_days"] == 7


def test_no_dispatch_when_all_bots_synced(env):
    """Healthy state must not generate a push."""
    from datetime import datetime, timezone
    fresh_ts = datetime.now(timezone.utc).isoformat()
    _seed_sync_log(env["shared_dir"], {"team_bot_a": fresh_ts, "admin_bot": fresh_ts})

    cost._check_key_sync_staleness(env["shared_dir"], env["config"])
    assert env["captured"] == []


def test_dedup_key_changes_with_stale_bot_set(env):
    """Same stale set → same dedup_key (cooldown applies). Different
    stale set → different key (fresh push so the operator sees the
    new finding immediately)."""
    from datetime import datetime, timezone, timedelta
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()

    # Only team_bot_a stale.
    _seed_sync_log(env["shared_dir"], {"team_bot_a": stale_ts, "admin_bot": fresh_ts})
    cost._check_key_sync_staleness(env["shared_dir"], env["config"])
    key_only_team_bot_a = env["captured"][-1]["dedup_key"]

    # Both stale now.
    _seed_sync_log(env["shared_dir"], {"team_bot_a": stale_ts, "admin_bot": stale_ts})
    cost._check_key_sync_staleness(env["shared_dir"], env["config"])
    key_both = env["captured"][-1]["dedup_key"]

    assert key_only_team_bot_a != key_both, (
        "A change in the stale-bots set must produce a fresh dedup_key "
        "so the operator sees the new finding immediately"
    )


def test_dispatcher_recognizes_cost_source():
    from evolve_admin.alerts.dispatcher import known_sources
    assert "cost" in set(known_sources())


# ── _alert_spend_threshold retirement ──────────────────────────────────────


def test_alert_spend_threshold_function_is_gone():
    """Pin the retirement: re-introducing _alert_spend_threshold would
    re-create the audit/spend_alert/cost.py triple-emit visible in the
    original screenshot evidence."""
    assert not hasattr(cost, "_alert_spend_threshold"), (
        "cost._alert_spend_threshold was retired in Phase C — "
        "spend_alert.py owns cost.daily_threshold now"
    )


def test_check_and_propose_does_not_push_spend_alert(env):
    """The anomaly_reason='exceeds threshold' branch in _check_and_propose
    must NOT dispatch — spend_alert.py is the canonical pusher for
    cost.daily_threshold and runs on its own cadence."""
    # Build a minimal anomalous report.
    report = cost.BotCostReport(
        bot_id="admin_bot",
        date="2026-05-10",
        api_spend_usd=5.96,
        billing_mode="pay-per-use",
        sessions_today=0,
        model_configured=None,
        model_expected=None,
        source="metrics",
        anomaly=True,
        anomaly_reason="${report.api_spend_usd:.2f} exceeds threshold ${threshold:.2f}",
    )
    cost._check_and_propose(report, env["shared_dir"], env["config"])
    # No dispatcher call at all from the spend path.
    spend_calls = [
        c for c in env["captured"]
        if c.get("catalog_event") == "cost.daily_threshold"
    ]
    assert spend_calls == [], (
        f"_check_and_propose must not dispatch cost.daily_threshold — "
        f"got {spend_calls}"
    )
