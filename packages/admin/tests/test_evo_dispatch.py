"""tests/test_evo_dispatch.py — /api/evo/dispatch + /api/evo/help endpoint tests.

Spec: internal/spec-evo-wizard-2026-05-05.md.

Exercises:
  * subcommand parsing (bare, with subcommand, with args, edge cases)
  * dispatch routing (bare evo, help, stub, unknown subcommand)
  * role filtering (primary vs secondary)
  * Flask routes via test client
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand parser
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_bare():
    from evolve_admin.evo.subcommands import parse

    for text in ("evo", "evolve", "EVO", "  evo  ", "evo.", "evo!", "Evolve?"):
        result = parse(text)
        assert result is not None, text
        assert result.is_bare, text
        assert result.subcommand == "evo"
        assert result.args == ""


def test_parse_subcommand():
    from evolve_admin.evo.subcommands import parse

    result = parse("evo help")
    assert result is not None
    assert result.subcommand == "help"
    assert result.args == ""
    assert not result.is_bare


def test_parse_with_args():
    from evolve_admin.evo.subcommands import parse

    result = parse("evo default better")
    assert result is not None
    assert result.subcommand == "default"
    assert result.args == "better"
    assert not result.is_bare


def test_parse_case_insensitive_head():
    from evolve_admin.evo.subcommands import parse

    result = parse("EVO Wizard")
    assert result is not None
    assert result.subcommand == "wizard"


def test_parse_non_evo_returns_none():
    from evolve_admin.evo.subcommands import parse

    for text in ("hello", "evolution", "evosomething", "", "  "):
        assert parse(text) is None, text


# ─────────────────────────────────────────────────────────────────────────────
# Identity resolver
# ─────────────────────────────────────────────────────────────────────────────


def test_identity_falls_back_to_primary_when_unrecorded():
    from evolve_admin.evo.identity import resolve_role

    network = {"bots": {"team_bot_a": {}}}
    assert resolve_role(network, "team_bot_a", "telegram", "anyone") == "primary"


def test_identity_matches_recorded_primary():
    from evolve_admin.evo.identity import resolve_role

    network = {
        "bots": {
            "team_bot_a": {
                "primary_user": {
                    "pod_user": "pod_admin_user",
                    "external_ids": {"telegram": "12345"},
                }
            }
        }
    }
    assert resolve_role(network, "team_bot_a", "telegram", "12345") == "primary"
    assert resolve_role(network, "team_bot_a", "telegram", "67890") == "secondary"
    # Unknown channel even though primary is recorded → fallback to primary
    assert resolve_role(network, "team_bot_a", "slack", "anyone") == "primary"


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher core
# ─────────────────────────────────────────────────────────────────────────────


def _dispatch(network, raw_text):
    from evolve_admin.evo.dispatch import dispatch

    return dispatch(
        network,
        bot_id="team_bot_a",
        channel="telegram",
        sender_external_id="12345",
        raw_text=raw_text,
    )


def test_bare_evo_returns_orientation():
    """Bare ``evo`` no longer dual-routes (wizard if not onboarded,
    rec_pending otherwise). It returns a short orientation message
    pointing at ``evo wizard`` / ``evo better`` + ``evo help``. The
    orientation surfaces no wizard session — the user has to invoke
    those subcommands explicitly."""
    result = _dispatch({}, "evo")
    assert result.subcommand == "evo"
    assert result.mode == "speak"
    assert result.wizard_session_id is None
    body = result.direct_send_message or ""
    # Empty network = no sharedDir = no marker = "not onboarded" path.
    assert "evo wizard" in body
    assert "evo help" in body


def test_evo_better_starts_rec_pending():
    """Explicit ``evo better`` always starts the rec_pending flow — the
    legacy onboarding pre-emption is gone (bare ``evo`` does that job
    now)."""
    result = _dispatch({}, "evo better")
    assert result.subcommand == "better"
    assert result.mode == "speak"
    assert "isn't available" not in (result.system_append or "")


def test_evo_help_speaks_help_text():
    result = _dispatch({}, "evo help")
    assert result.mode == "speak"
    assert result.subcommand == "help"
    assert result.system_append is not None
    # Static help block should mention several subcommands.
    body = result.system_append
    assert "evo wizard" in body
    assert "evo better" in body


def test_evo_help_populates_direct_send_message():
    """`evo help` is non-conversational, so the dispatcher must surface a
    bare user-facing body in ``direct_send_message`` for the plugin to
    deliver verbatim via the channel API (Telegram). The body must NOT
    include the "respond verbatim" preamble — that's only for the LLM
    side via ``system_append``."""
    result = _dispatch({}, "evo help")
    assert result.direct_send_message is not None
    assert "evo wizard" in result.direct_send_message
    assert "evo better" in result.direct_send_message
    assert "verbatim" not in result.direct_send_message.lower()
    assert "IMPORTANT" not in result.direct_send_message
    # system_append still wraps the body with the verbatim preamble.
    assert "verbatim" in (result.system_append or "").lower()


def test_evo_wizard_starts_a_session(tmp_path):
    """Slice 5a wired the wizard for primary callers; dispatch now starts a
    real session (with wizard_session_id set) instead of stubbing.
    Detailed behavior lives in test_evo_wizard.py — this is the smoke
    check that the dispatch branch is wired."""
    from evolve_admin.evo import dispatch as _disp

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    result = _disp.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo wizard",
    )
    assert result.mode == "speak"
    assert result.subcommand == "wizard"
    assert result.wizard_session_id  # non-None, non-empty
    assert "[EVO WIZARD" in (result.system_append or "")


def test_evo_wizard_does_not_populate_direct_send_message(tmp_path):
    """Wizard is conversational across multiple turns — the LLM should
    engage with the user, so direct_send_message stays None and the
    plugin echoes system_append rather than direct-sending."""
    from evolve_admin.evo import dispatch as _disp

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    result = _disp.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo wizard",
    )
    assert result.direct_send_message is None


def test_unknown_subcommand_returns_speak_with_message():
    result = _dispatch({}, "evo nonsense")
    assert result.mode == "speak"
    assert "evo help" in (result.system_append or "")
    # Error messages from the dispatcher are direct-sendable.
    assert result.direct_send_message is not None
    assert "evo help" in result.direct_send_message


def test_secondary_user_blocked_from_better():
    """Secondary users cannot run `evo better` — the registry restricts it."""
    network = {
        "bots": {
            "team_bot_a": {
                "primary_user": {
                    "pod_user": "pod_admin_user",
                    "external_ids": {"telegram": "owner-id"},
                }
            }
        }
    }
    from evolve_admin.evo.dispatch import dispatch

    result = dispatch(
        network,
        bot_id="team_bot_a",
        channel="telegram",
        sender_external_id="some-other-user",  # not the recorded primary
        raw_text="evo better",
    )
    # Note: explicit "evo better" goes through the role-availability filter
    # BEFORE the rec_pending dispatch, so a secondary user gets the
    # not-available message rather than a recommendation.
    assert result.mode == "speak"
    assert "isn't available" in (result.system_append or "")
    assert result.role == "secondary"
    # The role-block message is a terse error — direct-sendable.
    assert result.direct_send_message is not None
    assert "isn't available" in result.direct_send_message


def test_help_filters_by_role():
    from evolve_admin.evo.subcommands import subcommands_for_role

    primary_cmds = {sc.name for sc in subcommands_for_role("primary")}
    secondary_cmds = {sc.name for sc in subcommands_for_role("secondary")}

    # Primary sees more than secondary
    assert primary_cmds > secondary_cmds
    # Better/apps/guide are primary-only
    assert "better" in primary_cmds
    assert "better" not in secondary_cmds
    assert "guide" in primary_cmds
    assert "guide" not in secondary_cmds
    # Help and wizard available to both
    assert "help" in primary_cmds and "help" in secondary_cmds
    assert "wizard" in primary_cmds and "wizard" in secondary_cmds


def test_help_admin_sees_every_primary_command():
    """Admin is a strict superset of primary — mirroring the dispatcher's
    gate. Without this, `evo help` shows admins only `claim` (the lone
    command tagged ALL), which is what regression bit Pod_admin on 2026-05-12.
    """
    from evolve_admin.evo.subcommands import subcommands_for_role

    admin_cmds = {sc.name for sc in subcommands_for_role("admin")}
    primary_cmds = {sc.name for sc in subcommands_for_role("primary")}

    # Admin sees at least everything primary sees, plus claim
    assert primary_cmds.issubset(admin_cmds)
    assert "better" in admin_cmds
    assert "guide" in admin_cmds
    assert "apps" in admin_cmds
    assert "summary" in admin_cmds
    assert "claim" in admin_cmds


def test_role_can_run_helper_is_single_source_of_truth():
    """All three role gates (dispatcher, subcommands_for_role, help long)
    must agree. Asserts the helper covers admin-bypass, primary, and
    secondary cases without surprises.
    """
    from evolve_admin.evo.subcommands import role_can_run, lookup

    better = lookup("better")  # PRIMARY
    claim = lookup("claim")    # ALL
    help_cmd = lookup("help")  # BOTH (primary+secondary)

    assert role_can_run("primary", better) is True
    assert role_can_run("secondary", better) is False
    assert role_can_run("admin", better) is True   # bypass

    assert role_can_run("admin", claim) is True
    assert role_can_run("primary", claim) is True
    assert role_can_run("secondary", claim) is True

    assert role_can_run("primary", help_cmd) is True
    assert role_can_run("secondary", help_cmd) is True
    assert role_can_run("admin", help_cmd) is True  # bypass via primary


def test_help_long_form_admin_can_view_primary_only_commands(tmp_path):
    """Regression: `evo help better` rejected admin with "isn't available"
    even though `evo help` listed `better` in the available set. The
    inconsistency was the gate that protects this PR.
    """
    from evolve_admin.evo.dispatch import dispatch

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "pod": {
            "admins": {"external_ids": {"telegram": ["999"]}, "pod_users": []},
        },
    }
    r = dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="999",  # admin
        raw_text="evo help better",
    )
    body = r.system_append or ""
    assert "isn't available" not in body
    # The long-form payload should include the command name + short_help
    assert "**evo better**" in body
    assert r.role == "admin"


def test_help_long_form_secondary_still_blocked_from_primary_commands(tmp_path):
    """The fix must not silently expand secondary access — only admin
    gets the superset treatment.
    """
    from evolve_admin.evo.dispatch import dispatch

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "bots": {
            "team_bot_a": {"primary_user": {"external_ids": {"telegram": "12345"}}},
        },
    }
    r = dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="77777",  # secondary
        raw_text="evo help better",
    )
    body = r.system_append or ""
    assert "isn't available" in body
    assert r.role == "secondary"


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def evo_app(tmp_path):
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    return app


def test_dispatch_route_help(evo_app):
    client = evo_app.test_client()
    r = client.post(
        "/api/evo/dispatch",
        json={
            "bot_id": "team_bot_a",
            "channel": "telegram",
            "sender_external_id": "12345",
            "raw_text": "evo help",
        },
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["mode"] == "speak"
    assert data["subcommand"] == "help"
    assert "system_append" in data and data["system_append"]
    # direct_send_message must be on the wire so the plugin can route the
    # response through Telegram Bot API instead of trusting LLM-verbatim.
    assert "direct_send_message" in data
    assert data["direct_send_message"]
    assert "evo wizard" in data["direct_send_message"]


def test_dispatch_route_bare_evo(evo_app):
    """Bare ``evo`` goes through rec_pending dispatch (mode=speak)."""
    client = evo_app.test_client()
    r = client.post(
        "/api/evo/dispatch",
        json={
            "bot_id": "team_bot_a",
            "channel": "telegram",
            "sender_external_id": "12345",
            "raw_text": "evo",
        },
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["mode"] == "speak"
    assert data["subcommand"] == "evo"


def test_dispatch_route_missing_raw_text(evo_app):
    client = evo_app.test_client()
    r = client.post("/api/evo/dispatch", json={"bot_id": "team_bot_a"})
    assert r.status_code == 400


def test_dispatch_route_missing_bot_id(evo_app):
    client = evo_app.test_client()
    r = client.post("/api/evo/dispatch", json={"raw_text": "evo help"})
    assert r.status_code == 400


def test_help_route_default_role_is_primary(evo_app):
    client = evo_app.test_client()
    r = client.get("/api/evo/help")
    assert r.status_code == 200
    data = r.get_json()
    assert data["role"] == "primary"
    names = [sc["name"] for sc in data["subcommands"]]
    assert "better" in names  # primary-only command surfaces


def test_help_route_role_override_secondary(evo_app):
    client = evo_app.test_client()
    r = client.get("/api/evo/help?role=secondary")
    assert r.status_code == 200
    data = r.get_json()
    assert data["role"] == "secondary"
    names = [sc["name"] for sc in data["subcommands"]]
    assert "better" not in names  # primary-only filtered out
    assert "guide" not in names
    assert "help" in names
    assert "wizard" in names
