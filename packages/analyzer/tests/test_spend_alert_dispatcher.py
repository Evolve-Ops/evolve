"""Phase 3b — spend_alert routes through alerts.dispatcher.

Pins the migration contract:

  - send_alert / _maybe_send_weekly_summary all hand
    off to dispatcher.send instead of calling subprocess.run directly
  - dedup_keys are per-bot-per-day for daily threshold and cap alerts,
    per-iso-week for the weekly summary; this gives the dispatcher's
    cooldown a meaningful key without breaking the existing flag-file
    once-per-day idempotency
  - source="spend_alert" so the operator's alerts.spend_alert.enabled
    toggle gates all three, and the audit log carries a consistent
    source for forensics
  - return value of send_alert reflects DispatchResult.SENT (the caller
    writes the daily flag-file only on True; suppressed/failed leave
    the flag absent so the next eligible tick can retry)
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

import spend_alert  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Common dispatcher mock + capture list."""
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import (
        DispatchOutcome, DispatchResult, Severity,
    )

    captured: list = []
    next_result = {"value": DispatchResult.SENT}

    def fake_send(*, shared_dir, network, source, severity,
                  message=None, payload=None,
                  dedup_key=None, catalog_event=None, **_kw):
        captured.append({
            "shared_dir": shared_dir, "network": network,
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=next_result["value"], source=source, severity=severity,
            dedup_key=dedup_key, channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    # Belt-and-suspenders: any direct subprocess.run is a regression.
    def fail_run(*a, **kw):
        raise AssertionError(
            f"spend_alert must not subprocess.run after Phase 3b: args={a}"
        )
    monkeypatch.setattr(spend_alert.subprocess, "run", fail_run)

    network = {"alerts": {"channel": "telegram", "chatId": "12345"}}
    return {
        "captured": captured,
        "next_result": next_result,
        "network": network,
        "shared_dir": tmp_path / "evolve",
        "Severity": Severity,
        "DispatchResult": DispatchResult,
    }


def test_send_alert_routes_through_dispatcher_with_per_day_dedup_key(env):
    sent = spend_alert.send_alert(
        "admin_bot", 5.96, 5.0,
        shared_dir=env["shared_dir"], network=env["network"],
        today_iso="2026-05-09",
    )
    assert sent is True
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "spend_alert"
    assert call["severity"] == env["Severity"].WARNING
    assert call["dedup_key"] == "spend_alert/threshold/admin_bot/2026-05-09"
    assert call["catalog_event"] == "cost.daily_threshold"
    # Phase F3: payload-driven; catalog body_template renders.
    assert call["message"] is None
    assert call["payload"] == {
        "bot_id": "admin_bot", "amount": 5.96, "threshold": 5.0,
    }


def test_send_alert_returns_false_on_suppressed_disabled(env):
    env["next_result"]["value"] = env["DispatchResult"].SUPPRESSED_DISABLED
    sent = spend_alert.send_alert(
        "admin_bot", 5.96, 5.0,
        shared_dir=env["shared_dir"], network=env["network"],
        today_iso="2026-05-09",
    )
    assert sent is False, (
        "must return False so the daily flag-file isn't written; the next "
        "eligible tick after operator re-enable can retry"
    )


def test_send_alert_returns_false_on_failure(env):
    env["next_result"]["value"] = env["DispatchResult"].FAILED
    sent = spend_alert.send_alert(
        "admin_bot", 5.96, 5.0,
        shared_dir=env["shared_dir"], network=env["network"],
        today_iso="2026-05-09",
    )
    assert sent is False


# ``_send_cap_alert`` / ``cost.hard_cap_hit`` was removed with the pod-wide
# ``thresholds.dailySpendCapUsd`` branch — its only caller, and unreachable on
# any migrated pod. The hard-cap alert operators actually receive is
# ``cost.breaker_tripped`` from the L1 trip; its dedup-key namespace
# (``spend_alert/percap/...``) is pinned in test_spend_alert_per_bot_cap.py.


def test_weekly_summary_uses_iso_week_dedup_key(env, tmp_path):
    from datetime import date
    members = ["admin_bot", "team_bot_a"]
    today = date(2026, 5, 11)   # Monday of ISO week 2026-W20

    # Stub _weekly_spend so the test doesn't need real metrics files.
    def fake_weekly(_shared, _members, _today):
        return 6.40, {"admin_bot": 4.20, "team_bot_a": 2.20}
    import spend_alert as sa
    original = sa._weekly_spend
    try:
        sa._weekly_spend = fake_weekly
        sa._maybe_send_weekly_summary(
            shared_dir=tmp_path / "evolve",
            members=members,
            today=today,
            weekly_threshold=20.0,
            network=env["network"],
        )
    finally:
        sa._weekly_spend = original

    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["dedup_key"] == "spend_alert/weekly/2026-W20"  # ISO week containing 2026-05-11
    assert call["catalog_event"] == "cost.weekly_summary"
    assert call["severity"] == env["Severity"].INFO
    # Phase F3: payload-driven; catalog body_template renders.
    assert call["message"] is None
    assert call["payload"]["iso_week"] == "2026-W20"
    assert call["payload"]["total"] == 6.4
    assert "admin_bot: $4.20" in call["payload"]["per_bot_breakdown"]


def test_weekly_summary_writes_flag_only_when_dispatcher_sent(env, tmp_path):
    """If the dispatcher returns suppressed, the flag file must NOT be
    written — otherwise the operator re-enable doesn't cause a fresh
    summary push for the same week."""
    from datetime import date
    env["next_result"]["value"] = env["DispatchResult"].SUPPRESSED_DISABLED
    today = date(2026, 5, 11)

    import spend_alert as sa
    original = sa._weekly_spend
    try:
        sa._weekly_spend = lambda *_a: (6.40, {"admin_bot": 4.20})
        sa._maybe_send_weekly_summary(
            shared_dir=tmp_path / "evolve",
            members=["admin_bot"],
            today=today,
            weekly_threshold=20.0,
            network=env["network"],
        )
    finally:
        sa._weekly_spend = original

    flag = tmp_path / "evolve" / "alerts" / "weekly-summary-2026-W20.flag"
    assert not flag.exists(), (
        "flag must be absent on dispatcher suppression so a re-enable "
        "this same week can produce a fresh summary"
    )
