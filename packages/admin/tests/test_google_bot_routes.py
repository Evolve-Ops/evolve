"""Tests for the bot-facing Google tool routes — /api/google/{state,call}.

THE SECURITY-CRITICAL surface: the calling bot is bound server-side from the
unix-socket kernel peer uid, NEVER from a request field. These tests assert
the cross-bot isolation invariant directly:

  (a) identity binding  — a request whose body claims to be another bot acts
      only as the peer-uid owner (the body field is ignored);
  (b) gating            — an unconfigured bot, and any non-bot/TCP caller, get
      403 (no Google tools);
  (c) dispatch          — a configured Path-C bot's gmail/drive/calendar reads
      succeed through the route (Google mocked);
  (d) audit             — every call is logged (botId, tool, scopes, outcome).

Uses Flask's test client with manually-set request.environ (REMOTE_TRANSPORT /
REMOTE_PEER_UID) to simulate unix-socket peer credentials, the same technique
as test_peer_auth.py. The bot's macOS user is set to the *current* test user
so the real peer_auth resolver maps os.getuid() back to that bot — the binding
is exercised end-to-end, not mocked away.
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import peer_auth
from evolve_admin.web.google_bot_routes import register_google_bot_routes


_ME = pwd.getpwuid(os.getuid()).pw_name


def _write_network(tmp_path: Path, *, configured: bool = True,
                   bot_user: str = _ME) -> Path:
    """Write a network.json with bot 'lex' (owned by `bot_user`) + 'rex'.

    When `configured`, lex gets a Path-C google_integration; rex always gets
    one. lex's account is the current test user (so its uid maps back to lex);
    rex's account is unprovisioned (skipped in the uid map), so there is no
    real uid collision — see test_resolve_peer_bot_id_fails_closed_on_shared_account
    for the drop-on-collision behavior.
    """
    gi = {
        "mode": "service_account_dwd",
        "subject": "lex@example-corp.com",
        "service_account_secret_ref": "google-sa-example-corp",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }
    lex = {"role": "member", "user": bot_user}
    if configured:
        lex["google_integration"] = gi
    net = {
        "bots": {
            "lex": lex,
            "rex": {
                "role": "member",
                "user": "no_such_account_xyz",  # unprovisioned → skipped in map
                "google_integration": {**gi, "subject": "rex@example-corp.com"},
            },
        }
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


def _app(network_path: Path) -> Flask:
    app = Flask(__name__)
    register_google_bot_routes(app, network_path)
    return app


def _unix_env(uid: int | None = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


# ── full app builds (endpoint-name collision regression) ──────────────────────


def test_create_app_registers_google_routes_without_collision():
    """The real create_app() must build with BOTH wizard_google_routes and
    google_bot_routes registered — Flask derives endpoint names from the
    handler function name, so a name clash (e.g. a second `api_google_state`)
    raises at registration and breaks the whole server. Regression guard for
    that class of failure (caught only by the browser smoke suite otherwise)."""
    from evolve_admin.web.server import create_app
    app = create_app()
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/google/state" in rules
    assert "/api/google/call" in rules


# ── (a) identity binding — body `bot` is ignored ──────────────────────────────


def test_call_binds_identity_from_peer_uid_ignoring_body(tmp_path):
    """A request from lex's uid that *claims* bot=rex acts as LEX. The
    body's bot/subject override is ignored — the cross-bot leak is closed."""
    netp = _write_network(tmp_path)
    captured = {}

    def _capture(bot_id, tool, args, network=None):
        captured["bot_id"] = bot_id
        return {"ok": True, "bot": bot_id, "messages": [], "count": 0}

    app = _app(netp)
    with patch("evolve_admin.web.google_bot_routes.google_service.run_google_tool",
               side_effect=_capture):
        with app.test_client() as c:
            resp = c.post("/api/google/call",
                          json={"tool": "gmail_list_messages",
                                "args": {"q": "x"},
                                "bot": "rex", "subject": "evil@example.com"},
                          environ_overrides=_unix_env())
    assert resp.status_code == 200, resp.get_json()
    assert captured["bot_id"] == "lex"   # NOT rex
    assert resp.get_json()["bot"] == "lex"


