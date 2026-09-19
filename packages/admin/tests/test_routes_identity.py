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

from evolve_admin import external_ids as _external_ids  # noqa: E402
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
        assert data["primary_user"]["external_ids"]["slack"] == ["U0SLACK1"]
    network = json.loads(app.config["_NETWORK_PATH"].read_text())
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U0SLACK1"]


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
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U_SECOND"]


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
    assert net["bots"]["security_bot"]["primary_user"]["external_ids"]["telegram"] == ["777999"]

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
    assert net["bots"]["security_bot"]["primary_user"]["external_ids"]["telegram"] == ["777999"]
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
    # An emptied channel key is dropped, not left as [] (M1-B2) — the
    # reader guarantees "present" and "non-empty" mean the same thing.
    assert "telegram" not in data["pod_admins"]["external_ids"]


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


# ── Capability gate on the identity-MUTATION routes (CBR-1.2 class) ─────────
#
# routes_identity.py had ZERO capability checks. Its mutation routes reach the
# same writes the sibling gates cover, and `claim-admin` reaches the one with
# the largest blast radius of all:
#
#   claim-admin   → pod.admins.external_ids.<ch>   POD-WIDE admin promotion
#   revoke-admin  → pod.admins.external_ids.<ch>   pod-wide removal (lockout)
#   claim-primary → bots.<id>.primary_user  AND  seed_channel_identity, which
#                   auto-approves the id into the bot's allowFrom
#   clear-primary → bots.<id>.primary_user  (un-owns the bot)
#
# `claim-admin` is byte-for-byte the same write as
# routes_pairing._promote_to_pod_admin. That route is NOT gated on main —
# #3643 proposes the gate and was still OPEN when this landed, so nothing
# here assumes it — but claim-admin is strictly easier to reach anyway: no
# pending pairing request, no validate_id() format check, no
# known_channels() membership. And it is not merely a parallel hole — a
# pod-admin claim makes roster_overlay.resolve_role return "admin" (hence
# ["*"]) on EVERY bot, so the ungated route was the key that opens every
# capability gate elsewhere.
#
# None of these paths are in server's _AUTH_EXEMPT_PATHS, so an untrusted TCP
# peer 401s at the device gate — but the evo peer uid on the admin unix socket
# is exempted from that gate by peer_auth.device_gate_trusted_peer() on EVERY
# pod, and on an auth-DISABLED pod _enforce_device_auth returns None for
# everyone. Both reached the mutation header-free with no capability check.
#
# SCOPE OF THE FIX, stated precisely: this closes the UNIX-SOCKET caller on
# every pod, and the unauthenticated-TCP caller on an auth-ENABLED pod. It
# does NOT close header-less TCP on an auth-DISABLED pod — there
# _is_authenticated_ui_request returns True for everyone by design, because
# is_auth_enabled is False (routes_bot_users.py ~line 1431). That is the
# settled inherited semantic (#3435 / #3638) and deliberately not
# re-litigated here; an auth-disabled pod trusts its whole loopback surface,
# which is the operator's explicit opt-out. Read every "_tcp_denied" test
# below as "auth-ENABLED pod, no valid device cookie".
#
# Every deny test asserts the SIDE EFFECTS did not happen as well as the 403 —
# a 403 that already wrote is not a fix.

_SOCKET_ENV = {"REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": 0}

# The id an attacker is trying to install. Never legitimately present.
_ATTACKER = "1260193629"


def _seed_gate_network(tmp_path: Path) -> Path:
    """Pod with two admins (telegram 999, 555) and a bot that already has a
    primary recorded, so every allow-case has something real to mutate and
    every deny-case has a concrete prior value to compare against."""
    return _seed_network(
        tmp_path,
        pod={"admins": {"external_ids": {"telegram": ["999", "555"]}}},
        bots={
            "team_bot_a": {"role": "member", "port": 19002, "multiUser": True},
            "team_bot_c": {
                "role": "member", "port": 19003, "multiUser": True,
                "primary_user": {"name": "Sam Sample",
                                 "external_ids": {"telegram": ["4242"]}},
            },
            "security_bot": {"role": "member", "port": 19001,
                             "multiUser": False},
        },
    )


