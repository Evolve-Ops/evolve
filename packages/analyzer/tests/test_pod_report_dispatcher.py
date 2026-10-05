"""Phase 3c — pod_report routes through alerts.dispatcher.

Pins:

  - send_message hands off to dispatcher.send instead of subprocess
  - severity tracks the report's overall status (red→ERROR,
    yellow→WARNING, green→INFO) so the dispatcher's audit log is
    queryable by severity for forensics
  - dedup_key namespaces by report label (Daily / Manual / Dry-run / …)
    so a Manual run shortly after a Daily run still pushes
  - return value reflects DispatchResult.SENT (the caller gates the
    pod-report log line on this)
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

import pod_report  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
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
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=next_result["value"], source=source, severity=severity,
            dedup_key=dedup_key, channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    def fail_run(*a, **kw):
        raise AssertionError(
            f"pod_report must not subprocess.run after Phase 3c: {a}"
        )
    monkeypatch.setattr(pod_report.subprocess, "run", fail_run)

    return {
        "captured": captured,
        "next_result": next_result,
        "shared_dir": tmp_path / "evolve",
        "network": {"alerts": {"channel": "telegram", "chatId": "12345"}},
        "Severity": Severity,
        "DispatchResult": DispatchResult,
    }


def test_send_message_routes_through_dispatcher(env):
    sent = pod_report.send_message(
        "📊 Pod Report — Daily\nGreen across the board.",
        shared_dir=env["shared_dir"], network=env["network"],
        label="Daily", overall="green",
    )
    assert sent is True
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "pod_report"
    assert call["dedup_key"] == "pod_report/Daily"
    assert call["catalog_event"] == "summaries.daily_pod_report"
    assert call["severity"] == env["Severity"].INFO
    # Phase F6: payload-driven; catalog body_template renders
    # "📊 Pod report — {label}\n{summary}".
    assert call["message"] is None
    assert call["payload"] == {
        "label": "Daily",
        "summary": "📊 Pod Report — Daily\nGreen across the board.",
    }


def test_severity_maps_from_overall_status(env):
    """red → ERROR, yellow → WARNING, green → INFO. The audit log can
    then be filtered by severity when reviewing pod history."""
    cases = [
        ("red", env["Severity"].ERROR),
        ("yellow", env["Severity"].WARNING),
        ("green", env["Severity"].INFO),
        ("nonsense-status", env["Severity"].INFO),  # unknown → safe default
    ]
    for overall, expected in cases:
        env["captured"].clear()
        pod_report.send_message(
            "x", shared_dir=env["shared_dir"], network=env["network"],
            label="Daily", overall=overall,
        )
        assert env["captured"][0]["severity"] == expected, (
            f"overall={overall!r} expected severity={expected}, "
            f"got {env['captured'][0]['severity']}"
        )


def test_dedup_key_distinguishes_label(env):
    """A Manual run shortly after a Daily run should not be cooldown-
    suppressed — different operator-relevant events, different keys."""
    pod_report.send_message(
        "daily", shared_dir=env["shared_dir"], network=env["network"],
        label="Daily", overall="green",
    )
    pod_report.send_message(
        "manual", shared_dir=env["shared_dir"], network=env["network"],
        label="Manual", overall="green",
    )
    keys = [c["dedup_key"] for c in env["captured"]]
    assert keys == ["pod_report/Daily", "pod_report/Manual"]


def test_returns_false_on_dispatcher_suppression(env):
    env["next_result"]["value"] = env["DispatchResult"].SUPPRESSED_DISABLED
    sent = pod_report.send_message(
        "x", shared_dir=env["shared_dir"], network=env["network"],
        label="Daily", overall="green",
    )
    assert sent is False, (
        "must report False so the pod-report log records sent=False; "
        "operator who re-enables sees a fresh push next eligible tick"
    )


def test_returns_false_on_dispatcher_failure(env):
    env["next_result"]["value"] = env["DispatchResult"].FAILED
    sent = pod_report.send_message(
        "x", shared_dir=env["shared_dir"], network=env["network"],
        label="Daily", overall="red",
    )
    assert sent is False