def test_call_403_for_tcp_request(tmp_path):
    """TCP requests carry no peer credentials → 403. A browser/admin-UI
    session must never be able to act as a bot here."""
    netp = _write_network(tmp_path)
    app = _app(netp)
    with app.test_client() as c:
        # No REMOTE_TRANSPORT → treated as TCP.
        resp = c.post("/api/google/call",
                      json={"tool": "gmail_list_messages", "args": {}},
                      environ_overrides={"REMOTE_PEER_UID": os.getuid()})
    assert resp.status_code == 403


def test_call_403_for_unknown_peer_uid(tmp_path):
    """A unix-socket request whose peer uid maps to no bot → 403."""
    netp = _write_network(tmp_path)
    app = _app(netp)
    with app.test_client() as c:
        resp = c.post("/api/google/call",
                      json={"tool": "gmail_list_messages", "args": {}},
                      environ_overrides=_unix_env(uid=4242424))  # not any bot
    assert resp.status_code == 403


# ── (b) gating — unconfigured bot ⇒ no tools ──────────────────────────────────


def test_call_403_when_bot_not_google_configured(tmp_path):
    """A recognized bot with NO google_integration gets 403 not_configured —
    least-privilege by construction."""
    netp = _write_network(tmp_path, configured=False)
    app = _app(netp)
    with patch("evolve_admin.web.google_bot_routes.google_service.run_google_tool",
               side_effect=AssertionError("must not run a tool for unconfigured bot")):
        with app.test_client() as c:
            resp = c.post("/api/google/call",
                          json={"tool": "gmail_list_messages", "args": {}},
                          environ_overrides=_unix_env())
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "not_configured"


def test_state_reports_unconfigured(tmp_path):
    netp = _write_network(tmp_path, configured=False)
    app = _app(netp)
    with app.test_client() as c:
        resp = c.get("/api/google/state", environ_overrides=_unix_env())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["configured"] is False
    assert body["tools"] == []


def test_state_reports_configured_with_tools(tmp_path):
    netp = _write_network(tmp_path)
    app = _app(netp)
    with app.test_client() as c:
        resp = c.get("/api/google/state", environ_overrides=_unix_env())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["configured"] is True
    assert body["bot"] == "lex"
    assert len(body["tools"]) == 16


def test_state_403_for_tcp(tmp_path):
    netp = _write_network(tmp_path)
    app = _app(netp)
    with app.test_client() as c:
        resp = c.get("/api/google/state",
                     environ_overrides={"REMOTE_PEER_UID": os.getuid()})
    assert resp.status_code == 403


# ── unknown tool ──────────────────────────────────────────────────────────────


def test_call_unknown_tool_400(tmp_path):
    netp = _write_network(tmp_path)
    app = _app(netp)
    with app.test_client() as c:
        resp = c.post("/api/google/call",
                      json={"tool": "frobnicate", "args": {}},
                      environ_overrides=_unix_env())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "unknown_tool"


def test_call_missing_tool_400(tmp_path):
    netp = _write_network(tmp_path)
    app = _app(netp)
    with app.test_client() as c:
        resp = c.post("/api/google/call", json={"args": {}},
                      environ_overrides=_unix_env())
    assert resp.status_code == 400


# ── (c) dispatch — configured Path-C bot reads succeed through the route ───────


def _gmail_service_mock():
    fake_list = MagicMock(execute=MagicMock(return_value={
        "messages": [{"id": "m1", "threadId": "t1"}]}))
    def fake_get(userId, id, format, metadataHeaders):
        return MagicMock(execute=MagicMock(return_value={
            "id": id, "threadId": "t1", "snippet": "hi", "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": "From", "value": "a@b.com"},
                                    {"name": "Subject", "value": "Hi"}]}}))
    msgs = MagicMock()
    msgs.list = MagicMock(return_value=fake_list)
    msgs.get = MagicMock(side_effect=fake_get)
    svc = MagicMock()
    svc.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=msgs)))
    return svc


