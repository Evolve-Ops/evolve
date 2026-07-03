"""tests/test_action_alerts.py — action.subscriptions.{test,reset} +
action.alerts.{set_digest_hour,send_test_report} tools.

Four action tools that close the Reports / Alerts gaps from
``docs/audit-evo-tool-coverage-2026-06-02.md``. All route through
admin-ui HTTP (same shape PR #1982 established for
``action.subscriptions.set``) so the evo gateway honors the
``EVO_WRITE_SHARED_SUBDIRS = ("proposals", "signals")`` contract.

Coverage per tool:
  * registration + manifest shape
  * success path POSTs correct body and surfaces admin-ui response
  * HTTP error response surfaces with ``http_status`` + admin error
  * HTTP transport unreachable returns a clear error
  * input validation gate rejects obvious mistakes
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import action_alerts  # noqa: E402


_FAKE_BASE_URL = "http://127.0.0.1:5050"


def _make_stub_factory(
    calls: list[dict[str, Any]],
    response: "tuple[int, dict[str, Any] | None, str | None]" = (200, {"ok": True}, None),
):
    """Build a post_json stub. The stub records each call into
    ``calls`` for assertion and returns the canned response."""
    def stub(url: str, body: dict[str, Any], timeout: int):
        calls.append({"url": url, "body": body, "timeout": timeout})
        return response
    return stub


# ─── Registration ───────────────────────────────────────────────────────────


def test_action_subscriptions_test_is_registered():
    tool = _tools.lookup("action.subscriptions.test")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_subscriptions_reset_is_registered():
    tool = _tools.lookup("action.subscriptions.reset")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_alerts_set_digest_hour_is_registered():
    tool = _tools.lookup("action.alerts.set_digest_hour")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_alerts_send_test_report_is_registered():
    tool = _tools.lookup("action.alerts.send_test_report")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_alerts_tools_in_manifest():
    manifest = _tools.build_tool_manifest()
    names = {e["name"] for e in manifest}
    for n in (
        "action.subscriptions.test",
        "action.subscriptions.reset",
        "action.alerts.set_digest_hour",
        "action.alerts.send_test_report",
    ):
        assert n in names, f"{n} missing from manifest"


# ─── action.subscriptions.test ──────────────────────────────────────────────


def test_subscriptions_test_success_posts_event_key():
    """Test fire → POST to /api/alerts/subscriptions/test with
    {"event_key": key}; response surfaces channel + result."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (200, {
            "rendered": "[test] config drift on team-bot-a",
            "result": "sent",
            "channel": "telegram",
            "error": None,
        }, None),
    )
    result = action_alerts._test_handler(
        key="security.config_drift",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/subscriptions/test",
    )
    assert result["ok"] is True
    assert result["key"] == "security.config_drift"
    assert result["channel"] == "telegram"
    assert result["result"] == "sent"
    assert "team-bot-a" in result["rendered"]
    # Body shape
    assert len(calls) == 1
    assert calls[0]["body"] == {"event_key": "security.config_drift"}


def test_subscriptions_test_http_error_surfaces_status():
    """Admin server 404 → error includes http_status and the route's
    error envelope."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (404, {"error": "unknown event_key"}, None),
    )
    result = action_alerts._test_handler(
        key="security.fictional_event",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/subscriptions/test",
    )
    assert result["ok"] is False
    assert result["http_status"] == 404
    assert "unknown event_key" in result["error"]


def test_subscriptions_test_transport_unreachable_returns_clear_error():
    """Transport failure → ok=False with a string error, no
    http_status (since we never got one)."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (0, None, "admin server unreachable at http://127.0.0.1:5050"),
    )
    result = action_alerts._test_handler(
        key="security.config_drift",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/subscriptions/test",
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"]
    assert "http_status" not in result


def test_subscriptions_test_empty_key_rejected_locally():
    """Empty key never hits HTTP — local guard returns immediately."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(calls)
    result = action_alerts._test_handler(
        key="",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/subscriptions/test",
    )
    assert result["ok"] is False
    assert "key is required" in result["error"]
    assert calls == []


def test_subscriptions_test_validate_rejects_unknown_key():
    """Validate gate fires before any HTTP roundtrip — unknown
    catalog keys get rejected at the proxy."""
    res = action_alerts._test_validate(key="security.fictional_event")
    assert res["ok"] is False
    assert "unknown subscription key" in res["reason"]


def test_subscriptions_test_validate_accepts_real_key():
    res = action_alerts._test_validate(key="security.config_drift")
    assert res["ok"] is True


# ─── action.subscriptions.reset ─────────────────────────────────────────────


def test_subscriptions_reset_success_clears_overrides():
    """Reset → POST to /api/alerts/subscriptions/reset with empty body;
    response includes a verify_via pointer."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(calls, (200, {"rows": []}, None))
    result = action_alerts._reset_handler(
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/subscriptions/reset",
    )
    assert result["ok"] is True
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.subscriptions"
    assert len(calls) == 1
    assert calls[0]["body"] == {}


def test_subscriptions_reset_http_error_surfaces_status():
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (500, {"error": "subscriptions file missing"}, None),
    )
    result = action_alerts._reset_handler(
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/subscriptions/reset",
    )
    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "subscriptions file missing" in result["error"]