@pytest.fixture
def gate_app(tmp_path: Path, monkeypatch):
    network_path = _seed_gate_network(tmp_path)
    # claim-primary's seed_channel_identity resolves the bot's allowFrom via
    # routes_bot_users.bot_home; redirect it under tmp_path so the test
    # exercises the REAL write path (no sudo) and the allowFrom assertions
    # below are meaningful rather than vacuously-absent files.
    from evolve_admin.web import routes_bot_users as _rbu
    monkeypatch.setattr(_rbu, "bot_home",
                        lambda bot, net: tmp_path / "Users" / bot)
    a = Flask(__name__)
    register_routes(a, network_path)
    a.config["TESTING"] = True
    # Snapshot AFTER seeding so the whole-file backstop compares against the
    # exact bytes on disk at request time.
    baseline = json.loads(network_path.read_text())
    return a, network_path, baseline


def _allowfrom(tmp_path: Path, bot: str, channel: str = "telegram") -> Path:
    return (tmp_path / "Users" / bot / ".openclaw" / "credentials"
            / f"{channel}-default-allowFrom.json")


# name, path, body, scope ("pod" = _check_pod_admin, "bot" = _check_capability)
_GATED_ROUTES = [
    ("claim-admin", "/api/admin/identity/claim-admin",
     {"channel": "telegram", "external_id": _ATTACKER}, "pod"),
    ("revoke-admin", "/api/admin/identity/revoke-admin",
     {"channel": "telegram", "external_id": "555"}, "pod"),
    ("claim-primary", "/api/admin/identity/claim-primary",
     {"bot_id": "team_bot_a", "channel": "telegram",
      "external_id": _ATTACKER}, "bot"),
    ("clear-primary", "/api/admin/identity/clear-primary",
     {"bot_id": "team_bot_c"}, "bot"),
]
_GATED_IDS = [r[0] for r in _GATED_ROUTES]


def _assert_no_identity_write(network_path: Path, tmp_path: Path,
                              baseline: dict) -> None:
    """Assert a denied identity mutation left every write surface untouched.

    Whole-file equality against the seeded baseline is the backstop — it
    catches ANY network.json write, including a field a future route starts
    touching that the targeted checks below don't name. The targeted checks
    then make a failure read as "the pod-admin list changed" rather than
    "a dict differs somewhere".
    """
    after = json.loads(network_path.read_text())
    assert after == baseline, (
        "denied request mutated network.json (whole-file backstop)")

    # 1. No pod-wide promotion or removal — both seeded admins intact, and
    #    the attacker absent. Read through the tolerant reader, NEVER a raw
    #    .get(channel): external_ids tolerates a legacy SCALAR shape, and
    #    `x not in "8001234567"` is substring containment, not membership —
    #    a raw check would pass or fail for the wrong reason.
    admin_ids = _external_ids.ids_for(after["pod"]["admins"], "telegram")
    assert admin_ids == ["999", "555"], (
        f"pod admin list changed on a denied request: {admin_ids}")
    assert _ATTACKER not in admin_ids

    # 2. No primary_user change on any bot — attacker absent everywhere, and
    #    the pre-existing owner still recorded (clear-primary deny).
    for bot_id, cfg in after["bots"].items():
        assert _ATTACKER not in _external_ids.ids_for(
            cfg.get("primary_user"), "telegram"), (
            f"attacker landed in {bot_id}.primary_user")
    assert _external_ids.ids_for(
        after["bots"]["team_bot_c"].get("primary_user"), "telegram") == [
        "4242"], "team_bot_c lost its recorded primary on a denied request"
    assert after["bots"]["team_bot_c"]["primary_user"]["name"] == "Sam Sample"

    # 3. Not admitted into any bot's OC allowFrom. claim-primary seeds it via
    #    seed_channel_identity; a denied request must never reach that call,
    #    so the file should not exist at all. Tolerate existence (a future
    #    fixture may pre-seed it) but never the attacker inside it.
    for bot_id in after["bots"]:
        ap = _allowfrom(tmp_path, bot_id)
        if ap.exists():
            assert _ATTACKER not in json.loads(
                ap.read_text()).get("allowFrom", []), (
                f"attacker admitted to {bot_id} allowFrom on a denied request")


