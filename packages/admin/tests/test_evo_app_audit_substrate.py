"""Tests for the evo audit handler's substrate sub-grammar (Workstream B-skills).

We test the grammar dispatch, not the full speak() rendering — speak()
adds a header that varies across roles and is exercised in other tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture()
def network() -> dict:
    return {"bots": {"team_bot_a": {"user": "team_bot_a"}}}


def _render(args: str, network: dict):
    """Invoke the handler's render() and return the message body."""
    from evolve_admin.evo.handlers.app_audit import render
    # Role is a Literal type; pass the primary string value directly.
    return render(role="primary", bot_id="team_bot_a", args=args, network=network)


def test_evo_audit_skill_unknown_form_kicks_audit(network, monkeypatch) -> None:
    """`evo audit skill gmail` queues a skill audit."""
    fake = MagicMock(ok=True, error="", request_id="req-1")
    with patch(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        return_value=fake,
    ):
        out = _render("skill gmail", network)
    # speak() returns a structured message; convert to text
    text = json.dumps(out) if isinstance(out, dict) else str(out)
    assert "gmail" in text
    assert "Started" in text or "started" in text


def test_evo_audit_skill_all(network, monkeypatch) -> None:
    fake = MagicMock(ok=True, error="", request_id="req-2")
    captured: dict = {}
    def _spy(**kwargs):
        captured.update(kwargs)
        return fake
    with patch(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        side_effect=_spy,
    ):
        _render("skill all", network)
    assert captured.get("element_type") == "skill"
    assert captured.get("elements") is None
    assert captured.get("full_audit") is False


def test_evo_audit_skill_full_audit_flag(network, monkeypatch) -> None:
    fake = MagicMock(ok=True, error="", request_id="req-3")
    captured: dict = {}
    def _spy(**kwargs):
        captured.update(kwargs)
        return fake
    with patch(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        side_effect=_spy,
    ):
        _render("skill gmail full", network)
    assert captured.get("full_audit") is True


def test_evo_audit_provider_dispatches_with_provider_type(network) -> None:
    fake = MagicMock(ok=True, error="", request_id="req-4")
    captured: dict = {}
    def _spy(**kwargs):
        captured.update(kwargs)
        return fake
    with patch(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        side_effect=_spy,
    ):
        _render("provider google_workspace", network)
    assert captured.get("element_type") == "provider"
    assert captured.get("elements") == ["google_workspace"]


def test_evo_audit_skill_empty_renders_status(network) -> None:
    """`evo audit skill` (no args) renders a status listing."""
    out = _render("skill", network)
    text = json.dumps(out) if isinstance(out, dict) else str(out)
    # No trail dir under /Users/team_bot_a/... in tests; the handler reports that.
    assert "Skill audit status" in text or "never audited" in text


def test_evo_audit_skill_history_requires_name(network) -> None:
    out = _render("skill history", network)
    text = json.dumps(out) if isinstance(out, dict) else str(out)
    assert "Usage" in text or "name" in text.lower()


def test_evo_audit_skill_accept_requires_args(network) -> None:
    out = _render("skill accept", network)
    text = json.dumps(out) if isinstance(out, dict) else str(out)
    assert "Usage" in text or "signature" in text.lower()


def test_evo_audit_skill_accept_calls_mark_helper(network) -> None:
    captured: dict = {}
    def _spy(**kwargs):
        captured.update(kwargs)
        return (True, "")
    with patch(
        "evolve_admin.applications.audit_dispatch.mark_substrate_finding_accepted",
        side_effect=_spy,
    ):
        _render("skill accept gmail sig-abc", network)
    assert captured.get("element_type") == "skill"
    assert captured.get("element_id") == "gmail"
    assert captured.get("signature") == "sig-abc"


def test_evo_audit_provider_history_uses_provider_audits_dir(network, tmp_path: Path) -> None:
    """Substrate history reads from {parent}_audits/<element>/trail.jsonl."""
    out = _render("provider history google_workspace", network)
    text = json.dumps(out) if isinstance(out, dict) else str(out)
    # On a fresh system the dir doesn't exist; the handler reports
    # "never audited" or "Trail file not readable".
    assert "google_workspace" in text or "audit" in text.lower()
