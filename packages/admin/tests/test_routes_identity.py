"""End-to-end tests for the Phase 5 identity-claim Flask routes.

Verifies the three endpoints introduced by routes_identity.py:
  - GET /api/admin/identity — overview aggregation
  - POST /api/admin/identity/claim-admin — pod-wide admin claim
  - POST /api/admin/identity/claim-primary — per-bot primary claim

Mounts the routes on a minimal Flask app pointed at a tmp_path
network.json so claim writes don't touch the real config.
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

from evolve_admin.web.routes_identity import register_routes  # noqa: E402


def _seed_network(tmp_path: Path, **overrides) -> Path:
    """Write a minimal network.json. Overrides merge shallowly into the base."""
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["team_bot_a", "team_bot_c", "security_bot"],
        "bots": {
            "team_bot_a": {"role": "member", "port": 19002, "multiUser": True},
            "team_bot_c": {"role": "member", "port": 19003, "multiUser": True},
            "security_bot": {"role": "member", "port": 19001, "multiUser": False},
        },
        "pod": {"admins": {"external_ids": {}}},
    }
    base.update(overrides)
    p = tmp_path / "network.json"
    p.write_text(json.dumps(base, indent=2))
    return p


@pytest.fixture
def app(tmp_path: Path):
    network_path = _seed_network(tmp_path)
    a = Flask(__name__)
    register_routes(a, network_path)
    a.config["TESTING"] = True
    a.config["_NETWORK_PATH"] = network_path
    return a


# ── GET /api/admin/identity ─────────────────────────────────────────────────


def test_overview_lists_bots_with_multi_user_state(app):
    with app.test_client() as c:
        resp = c.get("/api/admin/identity")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "bots" in data
        assert "team_bot_a" in data["bots"]
        assert data["bots"]["team_bot_a"]["multi_user"] is True
        assert data["bots"]["security_bot"]["multi_user"] is False


def test_overview_flags_missing_admin_and_primary(app):
    """Fresh pod, no admin claimed, no primary recorded → both 'missing'
    entries appear on multi-user bots, none on single-user."""
    with app.test_client() as c:
        data = c.get("/api/admin/identity").get_json()
    assert "admin" in data["bots"]["team_bot_a"]["missing"]
    assert "primary" in data["bots"]["team_bot_a"]["missing"]
    # security_bot is single-user → no missing entries
    assert data["bots"]["security_bot"]["missing"] == []


# ── POST /api/admin/identity/claim-admin ────────────────────────────────────


def test_claim_admin_persists(app):
    with app.test_client() as c:
        resp = c.post("/api/admin/identity/claim-admin", json={
            "channel": "slack",
            "external_id": "U0PLKKXV0",
            "pod_user": "pod_admin_user",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "U0PLKKXV0" in data["admins"]["external_ids"]["slack"]
    # Verify the file got written
    network = json.loads(app.config["_NETWORK_PATH"].read_text())
    assert network["pod"]["admins"]["external_ids"]["slack"] == ["U0PLKKXV0"]
    assert "pod_admin_user" in network["pod"]["admins"]["pod_users"]


def test_claim_admin_idempotent_on_repeat(app):
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-admin", json={
            "channel": "slack", "external_id": "U0PLKKXV0",
        })
        c.post("/api/admin/identity/claim-admin", json={
            "channel": "slack", "external_id": "U0PLKKXV0",
        })
    network = json.loads(app.config["_NETWORK_PATH"].read_text())
    # No duplicate — claim_admin already guards
    assert network["pod"]["admins"]["external_ids"]["slack"] == ["U0PLKKXV0"]


def test_claim_admin_missing_external_id_400(app):
    with app.test_client() as c:
        resp = c.post("/api/admin/identity/claim-admin", json={
            "channel": "slack",
        })
        assert resp.status_code == 400
        assert "external_id" in resp.get_json()["error"]


def test_claim_admin_missing_channel_400(app):
    with app.test_client() as c:
        resp = c.post("/api/admin/identity/claim-admin", json={
            "external_id": "U123",
        })
        assert resp.status_code == 400


# ── POST /api/admin/identity/claim-primary ──────────────────────────────────


def test_claim_primary_persists(app):
    with app.test_client() as c:
        resp = c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a",
            "channel": "slack",
            "external_id": "U0SLACK1",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["primary_user"]["external_ids"]["slack"] == "U0SLACK1"
    network = json.loads(app.config["_NETWORK_PATH"].read_text())
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "U0SLACK1"


def test_claim_primary_overwrite_refused_without_force(app):
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a", "channel": "slack", "external_id": "U_FIRST",
        })
        resp = c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a", "channel": "slack", "external_id": "U_SECOND",
        })
        assert resp.status_code == 400
        assert "force" in resp.get_json()["error"].lower()


def test_claim_primary_overwrite_succeeds_with_force(app):
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a", "channel": "slack", "external_id": "U_FIRST",
        })
        resp = c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a", "channel": "slack",
            "external_id": "U_SECOND", "force": True,
        })
        assert resp.status_code == 200
    network = json.loads(app.config["_NETWORK_PATH"].read_text())
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "U_SECOND"


def test_claim_primary_telegram_also_seeds_allowfrom(app, tmp_path, monkeypatch):
    """Slice 2: Set-owner on telegram ALSO auto-approves the owner's DM.

    Redirect the bot-file writers into tmp_path so the seed's allowFrom +
    auth-profiles writes land where we can assert on them.
    """
    from evolve_admin.web import routes_bot_users as rbu
    import evolve_admin.web.routes_admin_shared as ras

    monkeypatch.setattr(rbu, "bot_home", lambda bot, net=None: tmp_path / "Users" / bot)

    # Pre-seed a telegram bot_token in auth-profiles so the cascade-safety gate
    # in seed_channel_identity allows the chat_id write (AuthProfilesTokenPairProbe
    # is the rightful cascade winner here; co-locating chat_id is safe). Without
    # a token in auth-profiles the gate deliberately skips the chat_id write to
    # avoid blanking a channels-stored token row (see
    # test_claim_primary_telegram_chat_id_skipped_when_token_in_channels).
    ap_store: dict = {
        "security_bot": {
            "profiles": {
                "telegram:token_pair": {
                    "provider": "telegram", "type": "token_pair",
                    "bot_token": "123:SECRET",
                }
            }
        }
    }
    monkeypatch.setattr(ras, "_read_auth_profiles",
                        lambda bot_id, *, network_path: dict(ap_store.get(bot_id, {})))

    def _fake_write(bot_id, data, *, network_path):
        ap_store[bot_id] = {k: v for k, v in data.items() if not k.startswith("_")}
        return True
    monkeypatch.setattr(ras, "_write_auth_profiles", _fake_write)

    with app.test_client() as c:
        resp = c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "security_bot", "channel": "telegram", "external_id": "777999",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["seed_warnings"] == []

    # Owner recorded in network.json.
    net = json.loads(app.config["_NETWORK_PATH"].read_text())
    assert net["bots"]["security_bot"]["primary_user"]["external_ids"]["telegram"] == "777999"

    # DM auto-approved: allowFrom carries the owner's id.
    allow_path = (
        tmp_path / "Users" / "security_bot" / ".openclaw" / "credentials"
        / "telegram-default-allowFrom.json"
    )
    assert allow_path.exists()
    assert "777999" in json.loads(allow_path.read_text())["allowFrom"]

    # chat_id co-located on the existing telegram token_pair auth profile
    # (token preserved).
    tg = ap_store["security_bot"]["profiles"]["telegram:token_pair"]
    assert tg["chat_id"] == "777999"
    assert tg["bot_token"] == "123:SECRET"


def test_claim_primary_telegram_chat_id_skipped_when_token_in_channels(
    app, tmp_path, monkeypatch
):
    """REGRESSION GUARD (route level): when the bot_token is NOT in
    auth-profiles (live pod shape — token in openclaw.json#channels.telegram),
    Set-owner seeds owner + DM approval but SKIPS the chat_id write, so a
    chat_id-only auth-profiles entry can't steal the probe cascade and blank
    the channels-stored token row.
    """
    from evolve_admin.web import routes_bot_users as rbu
    import evolve_admin.web.routes_admin_shared as ras

    monkeypatch.setattr(rbu, "bot_home", lambda bot, net=None: tmp_path / "Users" / bot)

    ap_store: dict = {}  # empty → no token in auth-profiles → gate skips chat_id
    monkeypatch.setattr(ras, "_read_auth_profiles",
                        lambda bot_id, *, network_path: dict(ap_store.get(bot_id, {})))

    def _fake_write(bot_id, data, *, network_path):
        ap_store[bot_id] = {k: v for k, v in data.items() if not k.startswith("_")}
        return True
    monkeypatch.setattr(ras, "_write_auth_profiles", _fake_write)

    with app.test_client() as c:
        resp = c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "security_bot", "channel": "telegram", "external_id": "777999",
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    # Owner recorded + DM approved (functional fix unaffected).
    net = json.loads(app.config["_NETWORK_PATH"].read_text())
    assert net["bots"]["security_bot"]["primary_user"]["external_ids"]["telegram"] == "777999"
    allow_path = (
        tmp_path / "Users" / "security_bot" / ".openclaw" / "credentials"
        / "telegram-default-allowFrom.json"
    )
    assert allow_path.exists()
    assert "777999" in json.loads(allow_path.read_text())["allowFrom"]

    # chat_id write was SKIPPED — no telegram token_pair profile materialized.
    assert "security_bot" not in ap_store or (
        "telegram:token_pair"
        not in ap_store["security_bot"].get("profiles", {})
    )


def test_claim_primary_unknown_bot_400(app):
    with app.test_client() as c:
        resp = c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "phantom",
            "channel": "slack",
            "external_id": "U123",
        })
        assert resp.status_code == 400
        assert "phantom" in resp.get_json()["error"]


def test_overview_reflects_claims_after_persist(app):
    """End-to-end: claim → GET overview shows the claim applied."""
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-admin", json={
            "channel": "slack", "external_id": "U_ADMIN",
        })
        c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a", "channel": "slack", "external_id": "U_PRIMARY",
        })
        data = c.get("/api/admin/identity").get_json()
    assert data["bots"]["team_bot_a"]["missing"] == []  # both resolved
    assert "U_ADMIN" in data["pod_admins"]["external_ids"]["slack"]


# ─────────────────────────────────────────────────────────────────────────────
# Extensions added in PR 2/3 of the user-admin UX work:
#   * names on claim-admin / claim-primary
#   * /revoke-admin endpoint
#   * /set-pod-passphrase + /set-bot-passphrase endpoints
#   * GET overview now includes pod_passphrases + per-bot
#     primary_passphrase_override
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_admin_accepts_name(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/claim-admin", json={
            "channel": "telegram", "external_id": "456",
            "pod_user": "pod_admin_user", "name": "Pod_admin",
        })
        assert r.status_code == 200
        admins = r.get_json()["admins"]
        assert admins["names"] == {"pod_admin_user": "Pod_admin"}


def test_claim_admin_silently_drops_name_without_pod_user(app):
    """Name needs a pod_user to attach to — without pod_user, drop
    the name. Verified at the model layer in test_evo_identity_extensions
    too; here we confirm the route honors the same contract."""
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-admin", json={
            "channel": "telegram", "external_id": "456", "name": "Pod_admin",
        })
        data = c.get("/api/admin/identity").get_json()
    assert data["pod_admins"].get("names", {}) == {}


def test_claim_primary_accepts_name(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a", "channel": "telegram", "external_id": "789",
            "name": "Marcus",
        })
        assert r.status_code == 200
        block = r.get_json()["primary_user"]
        assert block["name"] == "Marcus"


def test_revoke_admin_removes_entry(app):
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-admin", json={
            "channel": "telegram", "external_id": "456",
        })
        r = c.post("/api/admin/identity/revoke-admin", json={
            "channel": "telegram", "external_id": "456",
        })
        assert r.status_code == 200
        assert r.get_json()["removed"] is True
        data = c.get("/api/admin/identity").get_json()
    assert data["pod_admins"]["external_ids"]["telegram"] == []


def test_revoke_admin_with_drop_pod_user_clears_name(app):
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-admin", json={
            "channel": "telegram", "external_id": "456",
            "pod_user": "pod_admin_user", "name": "Pod_admin",
        })
        c.post("/api/admin/identity/revoke-admin", json={
            "channel": "telegram", "external_id": "456",
            "drop_pod_user": "pod_admin_user",
        })
        data = c.get("/api/admin/identity").get_json()
    admins = data["pod_admins"]
    assert admins.get("names", {}) == {}
    assert admins.get("pod_users", []) == []


def test_revoke_admin_idempotent_no_op(app):
    """Calling revoke on a non-existent admin returns ok=true,
    removed=false — operators get a clean 'already gone' signal."""
    with app.test_client() as c:
        r = c.post("/api/admin/identity/revoke-admin", json={
            "channel": "telegram", "external_id": "nope",
        })
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "removed": False}


def test_set_pod_passphrase_admin_persists(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/set-pod-passphrase", json={
            "kind": "admin", "passphrase": "carlyle",
        })
        assert r.status_code == 200
        data = c.get("/api/admin/identity").get_json()
    assert data["pod_passphrases"]["admin"] == "carlyle"


def test_set_pod_passphrase_primary_persists(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/set-pod-passphrase", json={
            "kind": "primary", "passphrase": "newton",
        })
        assert r.status_code == 200
        data = c.get("/api/admin/identity").get_json()
    assert data["pod_passphrases"]["primary"] == "newton"


def test_set_pod_passphrase_rejects_unknown_kind(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/set-pod-passphrase", json={
            "kind": "nope", "passphrase": "x",
        })
    assert r.status_code == 400


def test_set_bot_passphrase_override_persists(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/set-bot-passphrase", json={
            "bot_id": "team_bot_a", "passphrase": "newton",
        })
        assert r.status_code == 200
        data = c.get("/api/admin/identity").get_json()
    assert data["bots"]["team_bot_a"]["primary_passphrase_override"] == "newton"
    # Other bots unaffected.
    assert data["bots"]["team_bot_c"]["primary_passphrase_override"] is None


def test_set_bot_passphrase_clear_with_null(app):
    """Passing null clears the override and the bot inherits the pod
    default. Field is OMITTED from the bot config on clear, not
    written as null."""
    with app.test_client() as c:
        c.post("/api/admin/identity/set-bot-passphrase", json={
            "bot_id": "team_bot_a", "passphrase": "newton",
        })
        c.post("/api/admin/identity/set-bot-passphrase", json={
            "bot_id": "team_bot_a", "passphrase": None,
        })
        data = c.get("/api/admin/identity").get_json()
    assert data["bots"]["team_bot_a"]["primary_passphrase_override"] is None


def test_set_bot_passphrase_rejects_non_member(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/set-bot-passphrase", json={
            "bot_id": "ghost", "passphrase": "newton",
        })
    assert r.status_code == 400


def test_overview_includes_pod_passphrases(app):
    """Default network shows null passphrases until set (this test
    fixture doesn't seed them); set one and verify it surfaces."""
    with app.test_client() as c:
        c.post("/api/admin/identity/set-pod-passphrase", json={
            "kind": "admin", "passphrase": "carlyle",
        })
        data = c.get("/api/admin/identity").get_json()
    assert "pod_passphrases" in data
    assert data["pod_passphrases"]["admin"] == "carlyle"


def test_overview_includes_per_bot_passphrase_override_field(app):
    """Every bot entry has a primary_passphrase_override field
    (null when inheriting). Defends against UI code that would
    crash on undefined access."""
    with app.test_client() as c:
        data = c.get("/api/admin/identity").get_json()
    for bot_id, info in data["bots"].items():
        assert "primary_passphrase_override" in info, bot_id


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/admin/identity — pod_context surfacing
# ─────────────────────────────────────────────────────────────────────────────


def test_overview_includes_pod_context(app):
    """``pod_context`` carries the Unix admin, machine hostname, and
    admin URL. The Identity subtab uses this to render the read-only
    'Pod Context' card at the top so the operator sees the four
    identity concepts in one place."""
    with app.test_client() as c:
        data = c.get("/api/admin/identity").get_json()
    assert "pod_context" in data
    ctx = data["pod_context"]
    assert "admin_user" in ctx
    assert "hostname" in ctx
    assert "admin_base_url" in ctx


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/admin/identity/resolve-name
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_name_requires_channel(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/resolve-name", json={
            "external_id": "1260193629",
        })
    assert r.status_code == 400
    assert r.get_json()["reason"].startswith("channel")


def test_resolve_name_requires_external_id(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/resolve-name", json={
            "channel": "telegram",
        })
    assert r.status_code == 400
    assert r.get_json()["reason"].startswith("external_id")


