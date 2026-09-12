"""tests/test_action_feedback.py — action.feedback.file_issue tool.

Companion to test_action_errors.py — same injected-transport pattern.
The handler POSTs to admin-ui's ``/api/feedback/issues`` endpoint;
tests stub the transport so the suite doesn't bind a real network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import action_feedback  # noqa: E402


_FAKE_BASE_URL = "http://127.0.0.1:5050"
_FILE_URL = f"{_FAKE_BASE_URL}/api/feedback/issues"


def _make_stub(
    calls: list[dict[str, Any]],
    response: "tuple[int, dict[str, Any] | None, str | None]" = (
        200,
        {
            "ok": True,
            "url": "https://github.com/evolve-ops/evolve/issues/42",
            "number": 42,
            "github_repo": "evolve-ops/evolve",
        },
        None,
    ),
):
    def stub(url: str, body: dict[str, Any], timeout: int):
        calls.append({"url": url, "body": body, "timeout": timeout})
        return response
    return stub


# ── Registration ────────────────────────────────────────────────────────────


def test_action_feedback_is_registered():
    tool = _tools.lookup("action.feedback.file_issue")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_RISKY
    assert tool.validate is not None
    assert tool.authorization_scope == "admin"


def test_action_feedback_in_manifest():
    manifest = _tools.build_tool_manifest()
    entry = next(
        (e for e in manifest if e["name"] == "action.feedback.file_issue"),
        None,
    )
    assert entry is not None
    props = entry["input_schema"]["properties"]
    assert set(props.keys()) == {"kind", "title", "note", "attach_snapshot"}
    assert entry["input_schema"]["required"] == ["kind", "title", "note"]
    assert props["kind"]["enum"] == ["bug", "feature"]


# ── Validate ────────────────────────────────────────────────────────────────


def test_validate_rejects_unknown_kind():
    result = action_feedback._file_issue_validate(
        kind="question", title="t", note="n",
    )
    assert result["ok"] is False
    assert "kind must be" in result["reason"]


def test_validate_rejects_empty_content():
    """title AND note both empty — nothing to file."""
    result = action_feedback._file_issue_validate(
        kind="bug", title="", note="   ",
    )
    assert result["ok"] is False
    assert "non-empty" in result["reason"]


def test_validate_accepts_title_only():
    result = action_feedback._file_issue_validate(
        kind="feature", title="Add cowbell mode", note="",
    )
    assert result["ok"] is True
    assert result["context"]["kind"] == "feature"


def test_validate_accepts_note_only():
    result = action_feedback._file_issue_validate(
        kind="bug", title="", note="It crashes on startup with EACCES.",
    )
    assert result["ok"] is True


def test_validate_normalizes_kind_case():
    result = action_feedback._file_issue_validate(
        kind="BUG", title="t", note="n",
    )
    assert result["ok"] is True
    assert result["context"]["kind"] == "bug"


# ── Handler: happy path ─────────────────────────────────────────────────────


def test_handler_happy_path_returns_url_and_number():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)

    result = action_feedback._file_issue_handler(
        kind="bug",
        title="Toaster does not toast",
        note="It smells like burnt regret.",
        post_json=stub, post_url=_FILE_URL,
    )

    assert result["ok"] is True
    assert result["url"] == "https://github.com/evolve-ops/evolve/issues/42"
    assert result["number"] == 42
    assert result["github_repo"] == "evolve-ops/evolve"
    assert "Filed evolve-ops/evolve#42" in result["message"]


def test_handler_forwards_body_to_admin_ui():
    """The POST body shape is what admin-ui's /api/feedback/issues expects."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)

    action_feedback._file_issue_handler(
        kind="feature",
        title="Add cowbell mode",
        note="Needs more cowbell.",
        attach_snapshot=False,
        post_json=stub, post_url=_FILE_URL,
    )

    assert len(calls) == 1
    sent = calls[0]
    assert sent["url"] == _FILE_URL
    assert sent["body"] == {
        "kind": "feature",
        "title": "Add cowbell mode",
        "note": "Needs more cowbell.",
        "attach_snapshot": False,
    }