def test_call_gmail_list_happy_path_through_route(tmp_path):
    netp = _write_network(tmp_path)
    app = _app(netp)
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=_gmail_service_mock()):
        with app.test_client() as c:
            resp = c.post("/api/google/call",
                          json={"tool": "gmail_list_messages", "args": {"max_results": 5}},
                          environ_overrides=_unix_env())
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["bot"] == "lex"
    assert body["count"] == 1
    assert body["messages"][0]["from"] == "a@b.com"


def test_call_drive_list_happy_path_through_route(tmp_path):
    netp = _write_network(tmp_path)
    fake_files_list = MagicMock(execute=MagicMock(return_value={
        "files": [{"id": "f1", "name": "n.txt", "mimeType": "text/plain",
                   "modifiedTime": "2026-06-01T00:00:00Z", "webViewLink": "https://d/f1",
                   "owners": [], "size": "5", "parents": []}]}))
    svc = MagicMock(files=MagicMock(return_value=MagicMock(
        list=MagicMock(return_value=fake_files_list))))
    app = _app(netp)
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service", return_value=svc):
        with app.test_client() as c:
            resp = c.post("/api/google/call",
                          json={"tool": "drive_list_files", "args": {}},
                          environ_overrides=_unix_env())
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["files"][0]["id"] == "f1"


def test_call_calendar_list_happy_path_through_route(tmp_path):
    netp = _write_network(tmp_path)
    fake_events = MagicMock(execute=MagicMock(return_value={
        "items": [{"id": "e1", "summary": "S",
                   "start": {"dateTime": "2026-06-15T09:00:00-07:00"},
                   "end": {"dateTime": "2026-06-15T09:30:00-07:00"},
                   "status": "confirmed"}]}))
    svc = MagicMock(events=MagicMock(return_value=MagicMock(
        list=MagicMock(return_value=fake_events))))
    app = _app(netp)
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service", return_value=svc):
        with app.test_client() as c:
            resp = c.post("/api/google/call",
                          json={"tool": "calendar_list_events", "args": {}},
                          environ_overrides=_unix_env())
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["events"][0]["summary"] == "S"


# ── tool-level errors map to clean statuses ───────────────────────────────────


def test_call_validation_error_maps_to_400(tmp_path):
    """A tool ValueError (e.g. missing required arg) → 400 bad_request."""
    netp = _write_network(tmp_path)
    app = _app(netp)
    # gmail_get_message requires `id`; omit it. Creds/build are never reached
    # because the validation raises first.
    with app.test_client() as c:
        resp = c.post("/api/google/call",
                      json={"tool": "gmail_get_message", "args": {}},
                      environ_overrides=_unix_env())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "bad_request"


def test_call_insufficient_scopes_maps_to_403(tmp_path):
    netp = _write_network(tmp_path)
    from evolve_admin import google_auth
    app = _app(netp)
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               side_effect=google_auth.InsufficientGrantedScopes(
                   bot_id="lex", missing=["scopeX"], granted=[])):
        with app.test_client() as c:
            resp = c.post("/api/google/call",
                          json={"tool": "gmail_list_messages", "args": {}},
                          environ_overrides=_unix_env())
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "insufficient_scopes"


# ── (d) audit ─────────────────────────────────────────────────────────────────


def test_call_audits_outcome(tmp_path):
    netp = _write_network(tmp_path)
    audits = []
    app = _app(netp)
    with patch("evolve_admin.web.google_bot_routes._audit_log_entry",
               side_effect=lambda action, bot, details, **kw: audits.append((action, bot, details))), \
         patch("evolve_admin.web.google_bot_routes.google_service.run_google_tool",
               return_value={"ok": True, "bot": "lex"}):
        with app.test_client() as c:
            c.post("/api/google/call",
                   json={"tool": "gmail_list_messages", "args": {}},
                   environ_overrides=_unix_env())
    assert len(audits) == 1
    action, bot, details = audits[0]
    assert action == "google.bot_call.gmail_list_messages"
    assert bot == "lex"
    assert details["outcome"] == "ok"
    assert details["tool"] == "gmail_list_messages"


