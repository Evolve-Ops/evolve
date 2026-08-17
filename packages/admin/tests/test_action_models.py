"""tests/test_action_models.py — action.models.check_freshness tool."""

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
from evolve_admin.evo.tools import action_models  # noqa: E402


_FAKE_URL = "http://127.0.0.1:5050/api/models/check-freshness"


def _make_stub(
    calls: list[dict[str, Any]],
    response: "tuple[int, dict[str, Any] | None, str | None]" = (200, {"ok": True}, None),
):
    def stub(url: str, body: dict[str, Any], timeout: int):
        calls.append({"url": url, "body": body, "timeout": timeout})
        return response
    return stub


def test_action_models_check_freshness_is_registered():
    tool = _tools.lookup("action.models.check_freshness")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_models_check_freshness_in_manifest():
    manifest = _tools.build_tool_manifest()
    entry = next(
        (e for e in manifest if e["name"] == "action.models.check_freshness"),
        None,
    )
    assert entry is not None
    # Empty input — no required args
    assert entry["input_schema"].get("required", []) == []


def test_check_freshness_success_aggregates_counts():
    """Success → surfaces advisory_count, drift_count, diversity_count
    and the underlying lists."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (200, {
            "checked_at": "2026-06-02T12:00:00Z",
            "advisory_count": 3,
            "drift_count": 1,
            "advisories": [
                {"bot_id": "team-bot-a", "tier": "workhorse"},
                {"bot_id": "team-bot-a", "tier": "judge"},
                {"bot_id": "personal-bot", "tier": "power"},
            ],
            "diversity_advisories": [
                {"bot_id": "personal-bot", "providers": ["anthropic"]},
            ],
            "drift_findings": [
                {"bot_id": "team-bot-a", "kind": "tier_references_missing_catalog"},
            ],
        }, None),
    )
    result = action_models._check_freshness_handler(
        post_json=stub, post_url=_FAKE_URL,
    )
    assert result["ok"] is True
    assert result["advisory_count"] == 3
    assert result["drift_count"] == 1
    assert result["diversity_count"] == 1
    assert len(result["advisories"]) == 3
    assert len(result["drift_findings"]) == 1
    assert "checked_at" in result
    assert "stale" in result["message"]
    # Empty body POSTed
    assert calls[0]["body"] == {}
    # 30s timeout — iterates every bot
    assert calls[0]["timeout"] == 30


def test_check_freshness_zero_advisories():
    """All bots fresh → counts are zero, lists are empty."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (200, {
            "checked_at": "2026-06-02T12:00:00Z",
            "advisory_count": 0,
            "drift_count": 0,
            "advisories": [],
            "diversity_advisories": [],
            "drift_findings": [],
        }, None),
    )
    result = action_models._check_freshness_handler(
        post_json=stub, post_url=_FAKE_URL,
    )
    assert result["ok"] is True
    assert result["advisory_count"] == 0
    assert result["drift_count"] == 0
    assert result["diversity_count"] == 0


def test_check_freshness_http_error_surfaces_status():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (500, {"error": "model registry unavailable"}, None),
    )
    result = action_models._check_freshness_handler(
        post_json=stub, post_url=_FAKE_URL,
    )
    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "model registry unavailable" in result["error"]


def test_check_freshness_transport_unreachable():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls, (0, None, "admin server unreachable"))
    result = action_models._check_freshness_handler(
        post_json=stub, post_url=_FAKE_URL,
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_check_freshness_validate_always_ok():
    res = action_models._check_freshness_validate()
    assert res["ok"] is True


def test_check_freshness_handler_without_post_url_returns_base_url_error():
    result = action_models._check_freshness_handler(network_path=None)
    assert result["ok"] is False
    assert "admin base URL unavailable" in result["error"]