def test_subscriptions_reset_transport_unreachable():
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (0, None, "admin server unreachable"),
    )
    result = action_alerts._reset_handler(
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/subscriptions/reset",
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_subscriptions_reset_validate_always_ok():
    res = action_alerts._reset_validate()
    assert res["ok"] is True


# ─── action.alerts.set_digest_hour ───────────────────────────────────────────


def test_set_digest_hour_success_posts_hour():
    """Valid hour → POST to /api/alerts/digest-hour with {"hour": N}."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(calls, (200, {"applied": 8}, None))
    result = action_alerts._digest_hour_handler(
        hour=8,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/digest-hour",
    )
    assert result["ok"] is True
    assert result["hour"] == 8
    assert result["applied"] == 8
    assert "08:00" in result["message"]
    assert len(calls) == 1
    assert calls[0]["body"] == {"hour": 8}


def test_set_digest_hour_rejects_out_of_range():
    """Out-of-range hour → local guard rejects; no HTTP roundtrip."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(calls)
    result = action_alerts._digest_hour_handler(
        hour=25,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/digest-hour",
    )
    assert result["ok"] is False
    assert "0..23" in result["error"]
    assert calls == []


def test_set_digest_hour_rejects_non_int():
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(calls)
    result = action_alerts._digest_hour_handler(
        hour="not-a-number",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/digest-hour",
    )
    assert result["ok"] is False
    assert "must be an integer" in result["error"]
    assert calls == []


def test_set_digest_hour_http_error_surfaces():
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (400, {"error": "subscriptions disabled"}, None),
    )
    result = action_alerts._digest_hour_handler(
        hour=8,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/alerts/digest-hour",
    )
    assert result["ok"] is False
    assert result["http_status"] == 400
    assert "subscriptions disabled" in result["error"]


def test_set_digest_hour_validate_rejects_bad_hour():
    res = action_alerts._digest_hour_validate(hour=99)
    assert res["ok"] is False
    assert "0..23" in res["reason"]


def test_set_digest_hour_validate_accepts_good_hour():
    res = action_alerts._digest_hour_validate(hour=8)
    assert res["ok"] is True


# ─── action.alerts.send_test_report ──────────────────────────────────────────


def test_send_test_report_real_send_default():
    """real_send default → True → body includes real_send=True."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (200, {
            "sent": True,
            "preview": "Morning briefing for team-bot-a: ...",
            "overall": "ok",
            "error": None,
        }, None),
    )
    result = action_alerts._send_test_report_handler(
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/reports-alerts/send-test",
    )
    assert result["ok"] is True
    assert result["sent"] is True
    assert result["real_send"] is True
    assert "team-bot-a" in result["preview"]
    assert "dispatched" in result["message"]
    assert len(calls) == 1
    assert calls[0]["body"] == {"real_send": True}


def test_send_test_report_dry_run():
    """real_send=False → body includes real_send=False; message says
    preview, not dispatched."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (200, {
            "sent": False,
            "preview": "[dry-run] briefing preview",
            "overall": "ok",
            "error": None,
        }, None),
    )
    result = action_alerts._send_test_report_handler(
        real_send=False,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/reports-alerts/send-test",
    )
    assert result["ok"] is True
    assert result["sent"] is False
    assert result["real_send"] is False
    assert "previewed" in result["message"]
    assert calls[0]["body"] == {"real_send": False}


def test_send_test_report_http_error_surfaces():
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (404, {"sent": False, "error": "pod_report.py not found"}, None),
    )
    result = action_alerts._send_test_report_handler(
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/reports-alerts/send-test",
    )
    assert result["ok"] is False
    assert result["http_status"] == 404
    assert "pod_report.py not found" in result["error"]


def test_send_test_report_transport_unreachable():
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(
        calls,
        (0, None, "admin server unreachable"),
    )
    result = action_alerts._send_test_report_handler(
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/reports-alerts/send-test",
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_send_test_report_uses_60s_timeout():
    """pod_report.py runs a real LLM call — timeout must give it
    headroom (60s)."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub_factory(calls, (200, {"sent": True}, None))
    action_alerts._send_test_report_handler(
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/reports-alerts/send-test",
    )
    assert calls[0]["timeout"] == 60


# ─── Resolve-URL failure path (covers base-URL plumbing) ────────────────────


def test_test_handler_returns_base_url_error_when_unset():
    """No post_url, no network_path → handler returns the base-URL
    resolution error verbatim (clear pointer for diagnosis)."""
    result = action_alerts._test_handler(
        key="security.config_drift",
        network_path=None,
    )
    assert result["ok"] is False
    assert "admin base URL unavailable" in result["error"]