def test_resolve_name_unsupported_channel(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/resolve-name", json={
            "channel": "whatsapp",  # WhatsApp not yet in SUPPORTED_CHANNELS
            "external_id": "12345",
        })
    # Returns 200 with ok:false — the channel is a valid string,
    # we just don't support that platform yet.
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is False
    assert "supported" in j["reason"].lower()


def test_resolve_name_telegram_success(app, monkeypatch):
    """Stub the resolver's _channel_token + urlopen, then verify the
    route returns the resolved entry."""
    import json as _json
    from evolve_admin.evo import name_resolver

    monkeypatch.setattr(name_resolver, "_channel_token",
                        lambda net, ch: "TOK_TEST" if ch == "telegram" else None)

    class _FakeResp:
        def __init__(self, body):
            self._b = body.encode("utf-8")

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def read(self): return self._b

    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        lambda url, timeout=None: _FakeResp(_json.dumps({
            "ok": True,
            "result": {
                "id": 1260193629, "type": "private",
                "username": "cjalden", "first_name": "Pod_admin",
                "last_name": "Alden",
            },
        })),
    )

    with app.test_client() as c:
        r = c.post("/api/admin/identity/resolve-name", json={
            "channel": "telegram",
            "external_id": "1260193629",
        })
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["resolved"]["username"] == "cjalden"
    assert j["resolved"]["name"] == "Pod_admin Alden"


