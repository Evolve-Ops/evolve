"""Tests for the bot-facing ``/api/users-bot/{whoami,list}`` routes.

Mirrors ``test_directory_bot_routes.py``'s technique (real peer-uid binding via
``REMOTE_TRANSPORT``/``REMOTE_PEER_UID`` environ overrides — the current test user IS
the bot, so the real ``peer_auth`` resolver maps it back). Covers the SECURITY-CRITICAL
plumbing this route owns: peer-uid binding (never a request field) and the
``X-Requester-Identity`` header contract. The audience/refusal BUSINESS LOGIC is
covered exhaustively at the pure-function level in ``test_users_roster.py``; these
tests exercise the wiring around it (app_id omitted throughout, so no manifest
fixtures are needed — a plain, unscoped call, same as ``directory_lookup``'s own
no-app-concept precedent).

FAKE ids + ``*.example`` only (docs/PLACEHOLDER_NAMING.md).
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web import routes_bot_users as rbu  # noqa: E402
from evolve_admin.web import users_bot_routes as ubr  # noqa: E402
from evolve_admin.web.users_bot_routes import register_users_bot_routes  # noqa: E402

_ME = pwd.getpwuid(os.getuid()).pw_name
BOT = "lex"


def _write_network(tmp_path: Path) -> Path:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {BOT: {"role": "member", "user": _ME}},
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}, "names": {}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


def _write_allowfrom(tmp_path: Path, channel: str, ids: list[str]) -> None:
    creds = tmp_path / "Users" / BOT / ".openclaw" / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / f"{channel}-default-allowFrom.json").write_text(
        json.dumps({"allowFrom": ids}))


@pytest.fixture
def app(tmp_path, monkeypatch):
    network_path = _write_network(tmp_path)
    monkeypatch.setattr(rbu, "bot_home", lambda bot, net: tmp_path / "Users" / bot)
    # The route's audit call writes to the hard-coded REAL
    # /Users/Shared/evolve/audit-log.jsonl (routes_shared._audit_log_entry) — the
    # same shape directory_bot_routes uses in production. Never real in a test.
    monkeypatch.setattr(ubr, "_audit_log_entry", lambda *a, **k: None)
    _write_allowfrom(tmp_path, "telegram", ["111", "999"])
    a = Flask(__name__)
    register_users_bot_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def _unix_env(uid: "int | None" = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


# ── identity binding ────────────────────────────────────────────────────


def test_whoami_403_for_tcp(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/whoami", json={},
                       headers={"X-Requester-Identity": "telegram:111"},
                       environ_overrides={"REMOTE_PEER_UID": os.getuid()})
    assert resp.status_code == 403


def test_whoami_403_for_unknown_uid(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/whoami", json={},
                       headers={"X-Requester-Identity": "telegram:111"},
                       environ_overrides=_unix_env(uid=4242424))
    assert resp.status_code == 403


def test_whoami_400_for_missing_requester_header(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/whoami", json={},
                       environ_overrides=_unix_env())
    assert resp.status_code == 400


def test_whoami_400_for_malformed_requester_header(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/whoami", json={},
                       headers={"X-Requester-Identity": "not-a-colon-pair"},
                       environ_overrides=_unix_env())
    assert resp.status_code == 400


def test_whoami_returns_own_record_over_the_socket(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/whoami", json={},
                       headers={"X-Requester-Identity": "telegram:111"},
                       environ_overrides=_unix_env())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"]["channels"][0] == {
        "platform": "telegram", "stable_id": "111",
        "handle": None, "email": None,
    }


def test_list_returns_whole_bot_roster_when_not_app_mediated(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/list", json={},
                       headers={"X-Requester-Identity": "telegram:111"},
                       environ_overrides=_unix_env())
    assert resp.status_code == 200
    ids = {u["channels"][0]["stable_id"] for u in resp.get_json()["users"]}
    assert ids == {"111", "999"}


def test_list_403_for_tcp(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/list", json={},
                       headers={"X-Requester-Identity": "telegram:111"},
                       environ_overrides={"REMOTE_PEER_UID": os.getuid()})
    assert resp.status_code == 403


def test_whoami_app_id_must_be_a_string(app):
    with app.test_client() as c:
        resp = c.post("/api/users-bot/whoami", json={"app_id": 123},
                       headers={"X-Requester-Identity": "telegram:111"},
                       environ_overrides=_unix_env())
    assert resp.status_code == 400
