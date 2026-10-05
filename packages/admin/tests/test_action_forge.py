"""tests/test_action_forge.py — action.forge.{approve,reject} tools."""

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
from evolve_admin.evo.tools import action_forge  # noqa: E402


_FAKE_BASE_URL = "http://127.0.0.1:5050"
_VALID_JOB_ID = "forge_2026-06-02_team-bot-a_app-foo"


def _make_stub(
    calls: list[dict[str, Any]],
    response: "tuple[int, dict[str, Any] | None, str | None]" = (200, {"ok": True, "job_id": _VALID_JOB_ID}, None),
):
    def stub(url: str, body: dict[str, Any], timeout: int):
        calls.append({"url": url, "body": body, "timeout": timeout})
        return response
    return stub


# ─── Registration ───────────────────────────────────────────────────────────


def test_action_forge_approve_is_registered():
    tool = _tools.lookup("action.forge.approve")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_RISKY
    assert tool.validate is not None


def test_action_forge_reject_is_registered():
    tool = _tools.lookup("action.forge.reject")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_forge_tools_in_manifest():
    manifest = _tools.build_tool_manifest()
    names = {e["name"] for e in manifest}
    assert "action.forge.approve" in names
    assert "action.forge.reject" in names


# ─── action.forge.approve ───────────────────────────────────────────────────


def test_approve_success_posts_approved_by_and_notes():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (200, {"ok": True, "job_id": _VALID_JOB_ID}, None),
    )
    result = action_forge._approve_handler(
        job_id=_VALID_JOB_ID,
        approved_by="cjalden",
        notes="reviewed manifest, looks right",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/approve",
    )
    assert result["ok"] is True
    assert result["job_id"] == _VALID_JOB_ID
    assert result["approved_by"] == "cjalden"
    assert result["notes"] == "reviewed manifest, looks right"
    assert "approved" in result["message"]
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.forge_job"
    # Body shape
    assert calls[0]["body"] == {
        "approved_by": "cjalden",
        "notes": "reviewed manifest, looks right",
    }


def test_approve_default_approved_by_is_evo():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)
    action_forge._approve_handler(
        job_id=_VALID_JOB_ID,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/approve",
    )
    assert calls[0]["body"]["approved_by"] == "evo"
    assert calls[0]["body"]["notes"] == ""


def test_approve_rejects_invalid_job_id_shell_chars():
    """Job id with shell metacharacters → local guard rejects; no HTTP."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)
    result = action_forge._approve_handler(
        job_id="job; rm -rf /",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/x/approve",
    )
    assert result["ok"] is False
    assert "job_id must match" in result["error"]
    assert calls == []


def test_approve_rejects_empty_job_id():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)
    result = action_forge._approve_handler(
        job_id="",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/x/approve",
    )
    assert result["ok"] is False
    assert calls == []


def test_approve_http_error_surfaces_status():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (400, {"error": "Job not in awaiting_approval state"}, None),
    )
    result = action_forge._approve_handler(
        job_id=_VALID_JOB_ID,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/approve",
    )
    assert result["ok"] is False
    assert result["http_status"] == 400
    assert "awaiting_approval" in result["error"]
    assert result["job_id"] == _VALID_JOB_ID


def test_approve_transport_unreachable():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls, (0, None, "admin server unreachable"))
    result = action_forge._approve_handler(
        job_id=_VALID_JOB_ID,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/approve",
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_approve_validate_rejects_invalid_shape():
    res = action_forge._approve_validate(job_id="job; rm -rf /")
    assert res["ok"] is False


def test_approve_validate_rejects_missing_job_id():
    res = action_forge._approve_validate(job_id="")
    assert res["ok"] is False


def test_approve_validate_accepts_valid_id():
    res = action_forge._approve_validate(job_id=_VALID_JOB_ID)
    assert res["ok"] is True


# ─── action.forge.reject ────────────────────────────────────────────────────


def test_reject_success_posts_rejected_by_and_reason():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (200, {"ok": True, "job_id": _VALID_JOB_ID}, None),
    )
    result = action_forge._reject_handler(
        job_id=_VALID_JOB_ID,
        rejected_by="cjalden",
        reason="manifest references undefined skill",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/reject",
    )
    assert result["ok"] is True
    assert result["job_id"] == _VALID_JOB_ID
    assert result["rejected_by"] == "cjalden"
    assert result["reason"] == "manifest references undefined skill"
    assert "rejected" in result["message"]
    assert calls[0]["body"] == {
        "rejected_by": "cjalden",
        "reason": "manifest references undefined skill",
    }


def test_reject_default_rejected_by_is_evo():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)
    action_forge._reject_handler(
        job_id=_VALID_JOB_ID,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/reject",
    )
    assert calls[0]["body"]["rejected_by"] == "evo"
    assert calls[0]["body"]["reason"] == ""


def test_reject_rejects_invalid_job_id():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)
    result = action_forge._reject_handler(
        job_id="job/with/slashes",
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/x/reject",
    )
    assert result["ok"] is False
    assert calls == []


def test_reject_http_error_surfaces_status():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (404, {"error": "Job not found: x"}, None),
    )
    result = action_forge._reject_handler(
        job_id=_VALID_JOB_ID,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/reject",
    )
    assert result["ok"] is False
    assert result["http_status"] == 404
    assert "Job not found" in result["error"]


def test_reject_transport_unreachable():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls, (0, None, "admin server unreachable"))
    result = action_forge._reject_handler(
        job_id=_VALID_JOB_ID,
        post_json=stub,
        post_url=f"{_FAKE_BASE_URL}/api/forge/jobs/{_VALID_JOB_ID}/reject",
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_reject_validate_accepts_valid_id():
    res = action_forge._reject_validate(job_id=_VALID_JOB_ID)
    assert res["ok"] is True


def test_reject_validate_rejects_bad_id():
    res = action_forge._reject_validate(job_id="job with space")
    assert res["ok"] is False


def test_approve_handler_without_post_url_returns_base_url_error():
    """No post_url + no network_path → handler surfaces the base-URL
    resolution error."""
    result = action_forge._approve_handler(
        job_id=_VALID_JOB_ID,
        network_path=None,
    )
    assert result["ok"] is False
    assert "admin base URL unavailable" in result["error"]