# ── Deny matrix — one row per gated route ──────────────────────────────────


@pytest.mark.parametrize("_name,path,body,scope", _GATED_ROUTES,
                         ids=_GATED_IDS)
def test_identity_mutation_header_absent_over_socket_denied(
        gate_app, tmp_path, _name, path, body, scope):
    """A bot gateway on the admin unix socket that omits X-Requester-Identity
    is NOT the trusted UI — 403, and no identity write happened.

    This is the PoC: a header-less unix-socket POST to claim-admin returned
    200 and landed the id in pod.admins.external_ids.telegram before the gate.
    """
    a, network_path, baseline = gate_app
    with a.test_client() as c:
        resp = c.post(path, json=body, environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()["error"] == "forbidden"
    assert "no requester identity" in resp.get_json()["detail"]
    _assert_no_identity_write(network_path, tmp_path, baseline)


@pytest.mark.parametrize("_name,path,body,scope", _GATED_ROUTES,
                         ids=_GATED_IDS)
def test_identity_mutation_header_absent_unauthenticated_tcp_denied(
        gate_app, tmp_path, _name, path, body, scope):
    """Header-absent over TCP with device-auth ENFORCED and no valid cookie →
    403. Fail-closed off the socket too when the caller cannot be proven to
    be the authenticated UI."""
    a, network_path, baseline = gate_app
    import evolve_admin.web.admin_auth as _aa
    # conftest disables auth globally via EVOLVE_ADMIN_AUTH_DISABLED=1, so
    # patch the module attributes to model an auth-enabled pod with no
    # paired device.
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: False)
    try:
        with a.test_client() as c:
            resp = c.post(path, json=body)
        assert resp.status_code == 403, resp.get_json()
        assert "no requester identity" in resp.get_json()["detail"]
        _assert_no_identity_write(network_path, tmp_path, baseline)
    finally:
        mp.undo()


@pytest.mark.parametrize("_name,path,body,scope", _GATED_ROUTES,
                         ids=_GATED_IDS)
def test_identity_mutation_malformed_header_denied(
        gate_app, tmp_path, _name, path, body, scope):
    """A PRESENT but unparseable X-Requester-Identity is denied on EVERY
    transport, including the authenticated-UI one — an asserted-but-broken
    identity must never fall through to the trusted-UI path (WO-H1-2)."""
    a, network_path, baseline = gate_app
    with a.test_client() as c:
        resp = c.post(path, json=body,
                      headers={"X-Requester-Identity": "not-a-valid-identity"})
    assert resp.status_code == 403, resp.get_json()
    assert "malformed" in resp.get_json()["detail"]
    _assert_no_identity_write(network_path, tmp_path, baseline)


@pytest.mark.parametrize("_name,path,body,scope", _GATED_ROUTES,
                         ids=_GATED_IDS)
