"""End-to-end tests for the admin pairing wizard Flask routes.

Covers ``routes_pairing``:

  GET  /api/admin/pairing/config
  GET  /api/admin/bots/<bot>/pairing/lookup?code=XYZ
  POST /api/admin/bots/<bot>/pairing/commit
  GET  /api/admin/bots/<bot>/pairing/state

The commit endpoint is the load-bearing one — its role routing
determines where the operator's ID lands in network.json (pod
admins vs the bot's primary user vs nothing extra). Test all three
roles explicitly.

Same tmp_path bot-home redirection as test_routes_bot_users — the
commit step calls into routes_bot_users._approve which writes the
bot's allowFrom file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web import routes_pairing as rp  # noqa: E402
from evolve_admin.web import routes_bot_users as rbu  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _seed_network(tmp_path: Path) -> Path:
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["atlas", "team_bot_a"],
        "bots": {
            "atlas": {
                "role": "member", "port": 19010, "multiUser": False,
            },
            "team_bot_a": {
                "role": "member", "port": 19002, "multiUser": True,
                "primary_user": {"name": "Sam Sample", "external_ids": {}},
            },
        },
        "pod": {"admins": {"external_ids": {}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(base, indent=2))
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    return p


def _bot_creds_dir(tmp_path: Path, bot: str) -> Path:
    d = tmp_path / "Users" / bot / ".openclaw" / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(p: Path, payload: dict) -> None:
    p.write_text(json.dumps(payload))


def _seed_pending(tmp_path: Path, bot: str, channel: str, *,
                  id_: str, code: str, meta: dict | None = None) -> Path:
    creds = _bot_creds_dir(tmp_path, bot)
    pairing_path = creds / f"{channel}-pairing.json"
    pairing_path.write_text(json.dumps({
        "version": 1,
        "requests": [{
            "id": id_, "code": code, "createdAt": "2026-06-01T17:00:00Z",
            "meta": meta or {},
        }],
    }))
    # Empty allowFrom so commit can demonstrate the move.
    (creds / f"{channel}-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))
    return pairing_path


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    network_path = _seed_network(tmp_path)
    # Both modules use ``bot_home`` to resolve credentials dir; redirect
    # both so the test exercises the real file-write paths without sudo.
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    a = Flask(__name__)
    rp.register_routes(a, network_path)
    rbu.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a, network_path


# ── Config endpoint ─────────────────────────────────────────────────────────


def test_config_endpoint_returns_known_channels(app):
    a, _ = app
    with a.test_client() as c:
        data = c.get("/api/admin/pairing/config").get_json()
    chs = {row["channel"] for row in data["channels"]}
    assert chs == {"telegram", "slack", "discord", "whatsapp"}
    # Each row carries the keys the JS modal consumes.
    for row in data["channels"]:
        assert "id_validator_pattern" in row
        assert "discovery_method" in row
        assert "has_deeplink" in row


# ── Lookup ──────────────────────────────────────────────────────────────────


def test_lookup_finds_pending_by_code(app, tmp_path):
    a, _ = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ",
                  meta={"firstName": "Pod", "lastName": "Admin",
                        "username": "pod_admin"})
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/lookup?code=WX42YZ").get_json()
    assert data["found"] is True
    assert data["channel"] == "telegram"
    assert data["id"] == "1260193629"
    assert data["code"] == "WX42YZ"
    # display_name derived from telegram firstName + lastName
    assert data["display_name"] == "Pod Admin"
    assert data["meta"]["username"] == "pod_admin"


def test_lookup_returns_found_false_when_no_match(app, tmp_path):
    a, _ = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/lookup?code=NOPENOPE").get_json()
    assert data == {"found": False}


def test_lookup_400s_on_missing_code(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.get("/api/admin/bots/atlas/pairing/lookup")
    assert resp.status_code == 400


def test_lookup_404s_on_unknown_bot(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.get("/api/admin/bots/nonesuch/pairing/lookup?code=X")
    assert resp.status_code == 404


def test_lookup_searches_all_channels(app, tmp_path):
    """A pasted code with no channel hint should match across all
    channels — the modal's primary input is paste-the-code, not
    pick-the-channel."""
    a, _ = app
    _seed_pending(tmp_path, "team_bot_a", "slack",
                  id_="U01ABCDE2FG", code="SLK99",
                  meta={"real_name": "Sam Sample"})
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/team_bot_a/pairing/lookup?code=SLK99").get_json()
    assert data["found"] is True
    assert data["channel"] == "slack"
    assert data["display_name"] == "Sam Sample"


# ── Commit: pod_admin role ──────────────────────────────────────────────────


def test_commit_pod_admin_writes_external_ids_and_resolved_names(app, tmp_path):
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ",
                  meta={"firstName": "Pod"})
    with a.test_client() as c:
        resp = c.post(
            "/api/admin/bots/atlas/pairing/commit",
            json={
                "channel": "telegram", "id": "1260193629",
                "role": "pod_admin", "name": "Pod Admin",
            },
        )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["ok"] is True
    net = json.loads(network_path.read_text())
    # Pod-wide promotion: ID lands in admins.external_ids.telegram.
    assert "1260193629" in net["pod"]["admins"]["external_ids"]["telegram"]
    # And the resolved_names cache gets a name entry so the Users
    # page renders without a channel-API round trip.
    assert net["pod"]["admins"]["resolved_names"]["telegram:1260193629"]["name"] == "Pod Admin"
    # Per-bot approve also lands — allowFrom now contains the ID.
    allow = json.loads(
        (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
         / "telegram-default-allowFrom.json").read_text())
    assert "1260193629" in allow["allowFrom"]
    # Pending request is cleared.
    pairing = json.loads(
        (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
         / "telegram-pairing.json").read_text())
    assert pairing["requests"] == []


def test_commit_pod_admin_is_idempotent(app, tmp_path):
    """Re-pairing the same admin shouldn't duplicate IDs in
    external_ids — the operator might run the wizard twice."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="9991112222", code="A1", meta={})
    with a.test_client() as c:
        c.post("/api/admin/bots/atlas/pairing/commit",
               json={"channel": "telegram", "id": "9991112222",
                     "role": "pod_admin"})
    # Manual re-pair via the API
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="9991112222", code="A2", meta={})
    with a.test_client() as c:
        c.post("/api/admin/bots/atlas/pairing/commit",
               json={"channel": "telegram", "id": "9991112222",
                     "role": "pod_admin"})
    net = json.loads(network_path.read_text())
    assert net["pod"]["admins"]["external_ids"]["telegram"].count("9991112222") == 1


