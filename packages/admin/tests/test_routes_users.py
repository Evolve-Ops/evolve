"""Tests for the admin-UI ``GET /api/users/roster`` read (routes_users.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Flask

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin import users_roster as ur  # noqa: E402
from evolve_admin.web.routes_users import register_users_roster_routes  # noqa: E402

BOT_A = "team_bot_a"
BOT_B = "team_bot_b"


def _write_network(tmp_path: Path) -> Path:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            BOT_A: {"role": "member", "port": 19002},
            BOT_B: {"role": "member", "port": 19003},
        },
        "pod": {"admins": {"external_ids": {}, "names": {}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


def _stub_list_users(monkeypatch):
    users = [
        {"person_key": "telegram:111", "display_name": "P1",
         "channels": [{"platform": "telegram", "stable_id": "111",
                       "handle": None, "email": None}],
         "bots": [{"bot_id": BOT_A, "platform": "telegram", "stable_id": "111",
                   "role": "participant", "engagement_surfaces": [],
                   "rights_summary": "chat only", "labels": [], "last_seen": None,
                   "turn_count": None, "usage_key": "telegram:111"}],
         "apps": [{"app_id": "app-everyone", "name": "Everyone App",
                   "bot_id": BOT_A, "audience": "everyone", "access": "granted"}],
         "linked_accounts": {"status": "unknown"}},
        {"person_key": "slack:U2", "display_name": "P2",
         "channels": [{"platform": "slack", "stable_id": "U2",
                       "handle": None, "email": None}],
         "bots": [{"bot_id": BOT_B, "platform": "slack", "stable_id": "U2",
                   "role": "primary_user", "engagement_surfaces": [],
                   "rights_summary": "full control", "labels": ["owner"],
                   "last_seen": None, "turn_count": None, "usage_key": "slack:U2"}],
         "apps": [], "linked_accounts": {"status": "unknown"}},
    ]
    monkeypatch.setattr(ur, "list_users", lambda net: users)
    return users


def test_roster_returns_every_user(tmp_path, monkeypatch):
    network_path = _write_network(tmp_path)
    _stub_list_users(monkeypatch)
    a = Flask(__name__)
    register_users_roster_routes(a, network_path)
    with a.test_client() as c:
        resp = c.get("/api/users/roster")
    assert resp.status_code == 200
    names = {u["display_name"] for u in resp.get_json()["users"]}
    assert names == {"P1", "P2"}


def test_roster_filters_by_bot(tmp_path, monkeypatch):
    network_path = _write_network(tmp_path)
    _stub_list_users(monkeypatch)
    a = Flask(__name__)
    register_users_roster_routes(a, network_path)
    with a.test_client() as c:
        resp = c.get(f"/api/users/roster?bot={BOT_B}")
    names = {u["display_name"] for u in resp.get_json()["users"]}
    assert names == {"P2"}


def test_roster_filters_by_app(tmp_path, monkeypatch):
    network_path = _write_network(tmp_path)
    _stub_list_users(monkeypatch)
    a = Flask(__name__)
    register_users_roster_routes(a, network_path)
    with a.test_client() as c:
        resp = c.get("/api/users/roster?app=app-everyone")
    names = {u["display_name"] for u in resp.get_json()["users"]}
    assert names == {"P1"}