def test_resolve_name_failure_returns_clear_reason(app, monkeypatch):
    """When the resolver returns None, the route surfaces ok:false +
    a human-readable reason. We use the 'no token configured' path
    because it's the easiest failure mode to exercise without any
    real HTTP call."""
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(name_resolver, "_channel_token", lambda n, c: None)

    with app.test_client() as c:
        r = c.post("/api/admin/identity/resolve-name", json={
            "channel": "telegram",
            "external_id": "1260193629",
        })
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is False
    assert "resolve" in j["reason"].lower()


def test_resolve_name_persists_cache_on_success(app, tmp_path, monkeypatch):
    """A successful resolve should write the cache into network.json
    so subsequent reads (page reloads) see the name without re-fetching."""
    import json as _json
    from evolve_admin.evo import name_resolver

    monkeypatch.setattr(name_resolver, "_channel_token",
                        lambda net, ch: "TOK")

    class _FakeResp:
        def __init__(self, body):
            self._b = body.encode("utf-8")

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def read(self): return self._b

    monkeypatch.setattr(
        name_resolver.urllib.request, "urlopen",
        lambda url, timeout=None: _FakeResp(_json.dumps({
            "ok": True,
            "result": {
                "id": 1, "type": "private",
                "username": "u", "first_name": "User",
            },
        })),
    )

    network_path = app.config["_NETWORK_PATH"]
    with app.test_client() as c:
        c.post("/api/admin/identity/resolve-name", json={
            "channel": "telegram", "external_id": "1",
        })
    persisted = _json.loads(network_path.read_text())
    cache = persisted["pod"]["admins"].get("resolved_names", {})
    assert "telegram:1" in cache
    assert cache["telegram:1"]["username"] == "u"


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/admin/identity/clear-primary
# ─────────────────────────────────────────────────────────────────────────────


