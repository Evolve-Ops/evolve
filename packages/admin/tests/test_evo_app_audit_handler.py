"""Smoke tests for the evo app-audit handler.

We don't run the full chat dispatcher — that's covered by other evo tests.
We test the handler's grammar parsing + the message bodies it produces.

IMPORTANT: imports of ``evolve_admin.evo.*`` are deferred to function scope
because pytest's collection phase runs module-level imports BEFORE the
conftest's ``_restore_module_state`` autouse fixture establishes its
snapshot. Top-level imports here would leak ``evo.handlers._shared`` and
its transitive imports (dispatch, subcommands, identity) into the
session-global sys.modules with no per-test cleanup, breaking a swath
of downstream evo tests that depend on a fresh state. See conftest.py
``_restore_module_state`` for the mechanism.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


_NETWORK = {
    "bots": {"team_bot_a": {"user": "team_bot_a"}},
}


def _app_audit_module():
    """Lazy import — keeps evo modules out of the collection-phase sys.modules."""
    from evolve_admin.evo.handlers import app_audit
    return app_audit


def _call(args: str, network=_NETWORK):
    app_audit = _app_audit_module()
    return app_audit.render(role="primary", bot_id="team_bot_a", args=args, network=network)


def test_status_listing_when_no_manifests(tmp_path: Path, monkeypatch) -> None:
    """Bare `evo app-audit` with no manifests shows the empty state."""
    monkeypatch.setattr(_app_audit_module(), "_load_manifests", lambda b, n: [])
    result = _call("")
    assert "No apps installed yet" in result.direct_send_message


def test_status_listing_renders_per_app_badges(monkeypatch) -> None:
    manifests = [
        {"id": "clean", "display_name": "Clean",
         "last_audit": {"verified_at": "2026-05-15T00:00:00Z",
                        "findings_count": 0}},
        {"id": "noisy", "display_name": "Noisy",
         "last_audit": {"verified_at": "2026-05-15T00:00:00Z",
                        "findings_count": 3,
                        "outcomes": {"propose": 2, "conflict_notice": 0}}},
        {"id": "broken", "display_name": "Broken",
         "last_audit": {"verified_at": "2026-05-15T00:00:00Z",
                        "status": "failed", "error": "openclaw timed out"}},
        {"id": "fresh", "display_name": "Fresh"},
        {"id": "static", "display_name": "Static", "audit_eligible": False},
    ]
    monkeypatch.setattr(_app_audit_module(), "_load_manifests", lambda b, n: manifests)
    result = _call("")
    body = result.direct_send_message
    assert "✓ clean" in body.lower() or "✓ Clean" in body
    assert "2 raised" in body
    assert "audit failed" in body
    assert "never audited" in body
    assert "ineligible" in body


def test_single_app_kicks_audit(monkeypatch) -> None:
    """`evo app-audit <app>` dispatches via request_audit."""
    monkeypatch.setattr(_app_audit_module(), "_load_manifests",
                        lambda b, n: [{"id": "journal"}])

    called: dict = {}
    def _fake_request(*, bot_id, bot_user, apps, full_audit, requested_by, kick):
        called.update(bot_id=bot_id, bot_user=bot_user, apps=apps,
                      full_audit=full_audit, kick=kick)
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=True, request_id="audit-req-test",
            bot_id=bot_id, bot_user=bot_user,
            apps=apps, full_audit=full_audit, kicked=True,
        )
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(_ad, "request_audit", _fake_request)
    result = _call("journal")
    assert "Started auditing" in result.direct_send_message
    assert called["apps"] == ["journal"]
    assert called["full_audit"] is False


def test_single_app_full_flag_sets_full_audit(monkeypatch) -> None:
    monkeypatch.setattr(_app_audit_module(), "_load_manifests",
                        lambda b, n: [{"id": "journal"}])

    captured = {}
    def _fake_request(**kwargs):
        captured.update(kwargs)
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=True, request_id="r", bot_id="team_bot_a", bot_user="team_bot_a",
            apps=kwargs["apps"], full_audit=kwargs["full_audit"], kicked=True,
        )
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(_ad, "request_audit", _fake_request)
    _call("journal full")
    assert captured["full_audit"] is True


def test_all_form_audits_all_apps(monkeypatch) -> None:
    captured = {}
    def _fake_request(**kwargs):
        captured.update(kwargs)
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=True, request_id="r", bot_id="team_bot_a", bot_user="team_bot_a",
            apps=None, full_audit=kwargs["full_audit"], kicked=True,
        )
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(_ad, "request_audit", _fake_request)
    _call("all")
    assert captured["apps"] is None   # None means "all eligible"
    assert captured["full_audit"] is False


def test_all_full_form_sets_full_audit(monkeypatch) -> None:
    captured = {}
    def _fake_request(**kwargs):
        captured.update(kwargs)
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=True, request_id="r", bot_id="team_bot_a", bot_user="team_bot_a",
            apps=None, full_audit=True, kicked=True,
        )
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(_ad, "request_audit", _fake_request)
    _call("all full")
    assert captured["full_audit"] is True


def test_unknown_app_returns_friendly_error(monkeypatch) -> None:
    monkeypatch.setattr(_app_audit_module(), "_load_manifests",
                        lambda b, n: [{"id": "journal"}])
    result = _call("not-an-app")
    assert "not found" in result.direct_send_message.lower()


def test_accept_with_too_few_args_shows_usage() -> None:
    result = _call("accept")
    assert "Usage" in result.direct_send_message


def test_accept_form_calls_mark_finding_accepted(monkeypatch) -> None:
    captured = {}
    def _fake_mark(**kwargs):
        captured.update(kwargs)
        return (True, "")
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(_ad, "mark_finding_accepted", _fake_mark)
    result = _call("accept journal sig-abc-123")
    assert "accepted" in result.direct_send_message.lower()
    assert captured["app_id"] == "journal"
    assert captured["signature"] == "sig-abc-123"


def test_history_form_with_missing_app_shows_usage() -> None:
    result = _call("history")
    assert "Usage" in result.direct_send_message


def test_history_no_trail_returns_empty_state(tmp_path: Path) -> None:
    """When the trail.jsonl doesn't exist, the handler reports no history."""
    network = {"bots": {"team_bot_a": {"user": f"_test_no_such_user_{tmp_path.name}"}}}
    result = _app_audit_module().render(
        role="primary", bot_id="team_bot_a", args="history journal",
        network=network,
    )
    assert "No audit trail" in result.direct_send_message or "not readable" in result.direct_send_message
