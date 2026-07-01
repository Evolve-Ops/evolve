"""Tests for audit_dispatch substrate helpers (Workstream B-skills).

Exercises:
  - request_substrate_audit writes the right inbox shape for skill / provider
  - mark_substrate_finding_accepted appends to the sidecar
  - element_type validation rejects unknown types
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


def _patch_inbox_path(monkeypatch, tmp_path: Path) -> None:
    """Redirect _audit_inbox_dir() into a tmp dir so we don't write under /Users."""
    from evolve_admin.applications import audit_dispatch
    fake_dir = tmp_path / "inbox"
    fake_dir.mkdir()
    monkeypatch.setattr(
        audit_dispatch, "_audit_inbox_dir",
        lambda bot_user: fake_dir,
    )


def _disable_kick(monkeypatch) -> None:
    """No-op the kick subprocess so tests don't spawn anything."""
    from evolve_admin.applications import audit_dispatch
    monkeypatch.setattr(
        audit_dispatch, "_kick_runner",
        lambda bot_user, request_id: (True, ""),
    )


def test_request_substrate_audit_rejects_unknown_element_type(tmp_path, monkeypatch) -> None:
    _patch_inbox_path(monkeypatch, tmp_path)
    from evolve_admin.applications.audit_dispatch import request_substrate_audit
    r = request_substrate_audit(
        bot_id="team_bot_a", bot_user="team_bot_a",
        element_type="bogus", elements=["gmail"],
    )
    assert r.ok is False
    assert "unknown element_type" in r.error


def test_request_substrate_audit_writes_skill_inbox(tmp_path, monkeypatch) -> None:
    _patch_inbox_path(monkeypatch, tmp_path)
    _disable_kick(monkeypatch)
    from evolve_admin.applications.audit_dispatch import request_substrate_audit
    r = request_substrate_audit(
        bot_id="team_bot_a", bot_user="team_bot_a",
        element_type="skill", elements=["gmail"],
        kick=False,
    )
    assert r.ok is True
    inbox_path = Path(r.inbox_path)
    assert inbox_path.exists()
    data = json.loads(inbox_path.read_text())
    assert data["kind"] == "skill_audit"
    assert data["skills"] == ["gmail"]


def test_request_substrate_audit_writes_provider_inbox(tmp_path, monkeypatch) -> None:
    _patch_inbox_path(monkeypatch, tmp_path)
    _disable_kick(monkeypatch)
    from evolve_admin.applications.audit_dispatch import request_substrate_audit
    r = request_substrate_audit(
        bot_id="team_bot_a", bot_user="team_bot_a",
        element_type="provider", elements=None,    # all
        kick=False,
    )
    assert r.ok is True
    data = json.loads(Path(r.inbox_path).read_text())
    assert data["kind"] == "provider_audit"
    assert data["providers"] == "all"


def test_request_substrate_audit_full_audit_propagates(tmp_path, monkeypatch) -> None:
    _patch_inbox_path(monkeypatch, tmp_path)
    _disable_kick(monkeypatch)
    from evolve_admin.applications.audit_dispatch import request_substrate_audit
    r = request_substrate_audit(
        bot_id="team_bot_a", bot_user="team_bot_a",
        element_type="skill", elements=["gmail"], full_audit=True, kick=False,
    )
    data = json.loads(Path(r.inbox_path).read_text())
    assert data["full_audit"] is True


def test_mark_substrate_finding_accepted_rejects_unknown_element_type() -> None:
    from evolve_admin.applications.audit_dispatch import mark_substrate_finding_accepted
    ok, err = mark_substrate_finding_accepted(
        bot_id="team_bot_a", bot_user="team_bot_a",
        element_type="bogus", element_id="x", signature="sig", accepted_by="t",
    )
    assert ok is False
    assert "unknown element_type" in err


def test_mark_substrate_finding_accepted_writes_sidecar(tmp_path, monkeypatch) -> None:
    """Append-and-write to skill_audits/<skill>/accepted.json under the bot home.

    The dispatch builders resolve the bot home via the pwd-first ``_acct_home``
    seam (W10-F #12 — platform-keyed, /home/<bot> on Linux). Inject a writable
    tmp home so the write lands directly (no sudo) and we can read it back.
    """
    from evolve_admin.applications import audit_dispatch

    home = tmp_path / "home" / "team_bot_a"
    home.mkdir(parents=True)
    monkeypatch.setattr(audit_dispatch, "_acct_home", lambda _u: home)

    ok, err = audit_dispatch.mark_substrate_finding_accepted(
        bot_id="team_bot_a", bot_user="team_bot_a",
        element_type="skill", element_id="gmail",
        signature="sig-abc", accepted_by="test",
    )
    assert ok is True, err
    sidecar = home / ".openclaw/workspace/evolve/skill_audits/gmail/accepted.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert any(a["signature"] == "sig-abc" for a in data["accepted"])


@pytest.mark.skip(reason="Requires file-system isolation; covered in integration tests")
def test_mark_substrate_finding_accepted_idempotent() -> None:
    pass