def test_clear_primary_removes_block(app):
    """Set a primary, then clear it. Final state has no primary_user."""
    with app.test_client() as c:
        c.post("/api/admin/identity/claim-primary", json={
            "bot_id": "team_bot_a", "channel": "telegram",
            "external_id": "789",
        })
        r = c.post("/api/admin/identity/clear-primary", json={
            "bot_id": "team_bot_a",
        })
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "cleared": True}
        data = c.get("/api/admin/identity").get_json()
    assert "primary_user" not in data["bots"]["team_bot_a"]["primary_user"] \
        or data["bots"]["team_bot_a"]["primary_user"] == {}


def test_clear_primary_idempotent_no_op(app):
    """Bot has no primary recorded → cleared=False with no error."""
    with app.test_client() as c:
        r = c.post("/api/admin/identity/clear-primary", json={
            "bot_id": "team_bot_a",
        })
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "cleared": False}


def test_clear_primary_requires_bot_id(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/clear-primary", json={})
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/admin/identity/discover-primary
# ─────────────────────────────────────────────────────────────────────────────


def test_discover_primary_unknown_bot_400(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/discover-primary", json={
            "bot_id": "no-such-bot",
        })
    assert r.status_code == 400
    assert "unknown" in r.get_json()["error"]


def test_discover_primary_missing_bot_id_400(app):
    with app.test_client() as c:
        r = c.post("/api/admin/identity/discover-primary", json={})
    assert r.status_code == 400


