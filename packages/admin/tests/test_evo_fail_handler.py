"""Smoke tests for the evo fail handler.

Same testing approach as ``test_evo_app_audit_handler.py`` — lazy
imports to keep evo modules out of session-global sys.modules during
collection, and mocked dispatch helpers so we don't have to spin up a
real bot runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


_NETWORK = {
    "bots": {
        "team_bot_a": {
            "user": "team_bot_a",
            "primary_user": {
                "pod_user": "pod_admin_user",
                "external_ids": {"telegram": "12345"},
            },
        },
    },
}


def _fail_module():
    from evolve_admin.evo.handlers import fail
    return fail


def _call(args: str, role="primary", network=_NETWORK):
    fail = _fail_module()
    return fail.render(role=role, bot_id="team_bot_a", args=args, network=network)


# ── Bare form ──────────────────────────────────────────────────────────────


def test_bare_evo_fail_shows_usage() -> None:
    result = _call("")
    body = result.direct_send_message
    assert "evo fail <description>" in body
    assert "recent" in body
    assert "status" in body
    assert "flag" in body


# ── evo fail <description> ──────────────────────────────────────────────────


def test_describe_form_dispatches_via_request_investigation(monkeypatch) -> None:
    called: dict = {}

    def _fake_request(*, bot_id, bot_user, user_description,
                      requesting_user, kick):
        called.update(
            bot_id=bot_id, bot_user=bot_user,
            user_description=user_description,
            requesting_user=requesting_user, kick=kick,
        )
        from evolve_admin.applications.audit_dispatch import (
            InvestigationDispatchResult,
        )
        return InvestigationDispatchResult(
            ok=True, investigation_id="inv-abcd1234",
            bot_id=bot_id, bot_user=bot_user,
            user_description=user_description,
            requesting_user=requesting_user, kicked=True,
        )

    def _no_rate_limit(bot_user, requesting_user, *, window_seconds):
        return 0

    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(_ad, "request_investigation", _fake_request)
    monkeypatch.setattr(_ad, "count_investigations_in_window", _no_rate_limit)

    result = _call("morning briefing didn't arrive today")

    body = result.direct_send_message
    assert "Looking into it" in body
    assert "inv-abcd1234" in body
    assert called["user_description"] == "morning briefing didn't arrive today"
    assert called["bot_id"] == "team_bot_a"
    assert called["bot_user"] == "team_bot_a"
    # requesting_user should resolve via the primary block in _NETWORK
    assert called["requesting_user"] == "pod:pod_admin_user"


def test_describe_form_rate_limit_blocks_after_cap(monkeypatch) -> None:
    """4th investigation in an hour gets the 'still working' reply."""
    from evolve_admin.applications import audit_dispatch as _ad

    def _busy(bot_user, requesting_user, *, window_seconds):
        return 3

    monkeypatch.setattr(_ad, "count_investigations_in_window", _busy)

    # Should NOT call request_investigation
    def _fake_request(**kwargs):
        raise AssertionError("rate-limited call slipped through")

    monkeypatch.setattr(_ad, "request_investigation", _fake_request)

    result = _call("another report")
    assert "still working" in result.direct_send_message.lower()
    assert "3" in result.direct_send_message  # rate-limit count


def test_describe_form_empty_description_returns_usage_hint(
    monkeypatch,
) -> None:
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(
        _ad, "count_investigations_in_window",
        lambda bu, ru, **kw: 0,
    )
    # We pass "   " which trims to empty.
    fail = _fail_module()
    body = fail._handle_describe(
        bot_id="team_bot_a", bot_user="team_bot_a",
        description="   ", network=_NETWORK,
    )
    assert "Tell me what went wrong" in body


# ── evo fail recent ─────────────────────────────────────────────────────────


def test_recent_form_replays_last_description(monkeypatch) -> None:
    from evolve_admin.applications import audit_dispatch as _ad

    def _fake_latest(bot_user, requesting_user):
        return {
            "investigation_id": "inv-old",
            "user_description": "the old failure",
            "requesting_user": requesting_user,
        }

    captured = {}

    def _fake_request(**kwargs):
        captured.update(kwargs)
        from evolve_admin.applications.audit_dispatch import (
            InvestigationDispatchResult,
        )
        return InvestigationDispatchResult(
            ok=True, investigation_id="inv-new",
            bot_id="team_bot_a", bot_user="team_bot_a",
            user_description=kwargs["user_description"],
            requesting_user=kwargs["requesting_user"], kicked=True,
        )

    monkeypatch.setattr(_ad, "latest_investigation_for_user", _fake_latest)
    monkeypatch.setattr(_ad, "request_investigation", _fake_request)
    monkeypatch.setattr(
        _ad, "count_investigations_in_window",
        lambda bu, ru, **kw: 0,
    )

    result = _call("recent")
    assert captured["user_description"] == "the old failure"
    assert "inv-new" in result.direct_send_message


def test_recent_with_no_prior_returns_hint(monkeypatch) -> None:
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(
        _ad, "latest_investigation_for_user", lambda bu, ru: None,
    )
    result = _call("recent")
    assert "don't have a previous investigation" in result.direct_send_message


# ── evo fail status ────────────────────────────────────────────────────────


def test_status_form_renders_recent_entry(monkeypatch) -> None:
    from evolve_admin.applications import audit_dispatch as _ad

    def _fake_latest(bot_user, requesting_user):
        return {
            "investigation_id": "inv-test",
            "user_description": "something failed",
            "ts": "2026-05-17T10:00:00Z",
            "status": "ok",
            "diagnosis": "It was the gmail token.",
            "confidence": "high",
        }

    monkeypatch.setattr(_ad, "latest_investigation_for_user", _fake_latest)
    result = _call("status")
    body = result.direct_send_message
    assert "inv-test" in body
    assert "something failed" in body
    assert "It was the gmail token." in body


def test_status_form_no_prior_returns_hint(monkeypatch) -> None:
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(
        _ad, "latest_investigation_for_user", lambda bu, ru: None,
    )
    result = _call("status")
    assert "No recent investigations" in result.direct_send_message


# ── evo fail flag ──────────────────────────────────────────────────────────


def test_flag_form_dispatches_unresolved(monkeypatch) -> None:
    from evolve_admin.applications import audit_dispatch as _ad
    captured = {}

    def _fake_flag(*, bot_id, bot_user, requesting_user):
        captured.update(
            bot_id=bot_id, bot_user=bot_user,
            requesting_user=requesting_user,
        )
        return True, "", "inv-flagged"

    monkeypatch.setattr(_ad, "flag_investigation_unresolved", _fake_flag)
    result = _call("flag")
    assert "Flagged for the operator" in result.direct_send_message
    assert "inv-flagged" in result.direct_send_message
    assert captured["bot_id"] == "team_bot_a"
    assert captured["bot_user"] == "team_bot_a"
    assert captured["requesting_user"] == "pod:pod_admin_user"


def test_flag_form_no_recent_returns_hint(monkeypatch) -> None:
    from evolve_admin.applications import audit_dispatch as _ad
    monkeypatch.setattr(
        _ad, "flag_investigation_unresolved",
        lambda **kw: (False, "no recent investigation found", ""),
    )
    result = _call("flag")
    assert "don't have a recent investigation" in result.direct_send_message


# ── Registry registration ──────────────────────────────────────────────────


def test_fail_subcommand_registered() -> None:
    from evolve_admin.evo import subcommands as sc
    cmd = sc.lookup("fail")
    assert cmd is not None
    assert cmd.handler == "evolve_admin.evo.handlers.fail:render"
    # Available to primary; admin gets it via the role_can_run superset rule.
    assert sc.role_can_run("primary", cmd)
    assert sc.role_can_run("admin", cmd)
    assert not sc.role_can_run("secondary", cmd)