def test_handler_omits_attach_snapshot_when_not_specified():
    """No attach_snapshot kw → admin-ui applies its default (true for bug)."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)

    action_feedback._file_issue_handler(
        kind="bug", title="t", note="n",
        post_json=stub, post_url=_FILE_URL,
    )

    assert "attach_snapshot" not in calls[0]["body"]


def test_handler_strips_whitespace_from_title_and_note():
    calls: list[dict[str, Any]] = []
    stub = _make_stub(calls)

    action_feedback._file_issue_handler(
        kind="bug", title="  trimmed title  ", note="\nnote\n",
        post_json=stub, post_url=_FILE_URL,
    )

    assert calls[0]["body"]["title"] == "trimmed title"
    assert calls[0]["body"]["note"] == "note"


def test_handler_includes_snapshot_path_when_admin_attached_one():
    """admin-ui returns snapshot_path on bug+attach; tool surfaces it."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (200, {
            "ok": True,
            "url": "https://github.com/o/r/issues/7",
            "number": 7,
            "github_repo": "o/r",
            "snapshot_path": "/Users/evolve/.evolve/reports/report-20260607T120000.json",
        }, None),
    )
    result = action_feedback._file_issue_handler(
        kind="bug", title="t", note="n",
        post_json=stub, post_url=_FILE_URL,
    )
    assert result["snapshot_path"].endswith("report-20260607T120000.json")


# ── Handler: input validation (duplicate of validate, defensive) ────────────


def test_handler_rejects_unknown_kind():
    result = action_feedback._file_issue_handler(
        kind="rant", title="t", note="n",
        post_json=lambda *a, **kw: pytest.fail("should not call admin-ui"),
        post_url=_FILE_URL,
    )
    assert result["ok"] is False
    assert "kind must be" in result["error"]


def test_handler_rejects_empty_content():
    result = action_feedback._file_issue_handler(
        kind="bug", title="", note="",
        post_json=lambda *a, **kw: pytest.fail("should not call admin-ui"),
        post_url=_FILE_URL,
    )
    assert result["ok"] is False
    assert "non-empty" in result["error"]


# ── Handler: admin-ui error envelopes ───────────────────────────────────────


def test_handler_412_returns_fallback_url():
    """When admin-ui has no PAT, surface the fallback_url to evo."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (412, {
            "ok": False,
            "error": "No GitHub PAT in keystore slot 'github_intake'.",
            "fallback_url": "https://github.com/o/r/issues/new?template=bug_report.yml&title=t",
        }, None),
    )
    result = action_feedback._file_issue_handler(
        kind="bug", title="t", note="n",
        post_json=stub, post_url=_FILE_URL,
    )
    assert result["ok"] is False
    assert result["fallback_url"].startswith("https://github.com/")
    assert "GitHub PAT" in result["error"]
    assert result["http_status"] == 412
    # Hint guides the model toward the right behavior — don't pretend the
    # call succeeded; hand the operator a URL instead.
    assert "browser" in result["hint"]


def test_handler_502_propagates_github_error():
    """admin-ui returns 502 when GitHub itself rejects the call."""
    calls: list[dict[str, Any]] = []
    stub = _make_stub(
        calls,
        (502, {
            "ok": False,
            "error": "GitHub issue creation failed (422): Validation Failed",
        }, None),
    )
    result = action_feedback._file_issue_handler(
        kind="bug", title="t", note="n",
        post_json=stub, post_url=_FILE_URL,
    )
    assert result["ok"] is False
    assert "Validation Failed" in result["error"]
    assert result["http_status"] == 502
    # 502 isn't 412 — no fallback_url should be invented.
    assert "fallback_url" not in result


def test_handler_transport_error_propagates():
    """Connection failure: surface the error string cleanly."""
    def stub(url: str, body: dict[str, Any], timeout: int):
        return 0, None, f"admin server unreachable at {url}: Connection refused"

    result = action_feedback._file_issue_handler(
        kind="bug", title="t", note="n",
        post_json=stub, post_url=_FILE_URL,
    )
    assert result["ok"] is False
    assert "Connection refused" in result["error"]


def test_handler_200_with_ok_false_treated_as_failure():
    """Defensive: if admin-ui returns 200 but ok:false, we don't claim success."""
    stub = _make_stub(
        [],
        (200, {"ok": False, "error": "something weird"}, None),
    )
    result = action_feedback._file_issue_handler(
        kind="bug", title="t", note="n",
        post_json=stub, post_url=_FILE_URL,
    )
    assert result["ok"] is False
    assert "something weird" in result["error"]