def test_discover_primary_returns_candidates(app, monkeypatch):
    """Stub out the discovery helper + name resolver so we don't need
    real turn files — verifies the route plumbing returns candidates
    with the resolved name shape the UI expects."""
    from evolve_admin.evo import identity_discovery

    def _fake_discover(bot_id, bot_user=None, *, lookback_days, top_k):
        assert bot_id == "team_bot_a"
        return [
            {"channel": "telegram", "external_id": "111",
             "turn_count": 12, "first_seen": "2026-05-01T00:00:00Z",
             "last_seen": "2026-05-19T00:00:00Z"},
        ]

    def _fake_resolve(network, cands):
        # Mimic the production resolver enriching each row.
        out = []
        for c in cands:
            r = dict(c)
            r["username"] = "cjalden"
            r["display_name"] = "Pod_admin Alden"
            out.append(r)
        return out

    monkeypatch.setattr(
        identity_discovery, "discover_candidates", _fake_discover,
    )
    monkeypatch.setattr(
        identity_discovery, "resolve_with_names", _fake_resolve,
    )

    with app.test_client() as c:
        r = c.post("/api/admin/identity/discover-primary", json={
            "bot_id": "team_bot_a", "lookback_days": 30, "top_k": 5,
        })
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert len(j["candidates"]) == 1
    cand = j["candidates"][0]
    assert cand["channel"] == "telegram"
    assert cand["external_id"] == "111"
    assert cand["turn_count"] == 12
    assert cand["username"] == "cjalden"
    assert cand["display_name"] == "Pod_admin Alden"