def test_call_audits_missing_tool(tmp_path):
    """Even a malformed (missing tool) call is audited — the 'every call is
    audited' contract has no gap."""
    netp = _write_network(tmp_path)
    audits = []
    app = _app(netp)
    with patch("evolve_admin.web.google_bot_routes._audit_log_entry",
               side_effect=lambda action, bot, details, **kw: audits.append((action, bot, details))):
        with app.test_client() as c:
            resp = c.post("/api/google/call", json={"args": {}},
                          environ_overrides=_unix_env())
    assert resp.status_code == 400
    assert any(d["outcome"] == "bad_request" for _, _, d in audits)


def test_call_audits_not_configured(tmp_path):
    netp = _write_network(tmp_path, configured=False)
    audits = []
    app = _app(netp)
    with patch("evolve_admin.web.google_bot_routes._audit_log_entry",
               side_effect=lambda action, bot, details, **kw: audits.append((action, bot, details))):
        with app.test_client() as c:
            c.post("/api/google/call",
                   json={"tool": "gmail_list_messages", "args": {}},
                   environ_overrides=_unix_env())
    assert any(d["outcome"] == "not_configured" for _, _, d in audits)


# ── peer_auth resolver (the binding primitive) ────────────────────────────────


def test_resolve_peer_bot_id_maps_current_uid(tmp_path):
    netp = _write_network(tmp_path)
    app = Flask(__name__)
    with app.test_request_context(environ_overrides=_unix_env()):
        assert peer_auth.resolve_peer_bot_id(netp) == "lex"


def test_resolve_peer_bot_id_none_for_tcp(tmp_path):
    netp = _write_network(tmp_path)
    app = Flask(__name__)
    with app.test_request_context(
            environ_overrides={"REMOTE_PEER_UID": os.getuid()}):
        assert peer_auth.resolve_peer_bot_id(netp) is None


def test_resolve_peer_bot_id_none_for_unknown_uid(tmp_path):
    netp = _write_network(tmp_path)
    app = Flask(__name__)
    with app.test_request_context(environ_overrides=_unix_env(uid=4242424)):
        assert peer_auth.resolve_peer_bot_id(netp) is None


def test_resolve_peer_bot_id_fails_closed_on_shared_account(tmp_path):
    """Two bots on ONE macOS account (same uid) is ambiguous — the peer uid
    can't uniquely identify the caller, so resolve returns None (fail-closed)
    rather than silently binding to one of them and exposing the wrong bot's
    Google data."""
    net = {
        "bots": {
            "twin_a": {"role": "member", "user": _ME,
                       "google_integration": {"mode": "service_account_dwd",
                                              "subject": "a@x.com",
                                              "service_account_secret_ref": "sa",
                                              "scopes": []}},
            "twin_b": {"role": "member", "user": _ME,  # SAME account as twin_a
                       "google_integration": {"mode": "service_account_dwd",
                                              "subject": "b@x.com",
                                              "service_account_secret_ref": "sa",
                                              "scopes": []}},
        }
    }
    netp = tmp_path / "network.json"
    netp.write_text(json.dumps(net))
    app = Flask(__name__)
    with app.test_request_context(environ_overrides=_unix_env()):
        assert peer_auth.resolve_peer_bot_id(netp) is None


def test_peer_bot_route_exempt_only_bot_facing_paths(tmp_path):
    netp = _write_network(tmp_path)
    app = Flask(__name__)
    # Both bot-facing prefixes + the exact expand_app path are exempt for a
    # recognized bot peer …
    for p in ("/api/google/call", "/api/directory/lookup", "/api/directory/digest",
              "/api/applications/expand"):
        with app.test_request_context(p, environ_overrides=_unix_env()):
            assert peer_auth.peer_bot_route_exempt(netp) is True, p
    # … but no other admin surface is, even from the same trusted peer — including
    # the admin-only app-lifecycle routes under the SAME /api/applications/ subtree
    # (they must stay gated; the expand exemption is an EXACT path, not a prefix).
    for p in ("/api/admin/secrets", "/api/applications/lex/journal/pause",
              "/api/applications/expandx"):
        with app.test_request_context(p, environ_overrides=_unix_env()):
            assert peer_auth.peer_bot_route_exempt(netp) is False, p
