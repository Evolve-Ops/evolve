"""tests/test_feedback_file_issue.py — direct GitHub filing for the
Feedback page.

Companion to test_intake_promote.py — same injectable-transport pattern.
Exercises the new ``file_issue`` function (REST API path) introduced to
fix the Issue-Form prefill bug where the URL-based path silently
dropped the issue body.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for p in (str(_ADMIN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── file_issue: argument validation ─────────────────────────────────────────


def test_file_issue_invalid_repo_raises():
    from evolve_admin import feedback

    with pytest.raises(feedback.FeedbackFilingError, match="invalid github_repo"):
        feedback.file_issue(
            repo="not-a-real-shape",
            kind="bug",
            title="t",
            body="b",
            token="x",
        )


def test_file_issue_empty_token_raises():
    from evolve_admin import feedback

    with pytest.raises(feedback.FeedbackFilingError, match="no GitHub token"):
        feedback.file_issue(
            repo="evolve-ops/evolve",
            kind="bug",
            title="t",
            body="b",
            token="",
        )


def test_file_issue_whitespace_only_token_raises():
    from evolve_admin import feedback

    with pytest.raises(feedback.FeedbackFilingError, match="no GitHub token"):
        feedback.file_issue(
            repo="evolve-ops/evolve",
            kind="bug",
            title="t",
            body="b",
            token="   ",
        )


def test_file_issue_unknown_kind_raises():
    from evolve_admin import feedback

    with pytest.raises(feedback.FeedbackFilingError, match="unknown kind"):
        feedback.file_issue(
            repo="evolve-ops/evolve",
            kind="question",
            title="t",
            body="b",
            token="ghp_x",
        )


# ── file_issue: request shape ───────────────────────────────────────────────


def test_file_issue_bug_happy_path_sends_expected_shape():
    """Verifies request shape: URL, headers, payload, default labels."""
    from evolve_admin import feedback

    captured = {}

    def fake_transport(method, url, headers, body):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json.loads(body.decode())
        return 201, {
            "number": 7,
            "html_url": "https://github.com/evolve-ops/evolve/issues/7",
        }

    result = feedback.file_issue(
        repo="evolve-ops/evolve",
        kind="bug",
        title="Toaster does not toast",
        body="### What happened?\n\nIt smells like burnt regret.",
        token="ghp_t0k3n",
        transport=fake_transport,
    )

    assert result == {
        "url": "https://github.com/evolve-ops/evolve/issues/7",
        "number": 7,
    }
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.github.com/repos/evolve-ops/evolve/issues"
    assert captured["headers"]["Authorization"] == "Bearer ghp_t0k3n"
    assert captured["headers"]["Accept"] == "application/vnd.github+json"
    assert captured["headers"]["User-Agent"] == "evolve-admin-feedback"
    assert captured["payload"]["title"] == "Toaster does not toast"
    # Body is the raw markdown — NOT URL-encoded, NOT wrapped in form fields.
    # This is the whole point of the new code path; the URL-prefill flow
    # dropped this on the floor.
    assert "It smells like burnt regret." in captured["payload"]["body"]
    # Default labels: bug → ["bug"]; matches bug_report.yml.
    assert captured["payload"]["labels"] == ["bug"]


def test_file_issue_feature_uses_enhancement_label():
    from evolve_admin import feedback

    captured = {}

    def fake_transport(method, url, headers, body):
        captured["payload"] = json.loads(body.decode())
        return 201, {"number": 1, "html_url": "https://github.com/o/r/issues/1"}

    feedback.file_issue(
        repo="o/r",
        kind="feature",
        title="Add cowbell mode",
        body="### Problem\nNeeds more cowbell.",
        token="t",
        transport=fake_transport,
    )

    # Matches feature_request.yml's declared label.
    assert captured["payload"]["labels"] == ["enhancement"]


def test_file_issue_caller_can_override_labels():
    """Explicit labels= overrides the default per-kind list."""
    from evolve_admin import feedback

    captured = {}

    def fake_transport(method, url, headers, body):
        captured["payload"] = json.loads(body.decode())
        return 201, {"number": 1, "html_url": "https://github.com/o/r/issues/1"}

    feedback.file_issue(
        repo="o/r",
        kind="bug",
        title="t",
        body="b",
        token="t",
        labels=["bug", "from:admin-ui", "priority:high"],
        transport=fake_transport,
    )

    assert captured["payload"]["labels"] == ["bug", "from:admin-ui", "priority:high"]


def test_file_issue_empty_labels_list_suppresses_field():
    """Passing labels=[] explicitly drops the field from the payload."""
    from evolve_admin import feedback

    captured = {}

    def fake_transport(method, url, headers, body):
        captured["payload"] = json.loads(body.decode())
        return 201, {"number": 1, "html_url": "https://github.com/o/r/issues/1"}

    feedback.file_issue(
        repo="o/r",
        kind="bug",
        title="t",
        body="b",
        token="t",
        labels=[],
        transport=fake_transport,
    )

    assert "labels" not in captured["payload"]


def test_file_issue_blank_title_falls_back_to_default():
    """Empty/whitespace titles get a sensible default so GitHub doesn't 422."""
    from evolve_admin import feedback

    captured = {}

    def fake_transport(method, url, headers, body):
        captured["payload"] = json.loads(body.decode())
        return 201, {"number": 1, "html_url": "https://github.com/o/r/issues/1"}

    feedback.file_issue(
        repo="o/r",
        kind="feature",
        title="   ",
        body="b",
        token="t",
        transport=fake_transport,
    )

    assert captured["payload"]["title"] == "Feature request from Evolve admin"