def test_discover_primary_empty_history_returns_empty_list(app, monkeypatch):
    """No human turn history → candidates=[] (caller falls through to
    manual entry, but the route itself is ok=True)."""
    from evolve_admin.evo import identity_discovery
    monkeypatch.setattr(
        identity_discovery, "discover_candidates",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        identity_discovery, "resolve_with_names",
        lambda net, cands: [],
    )
    with app.test_client() as c:
        r = c.post("/api/admin/identity/discover-primary", json={
            "bot_id": "team_bot_a",
        })
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["candidates"] == []


def test_discover_primary_defaults_when_bad_params(app, monkeypatch):
    """Negative / non-int lookback_days + top_k fall back to defaults
    rather than 400 — the UI button doesn't expose those knobs, so
    we want forgiving behavior for direct API users."""
    from evolve_admin.evo import identity_discovery
    seen = {}

    def _capture(bot_id, bot_user=None, *, lookback_days, top_k):
        seen["lookback_days"] = lookback_days
        seen["top_k"] = top_k
        return []

    monkeypatch.setattr(
        identity_discovery, "discover_candidates", _capture,
    )
    monkeypatch.setattr(
        identity_discovery, "resolve_with_names", lambda n, c: c,
    )

    with app.test_client() as c:
        c.post("/api/admin/identity/discover-primary", json={
            "bot_id": "team_bot_a", "lookback_days": -5, "top_k": 0,
        })
    assert seen["lookback_days"] == 30
    assert seen["top_k"] == 5