def test_identity_mutation_participant_identity_denied(
        gate_app, tmp_path, _name, path, body, scope):
    """A real, well-formed participant identity over the socket is refused.

    telegram:333 is not a pod admin, has no overlay entry, and is not any
    bot's primary — it resolves to ``participant``, which grants no bot.*
    built-ins and is not the pod-admin tier. So it can neither promote
    itself to pod admin nor mutate a bot's owner.
    """
    a, network_path, baseline = gate_app
    with a.test_client() as c:
        resp = c.post(path, json=body,
                      headers={"X-Requester-Identity": "telegram:333"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403, resp.get_json()
    detail = resp.get_json()["detail"]
    if scope == "pod":
        assert "not a pod admin" in detail
    else:
        assert "bot.roster.mutate" in detail
    _assert_no_identity_write(network_path, tmp_path, baseline)


# ── Allow matrix — the legitimate callers still work ───────────────────────


@pytest.mark.parametrize("_name,path,body,scope", _GATED_ROUTES,
                         ids=_GATED_IDS)
def test_identity_mutation_header_absent_authenticated_ui_allowed(
        gate_app, _name, path, body, scope):
    """Working path: header-absent over the authenticated admin-UI HTTP
    transport (device-auth enforced, VALID device cookie) → 200.

    This is the ONLY production caller of these routes — the Users page in
    the admin SPA (packages/admin/evolve_admin/web/static/js/pages/users.js),
    which never sends X-Requester-Identity. Preserving it is the whole
    compatibility story for this change.
    """
    a, _, _baseline = gate_app
    import evolve_admin.web.admin_auth as _aa
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: True)
    try:
        with a.test_client() as c:
            resp = c.post(path, json=body)
        assert resp.status_code == 200, resp.get_json()
    finally:
        mp.undo()


@pytest.mark.parametrize("_name,path,body,scope", _GATED_ROUTES,
                         ids=_GATED_IDS)
def test_identity_mutation_pod_admin_identity_over_socket_allowed(
        gate_app, _name, path, body, scope):
    """Working path: a gateway presenting a VALID pod-admin identity over the
    socket is still allowed — the socket is not blanket-denied, only the
    header-absent and under-privileged cases flip to deny."""
    a, _, _baseline = gate_app
    with a.test_client() as c:
        resp = c.post(path, json=body,
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()


# ── The writes the deny tests rule out are real (non-vacuity proof) ────────


def test_claim_admin_allowed_actually_promotes(gate_app):
    """The pod-admin promotion the deny tests assert did NOT happen is a real
    write on the allow path — otherwise _assert_no_identity_write would be
    proving nothing."""
    a, network_path, baseline = gate_app
    with a.test_client() as c:
        resp = c.post("/api/admin/identity/claim-admin",
                      json={"channel": "telegram", "external_id": _ATTACKER},
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    net = json.loads(network_path.read_text())
    assert _ATTACKER in _external_ids.ids_for(net["pod"]["admins"], "telegram")


def test_claim_primary_allowed_actually_writes_primary_and_allowfrom(
        gate_app, tmp_path):
    """Same non-vacuity proof for claim-primary's TWO write surfaces:
    bots.<id>.primary_user and the seed_channel_identity allowFrom admission
    that rides along with it."""
    a, network_path, baseline = gate_app
    with a.test_client() as c:
        resp = c.post("/api/admin/identity/claim-primary",
                      json={"bot_id": "team_bot_a", "channel": "telegram",
                            "external_id": _ATTACKER},
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    net = json.loads(network_path.read_text())
    assert _ATTACKER in _external_ids.ids_for(
        net["bots"]["team_bot_a"].get("primary_user"), "telegram")
    ap = _allowfrom(tmp_path, "team_bot_a")
    assert ap.exists(), (
        "claim-primary must seed the allowFrom for the deny-side assertion "
        "to be meaningful — bot_home redirect may have broken")
    assert _ATTACKER in json.loads(ap.read_text())["allowFrom"]


def test_clear_primary_allowed_actually_clears(gate_app):
    """Non-vacuity proof for clear-primary."""
    a, network_path, baseline = gate_app
    with a.test_client() as c:
        resp = c.post("/api/admin/identity/clear-primary",
                      json={"bot_id": "team_bot_c"},
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["cleared"] is True
    net = json.loads(network_path.read_text())
    assert _external_ids.ids_for(
        net["bots"]["team_bot_c"].get("primary_user"), "telegram") == []


def test_revoke_admin_allowed_actually_revokes(gate_app):
    """Non-vacuity proof for revoke-admin."""
    a, network_path, baseline = gate_app
    with a.test_client() as c:
        resp = c.post("/api/admin/identity/revoke-admin",
                      json={"channel": "telegram", "external_id": "555"},
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["removed"] is True
    net = json.loads(network_path.read_text())
    assert _external_ids.ids_for(
        net["pod"]["admins"], "telegram") == ["999"]


# ── Ordering + bootstrap ───────────────────────────────────────────────────


def test_gate_runs_after_body_validation(gate_app):
    """Validation-before-403 parity with every sibling handler: a malformed
    body answers 400 even for a caller the gate would deny. (routes_identity
    has no 404 branch — an unknown bot surfaces as a 400 ClaimError from the
    identity writer — so 400 is the ordering invariant here.)

    Asserted as a PAIR from the SAME denied caller. The 400 alone would pass
    on pre-gate code too (it 400s for everyone), proving nothing about
    ordering; it is the contrast — same caller, same transport, only the body
    differs → 400 vs 403 — that pins the gate as sitting after validation
    rather than before it or nowhere at all.
    """
    a, _, _baseline = gate_app
    with a.test_client() as c:
        bad_body = c.post("/api/admin/identity/claim-admin",
                          json={"channel": "telegram"},  # no external_id
                          environ_overrides=_SOCKET_ENV)
        good_body = c.post("/api/admin/identity/claim-admin",
                           json={"channel": "telegram",
                                 "external_id": _ATTACKER},
                           environ_overrides=_SOCKET_ENV)
    assert bad_body.status_code == 400
    assert "external_id" in bad_body.get_json()["error"]
    assert good_body.status_code == 403


def test_fresh_pod_bootstrap_still_works_from_the_ui(tmp_path, monkeypatch):
    """The bootstrap case the pod-scoped gate could plausibly have bricked:
    a FRESH pod with ZERO pod admins, where "requester must already be a pod
    admin" has nobody to admit.

    It still works, because the admin UI sends no X-Requester-Identity and is
    trusted by the same header-absent rule _check_capability applies. (The
    other two first-admin paths — the DM `evo claim <passphrase>` flow in
    evo.dispatch and the setup wizard in evo.wizard.engine — call
    evo.identity.claim_admin IN-PROCESS behind the pod admin passphrase and
    never touch this route at all.)
    """
    network_path = _seed_network(tmp_path)  # pod.admins.external_ids == {}
    from evolve_admin.web import routes_bot_users as _rbu
    monkeypatch.setattr(_rbu, "bot_home",
                        lambda bot, net: tmp_path / "Users" / bot)
    a = Flask(__name__)
    register_routes(a, network_path)
    a.config["TESTING"] = True

    import evolve_admin.web.admin_auth as _aa
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: True)
    try:
        with a.test_client() as c:
            resp = c.post("/api/admin/identity/claim-admin",
                          json={"channel": "telegram",
                                "external_id": "1111"})
        assert resp.status_code == 200, resp.get_json()
    finally:
        mp.undo()
    net = json.loads(network_path.read_text())
    assert "1111" in _external_ids.ids_for(net["pod"]["admins"], "telegram")


def test_fresh_pod_bootstrap_still_denied_from_the_socket(tmp_path):
    """The other half of the bootstrap story: a fresh pod does NOT become an
    open door. With zero pod admins, a header-less socket caller is still
    denied — "no admins yet" must not degrade to "anyone may claim"."""
    network_path = _seed_network(tmp_path)
    a = Flask(__name__)
    register_routes(a, network_path)
    a.config["TESTING"] = True
    with a.test_client() as c:
        resp = c.post("/api/admin/identity/claim-admin",
                      json={"channel": "telegram", "external_id": _ATTACKER},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403
    net = json.loads(network_path.read_text())
    assert _external_ids.ids_for(net["pod"]["admins"], "telegram") == []


# ── Explicitly NOT gated (reported, not fixed — see the PR body) ───────────


def test_identity_reads_remain_ungated(gate_app):
    """Scope boundary: GET /api/admin/identity is a READ and is deliberately
    left ungated by this change (reported, not fixed — see the PR body).
    resolve-name and discover-primary are likewise reads: they write only the
    resolved_names cache, never pod.admins / primary_user / allowFrom.

    Asserted so a future edit that silently widens THIS change's scope has to
    do so deliberately. It is a statement about today's boundary, not a claim
    that leaving them ungated is correct forever.
    """
    a, _, _baseline = gate_app
    with a.test_client() as c:
        assert c.get("/api/admin/identity",
                     environ_overrides=_SOCKET_ENV).status_code == 200


def test_passphrase_routes_are_a_known_ungated_pivot(gate_app):
    """set-pod-passphrase is NOT gated by this change — and that is a KNOWN
    HOLE, not a design endorsement. Documented here rather than silently left
    out, because it is a one-hop pivot around the claim-admin gate:

        POST set-pod-passphrase {"kind":"admin","passphrase":"X"}
          → writes pod.admin_passphrase (evo/identity.py set_pod_admin_passphrase)
          → matches_admin_passphrase(net, "X") is now True
          → `evo claim X` over DM reaches evo/dispatch.py's in-process
            claim_admin with no capability check → attacker is a pod admin.

    So the passphrase routes are the identity-mutation class with one extra
    hop, and gating claim-admin without them is incomplete. They are out of
    THIS change's declared scope (a credentials-class chip owns them); this
    test exists so the pivot is discoverable and so whoever gates them finds
    a test that documents the hole rather than one asserting 200 forever.

    Deliberately asserts only that the route is REACHED (not 404) — it does
    not pin the ungated 200, so the chip that gates it changes this test's
    expectation rather than deleting a passing assertion.
    """
    a, _, _baseline = gate_app
    with a.test_client() as c:
        resp = c.post("/api/admin/identity/set-pod-passphrase",
                      json={"kind": "admin", "passphrase": "hunter2"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code != 404, "route disappeared; update this note"
    assert resp.status_code in (200, 403), resp.status_code


def test_blocked_pod_admin_cannot_mint_a_fresh_admin(gate_app, tmp_path):
    """The sticky block is honored at pod scope.

    roster_overlay.resolve_role checks is_blocked BEFORE the pod-admin
    branch, so a blocked identity gets no capabilities on that bot. The
    pod-scoped helper has no single overlay, so it checks every bot's.

    Without this, blocking a rogue pod admin would be trivially escapable:
    telegram:999 is refused by the bot-scoped claim-primary gate but would
    still pass claim-admin, minting a second UNBLOCKED pod-admin id and
    walking straight back in on every bot — leaving the pod-scoped gate
    strictly LOOSER than the per-bot one it is modeled on.
    """
    from evolve_admin import roster_overlay as _ro

    a, network_path, baseline = gate_app
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    for bot_id in ("team_bot_a", "team_bot_c", "security_bot"):
        ov = _ro.load_overlay(shared, bot_id)
        _ro.block_identity(ov, "telegram", "999", reason="rogue",
                           by="ui:admin")
        _ro.save_overlay(shared, bot_id, ov)

    with a.test_client() as c:
        # Baseline: the bot-scoped gate already refuses a blocked identity.
        denied_bot = c.post(
            "/api/admin/identity/claim-primary",
            json={"bot_id": "team_bot_a", "channel": "telegram",
                  "external_id": _ATTACKER},
            headers={"X-Requester-Identity": "telegram:999"},
            environ_overrides=_SOCKET_ENV)
        assert denied_bot.status_code == 403
        # The fix: the pod-scoped gate must refuse it too.
        denied_pod = c.post(
            "/api/admin/identity/claim-admin",
            json={"channel": "telegram", "external_id": _ATTACKER},
            headers={"X-Requester-Identity": "telegram:999"},
            environ_overrides=_SOCKET_ENV)
    assert denied_pod.status_code == 403, denied_pod.get_json()
    assert "blocked" in denied_pod.get_json()["detail"]
    _assert_no_identity_write(network_path, tmp_path, baseline)