# ── Commit: primary role ────────────────────────────────────────────────────


def test_commit_primary_user_writes_per_bot_only(app, tmp_path):
    a, network_path = app
    _seed_pending(tmp_path, "team_bot_a", "telegram",
                  id_="8001234567", code="P1", meta={"firstName": "Sam"})
    with a.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/pairing/commit",
            json={"channel": "telegram", "id": "8001234567",
                  "role": "primary", "name": "Sam Sample"},
        )
    assert resp.status_code == 200, resp.get_json()
    net = json.loads(network_path.read_text())
    # Per-bot primary_user.external_ids gets the new ID.
    assert net["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"] == ["8001234567"]
    assert net["bots"]["team_bot_a"]["primary_user"]["name"] == "Sam Sample"
    # NOT promoted to pod admin.
    assert "8001234567" not in (
        net["pod"]["admins"]["external_ids"].get("telegram") or [])
    # But IS approved for THIS bot's allowFrom.
    allow = json.loads(
        (tmp_path / "Users" / "team_bot_a" / ".openclaw" / "credentials"
         / "telegram-default-allowFrom.json").read_text())
    assert "8001234567" in allow["allowFrom"]


def test_commit_primary_replaces_existing_primary_id(app, tmp_path):
    """Operator deliberately re-pairing the primary should win — the
    old primary's ID stays in allowFrom (can be revoked from the
    Users page) but is no longer the recorded primary."""
    a, network_path = app
    # Pre-seed an existing primary in network.json (any string — only
    # the new value is regex-validated)
    net = json.loads(network_path.read_text())
    net["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"] = "1111111111"
    network_path.write_text(json.dumps(net, indent=2))

    _seed_pending(tmp_path, "team_bot_a", "telegram",
                  id_="2222222222", code="P2")
    with a.test_client() as c:
        c.post("/api/admin/bots/team_bot_a/pairing/commit",
               json={"channel": "telegram", "id": "2222222222",
                     "role": "primary"})
    net = json.loads(network_path.read_text())
    assert net["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"] == ["2222222222"]


# ── Commit: other role ──────────────────────────────────────────────────────


def test_commit_other_only_approves_no_network_change(app, tmp_path):
    a, network_path = app
    before = network_path.read_text()
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="7770001234", code="X1")
    with a.test_client() as c:
        resp = c.post(
            "/api/admin/bots/atlas/pairing/commit",
            json={"channel": "telegram", "id": "7770001234",
                  "role": "other"},
        )
    assert resp.status_code == 200, resp.get_json()
    # network.json unchanged — no pod-admin or primary promotion.
    assert network_path.read_text() == before
    # But ID is approved into the bot's allowFrom.
    allow = json.loads(
        (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
         / "telegram-default-allowFrom.json").read_text())
    assert "7770001234" in allow["allowFrom"]


# ── Commit validation ──────────────────────────────────────────────────────


def test_commit_rejects_unknown_channel(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json={"channel": "signal", "id": "999",
                            "role": "pod_admin"})
    assert resp.status_code == 400


def test_commit_rejects_invalid_id_format(app):
    a, _ = app
    with a.test_client() as c:
        # Telegram ID validator wants 6-12 digits; "abc" fails.
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json={"channel": "telegram", "id": "abc",
                            "role": "pod_admin"})
    assert resp.status_code == 400


def test_commit_rejects_unknown_role(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json={"channel": "telegram", "id": "1260193629",
                            "role": "superuser"})
    assert resp.status_code == 400


def test_commit_404s_on_unknown_bot(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/nonesuch/pairing/commit",
                      json={"channel": "telegram", "id": "1260193629",
                            "role": "pod_admin"})
    assert resp.status_code == 404


# ── State ──────────────────────────────────────────────────────────────────


def test_state_reports_unpaired_then_paired(app, tmp_path):
    a, _ = app
    _bot_creds_dir(tmp_path, "atlas")
    (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
     / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))

    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/state?channel=telegram").get_json()
    assert data["channels"]["telegram"]["paired"] is False

    # Add an approved ID; state should flip.
    (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
     / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["1260193629"]}))
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/state?channel=telegram").get_json()
    assert data["channels"]["telegram"]["paired"] is True
    assert data["channels"]["telegram"]["approved_count"] == 1