# ── file_issue: error mapping ───────────────────────────────────────────────


def test_file_issue_github_422_raises_with_message():
    from evolve_admin import feedback

    def fake_transport(method, url, headers, body):
        return 422, {"message": "Validation Failed", "errors": [{"code": "missing"}]}

    with pytest.raises(feedback.FeedbackFilingError) as exc:
        feedback.file_issue(
            repo="o/r", kind="bug", title="t", body="b", token="t",
            transport=fake_transport,
        )

    msg = str(exc.value)
    assert "422" in msg
    assert "Validation Failed" in msg


def test_file_issue_github_401_raises_with_message():
    from evolve_admin import feedback

    def fake_transport(method, url, headers, body):
        return 401, {"message": "Bad credentials"}

    with pytest.raises(feedback.FeedbackFilingError, match="401"):
        feedback.file_issue(
            repo="o/r", kind="bug", title="t", body="b", token="t",
            transport=fake_transport,
        )


def test_file_issue_transport_network_error_raises():
    """Transport returning status=0 (network failure) surfaces cleanly."""
    from evolve_admin import feedback

    def fake_transport(method, url, headers, body):
        return 0, {"error": "network: Connection refused"}

    with pytest.raises(feedback.FeedbackFilingError, match="Connection refused"):
        feedback.file_issue(
            repo="o/r", kind="bug", title="t", body="b", token="t",
            transport=fake_transport,
        )


def test_file_issue_non_201_2xx_treated_as_failure():
    """GitHub uses 201 specifically for issue creation; treat 200 as wrong."""
    from evolve_admin import feedback

    def fake_transport(method, url, headers, body):
        return 200, {"number": 42, "html_url": "https://github.com/o/r/issues/42"}

    with pytest.raises(feedback.FeedbackFilingError, match="200"):
        feedback.file_issue(
            repo="o/r", kind="bug", title="t", body="b", token="t",
            transport=fake_transport,
        )


def test_file_issue_missing_number_in_response_returns_none():
    """A successful 201 without a number field shouldn't crash — return None."""
    from evolve_admin import feedback

    def fake_transport(method, url, headers, body):
        return 201, {"html_url": "https://github.com/o/r/issues/?"}

    result = feedback.file_issue(
        repo="o/r", kind="bug", title="t", body="b", token="t",
        transport=fake_transport,
    )
    assert result["number"] is None
    assert result["url"] == "https://github.com/o/r/issues/?"
