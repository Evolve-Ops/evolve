"""tests/test_action_errors.py — action.errors.dismiss tool."""

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
from evolve_admin.evo.tools import action_errors  # noqa: E402


_FAKE_BASE_URL = "http://127.0.0.1:5050"
_DISMISS_URL = f"{_FAKE_BASE_URL}/api/report/dismiss"


def _make_stub(
    calls: list[dict[str, Any]],
    response: "tuple[int, dict[str, Any] | None, str | None]" = (200, {"ok": True}, None),
):
    def stub(url: str, body: dict[str, Any], timeout: int):
        calls.append({"url": url, "body": body, "timeout": timeout})
        return response
    return stub


def test_action_errors_dismiss_is_registered():
    tool = _tools.lookup("action.errors.dismiss")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_errors_dismiss_in_manifest():
    manifest = _tools.build_tool_manifest()
    entry = next(
        (e for e in manifest if e["name"] == "action.errors.dismiss"),
        None,
    )
    assert entry is not None
    assert "snooze_minutes" in entry["input_schema"]["properties"]


def test_dismiss_default_snooze_is_5_minutes():
    """No arg → snooze_minutes=5 (matches the UI default)."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (200, {
            "ok": True,
            "dismissed_at": "2026-06-02T12:00:00Z",
            "snooze_minutes": 5,
        }, None),
    )
    result = action_errors._dismiss_handler(
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert result["ok"] is True
    assert result["snooze_minutes"] == 5
    assert "5-minute snooze" in result["message"]
    assert calls[0]["body"] == {"snooze_minutes": 5}


def test_dismiss_custom_snooze():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (200, {
            "ok": True,
            "dismissed_at": "2026-06-02T12:00:00Z",
            "snooze_minutes": 30,
        }, None),
    )
    result = action_errors._dismiss_handler(
        snooze_minutes=30,
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert result["ok"] is True
    assert result["snooze_minutes"] == 30
    assert "30-minute" in result["message"]
    assert calls[0]["body"] == {"snooze_minutes": 30}


def test_dismiss_zero_snooze_uses_next_poll_message():
    """snooze=0 → the message reads "next poll" not "N-minute snooze"."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls, (200, {"ok": True, "snooze_minutes": 0}, None))
    result = action_errors._dismiss_handler(
        snooze_minutes=0,
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert result["ok"] is True
    assert result["snooze_minutes"] == 0
    assert "next poll" in result["message"]


def test_dismiss_returns_verify_via_pointer():
    """Result includes a verify_via pointer at pod_state.errors so the
    model confirms the banner state changed."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls, (200, {"ok": True, "snooze_minutes": 5}, None))
    result = action_errors._dismiss_handler(
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.errors"


def test_dismiss_rejects_negative_snooze():
    """Negative snooze rejected locally; no HTTP."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)
    result = action_errors._dismiss_handler(
        snooze_minutes=-5,
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert result["ok"] is False
    assert ">= 0" in result["error"]
    assert calls == []


def test_dismiss_rejects_non_int_snooze():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)
    result = action_errors._dismiss_handler(
        snooze_minutes="ten",
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert result["ok"] is False
    assert "integer" in result["error"]
    assert calls == []


def test_dismiss_http_error_surfaces_status():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls, (500, {"error": "reporter unavailable"}, None),
    )
    result = action_errors._dismiss_handler(
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "reporter unavailable" in result["error"]


def test_dismiss_transport_unreachable():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls, (0, None, "admin server unreachable"))
    result = action_errors._dismiss_handler(
        post_json=stub, post_url=_DISMISS_URL,
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_dismiss_validate_rejects_negative():
    res = action_errors._dismiss_validate(snooze_minutes=-1)
    assert res["ok"] is False


def test_dismiss_validate_accepts_zero():
    res = action_errors._dismiss_validate(snooze_minutes=0)
    assert res["ok"] is True


def test_dismiss_validate_accepts_default():
    res = action_errors._dismiss_validate()
    assert res["ok"] is True


def test_dismiss_handler_without_post_url_returns_base_url_error():
    """No post_url + no network_path → handler surfaces the base-URL
    resolution error."""
    result = action_errors._dismiss_handler(network_path=None)
    assert result["ok"] is False
    assert "admin base URL unavailable" in result["error"]
