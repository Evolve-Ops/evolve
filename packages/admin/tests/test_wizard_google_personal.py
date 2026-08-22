"""tests/test_wizard_google_personal.py — Phase A.2 wizard route tests.

Spec: [docs/spec-google-path-a-2026-06-01.md](../../../docs/spec-google-path-a-2026-06-01.md).

Covers:

  GET  /api/wizard/google/scopes-by-path?path=A|C
  GET  /api/wizard/google/personal/state?bot=<id>
  POST /api/wizard/google/personal/finalize
  POST /api/wizard/google/personal/reconsent

The OC profile reader and Path-A preflight are injected as stub
callables via ``register_google_personal_wizard_routes`` so tests
don't need a real bot user / OAuth callback.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import wizard_google_personal_routes as wgp  # noqa: E402
from evolve_admin.web.wizard_google_personal_routes import (  # noqa: E402
    filter_catalog_by_path,
    mirror_oc_profile_to_canonical_store,
    register_google_personal_wizard_routes,
    _validate_finalize_payload,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "googleOAuthClient": {
            "client_id": "pod-client.apps.googleusercontent.com",
            "client_secret": "pod-secret",
        },
        "bots": {
            "evo": {"role": "primary", "display_name": "Evo",
                    "primary_user": {"name": "Sam"}},
            "ada": {"role": "member", "display_name": "Ada",
                    "primary_user": {"name": "Sam"}},
        },
    }))
    return p


@pytest.fixture
def tokens_dir(tmp_path: Path) -> Path:
    d = tmp_path / "google_oauth_tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _oc_profile(
    *,
    refresh_token: str = "1//0gT_refresh",
    access_token: str = "ya29.access",
    google_account: str = "ada.smith@gmail.com",
    scopes: list[str] | None = None,
    expires_epoch: float = 1781234567.0,  # arbitrary deterministic value
    issued_epoch: float = 1781234000.0,
    status: str = "active",
) -> dict:
    if scopes is None:
        scopes = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ]
    return {
        "provider": "google_workspace",
        "type": "oauth",
        "google_account": google_account,
        "scopes": scopes,
        "services": ["gmail", "gmail_readonly"],
        "access_token": access_token,
        "access_token_expires_at": expires_epoch,
        "refresh_token": refresh_token,
        "issued_at": issued_epoch,
        "status": status,
    }


@pytest.fixture
def app_factory(network_path: Path, tokens_dir: Path):
    """Build a Flask app with stub readers, parameterized per test."""

    def _build(*, oc_profile=None, preflight=None) -> Flask:
        flask_app = Flask(__name__)
        register_google_personal_wizard_routes(
            flask_app,
            network_path,
            tokens_dir=tokens_dir,
            read_oc_profile=lambda bot_id: oc_profile,
            preflight_fn=preflight or (lambda bot_id, td: {"ok": True, "profile": {}}),
        )
        return flask_app

    return _build


# ── filter_catalog_by_path ──────────────────────────────────────────────────


def test_filter_by_path_A_drops_restricted():
    """Path-A excludes gmail.modify + drive (unverified consent can't grant)."""
    from evolve_admin.web.wizard_google_routes import SCOPE_CATALOG
    filtered = filter_catalog_by_path(SCOPE_CATALOG, "A")
    ids = {e["id"] for e in filtered}
    assert "https://www.googleapis.com/auth/gmail.send" in ids
    assert "https://www.googleapis.com/auth/gmail.readonly" in ids
    assert "https://www.googleapis.com/auth/drive.file" in ids
    assert "https://www.googleapis.com/auth/gmail.modify" not in ids
    assert "https://www.googleapis.com/auth/drive" not in ids


def test_filter_by_path_C_includes_all():
    from evolve_admin.web.wizard_google_routes import SCOPE_CATALOG
    filtered = filter_catalog_by_path(SCOPE_CATALOG, "C")
    assert len(filtered) == len(SCOPE_CATALOG)


def test_filter_no_path_returns_full_catalog():
    from evolve_admin.web.wizard_google_routes import SCOPE_CATALOG
    filtered = filter_catalog_by_path(SCOPE_CATALOG, None)
    assert len(filtered) == len(SCOPE_CATALOG)


# ── GET /scopes-by-path ─────────────────────────────────────────────────────


def test_scopes_by_path_a(app_factory):
    client = app_factory().test_client()
    r = client.get("/api/wizard/google/scopes-by-path?path=A")
    assert r.status_code == 200
    data = r.get_json()
    assert data["path"] == "A"
    ids = {s["id"] for s in data["scopes"]}
    assert "https://www.googleapis.com/auth/gmail.send" in ids
    assert "https://www.googleapis.com/auth/gmail.modify" not in ids
    # default_set defaults include gmail.send (per existing SCOPE_CATALOG)
    assert "https://www.googleapis.com/auth/gmail.send" in data["default_scopes"]


def test_scopes_by_path_c(app_factory):
    client = app_factory().test_client()
    r = client.get("/api/wizard/google/scopes-by-path?path=C")
    data = r.get_json()
    ids = {s["id"] for s in data["scopes"]}
    assert "https://www.googleapis.com/auth/gmail.modify" in ids
    assert "https://www.googleapis.com/auth/drive" in ids


# ── GET /personal/state ─────────────────────────────────────────────────────


def test_state_unknown_bot(app_factory):
    client = app_factory().test_client()
    r = client.get("/api/wizard/google/personal/state?bot=nope")
    assert r.status_code == 404
    assert "not in network.json" in r.get_json()["error"]


def test_state_missing_bot_param(app_factory):
    client = app_factory().test_client()
    r = client.get("/api/wizard/google/personal/state")
    assert r.status_code == 400


def test_state_bot_with_no_config(app_factory):
    client = app_factory().test_client()
    r = client.get("/api/wizard/google/personal/state?bot=ada")
    data = r.get_json()
    assert data["configured"] is False
    assert data["mode"] is None
    assert data["canonical_token_present"] is False


def test_state_bot_after_finalize(app_factory, network_path: Path, tokens_dir: Path):
    """State reflects on-disk reality after finalize writes both stores."""
    # Pre-populate
    (tokens_dir / "ada.json").write_text(json.dumps({
        "refresh_token": "x", "access_token": "y",
        "scopes_granted": ["https://www.googleapis.com/auth/gmail.send"],
        "google_account": "ada.smith@gmail.com",
    }))
    network = json.loads(network_path.read_text())
    network["bots"]["ada"]["google_integration"] = {
        "mode": "free_gmail_oauth",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "consent_screen_state": "testing",
    }
    network_path.write_text(json.dumps(network))

    client = app_factory().test_client()
    r = client.get("/api/wizard/google/personal/state?bot=ada")
    data = r.get_json()
    assert data["configured"] is True
    assert data["mode"] == "free_gmail_oauth"
    assert data["canonical_token_present"] is True
    assert data["google_account"] == "ada.smith@gmail.com"


# ── POST /personal/finalize ─────────────────────────────────────────────────


def test_finalize_missing_bot_id(app_factory):
    client = app_factory().test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={})
    assert r.status_code == 400
    assert "bot_id is required" in r.get_json()["errors"][0]


def test_finalize_unknown_bot(app_factory):
    client = app_factory().test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "nope",
        "consent_screen_state": "testing",
    })
    assert r.status_code == 400
    assert "not in network.json" in r.get_json()["errors"][0]


def test_finalize_no_oc_profile(app_factory):
    """Operator hits finalize before completing OAuth → clear error."""
    client = app_factory(oc_profile=None).test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada",
        "consent_screen_state": "testing",
    })
    assert r.status_code == 400
    assert "no OAuth profile found" in r.get_json()["errors"][0]


def test_finalize_oc_profile_in_reauth_required(app_factory):
    """An OC profile in reauth_required state is not finalizable."""
    client = app_factory(
        oc_profile=_oc_profile(status="reauth_required"),
    ).test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada",
        "consent_screen_state": "testing",
    })
    assert r.status_code == 400
    assert "reauth_required" in r.get_json()["errors"][0]


def test_finalize_oc_profile_missing_refresh_token(app_factory):
    """OAuth without offline access (no refresh_token) → error."""
    bad = _oc_profile()
    bad["refresh_token"] = ""
    client = app_factory(oc_profile=bad).test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada",
        "consent_screen_state": "testing",
    })
    assert r.status_code == 400
    assert "no refresh_token" in r.get_json()["errors"][0]


def test_finalize_invalid_consent_state(app_factory):
    client = app_factory(oc_profile=_oc_profile()).test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada",
        "consent_screen_state": "magic",
    })
    assert r.status_code == 400
    assert "consent_screen_state" in r.get_json()["errors"][0]


def test_finalize_happy_path_mirrors_token_and_writes_config(
    app_factory, network_path: Path, tokens_dir: Path,
):
    """The full happy path: OC profile → canonical token + google_integration."""
    preflight_calls: list[tuple] = []

    def _pf(bot_id: str, td: Path) -> dict:
        preflight_calls.append((bot_id, td))
        return {"ok": True, "profile": {"emailAddress": "ada.smith@gmail.com"}}

    profile = _oc_profile(
        refresh_token="1//REFRESH_ADA",
        access_token="ya29.ADA",
        scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ],
    )
    client = app_factory(oc_profile=profile, preflight=_pf).test_client()

    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada",
        "consent_screen_state": "testing",
        "reauth_contact": {
            "channel": "telegram",
            "user_external_id": "12345",
        },
    })
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["ok"] is True
    assert data["token_mirrored"] is True
    assert data["preflight"]["ok"] is True

    # 1. Canonical token store written
    token_file = tokens_dir / "ada.json"
    assert token_file.exists()
    assert (os.stat(token_file).st_mode & 0o777) == 0o600
    record = json.loads(token_file.read_text())
    assert record["refresh_token"] == "1//REFRESH_ADA"
    assert record["access_token"] == "ya29.ADA"
    assert record["google_account"] == "ada.smith@gmail.com"
    assert "https://www.googleapis.com/auth/gmail.send" in record["scopes_granted"]

    # 2. network.json google_integration block written
    network = json.loads(network_path.read_text())
    gi = network["bots"]["ada"]["google_integration"]
    assert gi["mode"] == "free_gmail_oauth"
    assert gi["consent_screen_state"] == "testing"
    assert gi["reauth_contact"]["channel"] == "telegram"
    assert gi["reauth_contact"]["user_external_id"] == "12345"
    assert "https://www.googleapis.com/auth/gmail.send" in gi["scopes"]
    assert gi["oauth_client_secret_ref"] == "google-oauth-client-pod"
    assert gi["oauth_token_secret_ref"] == "ada"

    # 3. Preflight was called with bot_id + tokens_dir
    assert preflight_calls == [("ada", tokens_dir)]


def test_finalize_preflight_failure_still_writes_config(
    app_factory, network_path: Path, tokens_dir: Path,
):
    """A preflight failure does NOT roll back the config write.

    Spec §2.1 step 10: failed preflight surfaces an operator hint;
    they fix and re-verify. Rollback would lose the consented token.
    """
    profile = _oc_profile()
    client = app_factory(
        oc_profile=profile,
        preflight=lambda bot_id, td: {"ok": False, "error": "bad scope"},
    ).test_client()

    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada",
        "consent_screen_state": "testing",
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["preflight"]["ok"] is False
    # Config still landed
    network = json.loads(network_path.read_text())
    assert network["bots"]["ada"]["google_integration"]["mode"] == "free_gmail_oauth"


# ── _default_read_oc_profile (production reader) ─────────────────────────────
#
# Regression for the false-success incident (2026-06-21, bot atlas): consent
# succeeded and the credential WAS written to disk, but finalize reported "no
# OAuth profile found" because the bespoke reader (a) looked in the wrong
# directory and (b) used the wrong profile-id key. These tests pin the reader
# to the SAME discovery + key convention the OAuth callback writes with.
# Existing finalize tests inject a stub reader, so they never exercised this.


def test_default_reader_uses_canonical_underscore_key(monkeypatch, network_path: Path):
    """The reader must look up server._google_oauth_profile_id →
    'google_workspace_<bot>' (underscore) — the key the callback writes — and
    delegate discovery to the shared _read_auth_profiles (which finds the
    agent-dir location). A colon-keyed decoy must be ignored."""
    from evolve_admin.web import routes_admin_shared as ras

    captured: dict = {}

    def _fake_read(bot_id, *, network_path):
        captured["bot_id"] = bot_id
        return {"profiles": {
            "google_workspace_ada": {"refresh_token": "r", "google_account": "ada@gmail.com"},
            "google_workspace:ada": {"refresh_token": "WRONG_COLON_KEY"},
        }}

    monkeypatch.setattr(ras, "_read_auth_profiles", _fake_read)
    prof = wgp._default_read_oc_profile("ada", network_path=network_path)
    assert prof is not None
    assert prof["refresh_token"] == "r"
    assert prof["google_account"] == "ada@gmail.com"
    assert captured["bot_id"] == "ada"  # delegated to the shared reader


def test_default_reader_falls_back_to_bare_legacy_key(monkeypatch, network_path: Path):
    from evolve_admin.web import routes_admin_shared as ras
    monkeypatch.setattr(
        ras, "_read_auth_profiles",
        lambda bot_id, *, network_path: {"profiles": {"google_workspace": {"refresh_token": "legacy"}}},
    )
    prof = wgp._default_read_oc_profile("ada", network_path=network_path)
    assert prof is not None and prof["refresh_token"] == "legacy"


def test_default_reader_none_when_no_profiles(monkeypatch, network_path: Path):
    from evolve_admin.web import routes_admin_shared as ras
    monkeypatch.setattr(ras, "_read_auth_profiles", lambda bot_id, *, network_path: {})
    assert wgp._default_read_oc_profile("ada", network_path=network_path) is None


def test_default_reader_ignores_colon_only_key(monkeypatch, network_path: Path):
    """If ONLY the wrong colon-form key is present, the reader returns None —
    it never silently reads the wrong convention (this was the live shape)."""
    from evolve_admin.web import routes_admin_shared as ras
    monkeypatch.setattr(
        ras, "_read_auth_profiles",
        lambda bot_id, *, network_path: {"profiles": {"google_workspace:ada": {"refresh_token": "x"}}},
    )
    assert wgp._default_read_oc_profile("ada", network_path=network_path) is None


def test_finalize_with_real_reader_persists_both_stores(
    monkeypatch, network_path: Path, tokens_dir: Path,
):
    """End-to-end through the PRODUCTION reader (no injected stub): a profile
    stored under the canonical underscore key is found, so finalize mirrors
    the canonical token + writes google_integration. This is the chain that
    silently failed live for atlas."""
    from evolve_admin.web import routes_admin_shared as ras
    monkeypatch.setattr(
        ras, "_read_auth_profiles",
        lambda bot_id, *, network_path: {"profiles": {
            "google_workspace_ada": _oc_profile(
                refresh_token="1//REAL_ADA", google_account="ada@gmail.com"),
        }},
    )

    flask_app = Flask("gws_real_reader")
    register_google_personal_wizard_routes(
        flask_app, network_path, tokens_dir=tokens_dir,
        preflight_fn=lambda bot_id, td: {"ok": True, "profile": {}},
    )
    client = flask_app.test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada", "consent_screen_state": "testing"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True and body["token_mirrored"] is True

    record = json.loads((tokens_dir / "ada.json").read_text())
    assert record["refresh_token"] == "1//REAL_ADA"
    network = json.loads(network_path.read_text())
    assert network["bots"]["ada"]["google_integration"]["mode"] == "free_gmail_oauth"


def test_finalize_with_real_reader_fails_on_colon_key(
    monkeypatch, network_path: Path, tokens_dir: Path,
):
    """The failure half of the success-contract: a profile stored under ONLY
    the wrong colon key is NOT found, so finalize returns a clean error and
    persists NOTHING (the frontend renders this as red, not green)."""
    from evolve_admin.web import routes_admin_shared as ras
    monkeypatch.setattr(
        ras, "_read_auth_profiles",
        lambda bot_id, *, network_path: {"profiles": {
            "google_workspace:ada": _oc_profile(refresh_token="1//WRONG"),
        }},
    )

    flask_app = Flask("gws_real_reader_fail")
    register_google_personal_wizard_routes(
        flask_app, network_path, tokens_dir=tokens_dir,
    )
    client = flask_app.test_client()
    r = client.post("/api/wizard/google/personal/finalize", json={
        "bot_id": "ada", "consent_screen_state": "testing"})
    assert r.status_code == 400
    assert "no OAuth profile found" in r.get_json()["errors"][0]
    assert not (tokens_dir / "ada.json").exists()  # nothing persisted
    network = json.loads(network_path.read_text())
    assert "google_integration" not in network["bots"]["ada"]


def test_default_reader_reads_agent_dir_file_over_wrong_dir(
    monkeypatch, network_path: Path, tmp_path: Path,
):
    """Filesystem-level: drive the REAL _read_auth_profiles over actual files.

    Closes the loop on the *location* half of the incident. The OAuth
    callback writes to the AGENT dir
    (``~/.openclaw/agents/main/agent/auth-profiles.json``); the old reader
    looked at ``~/.openclaw/auth-profiles.json`` (a decoy here with the
    WRONG profile). With the agent-dir path first in discovery (as
    _find_auth_profile_paths orders it), the reader must return the
    agent-dir profile under the underscore key — read from a real file, via
    the real normalization + selection — and never the wrong-dir decoy.
    """
    oc = tmp_path / "ada" / ".openclaw"
    agent_dir = oc / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / "auth-profiles.json"
    wrong_file = oc / "auth-profiles.json"
    agent_file.write_text(json.dumps({"profiles": {
        "google_workspace_ada": _oc_profile(refresh_token="AGENT_DIR_RIGHT"),
    }}))
    wrong_file.write_text(json.dumps({"profiles": {
        "google_workspace_ada": _oc_profile(refresh_token="DECOY_WRONG_DIR"),
    }}))

    from evolve_admin.web import routes_admin_shared as ras
    # Real _find ordering: agent-dir (resolve_bot_paths result) first.
    monkeypatch.setattr(
        ras, "_find_auth_profile_paths",
        lambda bot_id, *, network_path: [str(agent_file), str(wrong_file)],
    )

    prof = wgp._default_read_oc_profile("ada", network_path=network_path)
    assert prof is not None
    assert prof["refresh_token"] == "AGENT_DIR_RIGHT"  # agent dir wins, not decoy


# ── mirror_oc_profile_to_canonical_store ────────────────────────────────────


def test_mirror_translates_epoch_to_iso(tmp_path: Path):
    """OC stores expires_at as float epoch; canonical record stores ISO-8601."""
    profile = _oc_profile(
        expires_epoch=1781234567.0,
        issued_epoch=1781234000.0,
    )
    record = mirror_oc_profile_to_canonical_store("ada", profile, tmp_path)
    assert "T" in record["access_token_expires_at"]  # ISO-8601 marker
    assert record["access_token_expires_at"].endswith("Z")
    assert record["scopes_granted"] == profile["scopes"]


def test_mirror_handles_missing_expiry(tmp_path: Path):
    """No expiry → record a near-future expiry to force refresh on first use."""
    profile = _oc_profile()
    profile["access_token_expires_at"] = 0
    record = mirror_oc_profile_to_canonical_store("ada", profile, tmp_path)
    # Just check the field is populated; the value triggers refresh on load.
    assert record["access_token_expires_at"].endswith("Z")


# ── POST /personal/reconsent ────────────────────────────────────────────────


def test_reconsent_builds_authorize_url_preserving_scopes(
    app_factory, network_path: Path,
):
    network = json.loads(network_path.read_text())
    network["bots"]["ada"]["google_integration"] = {
        "mode": "free_gmail_oauth",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar",
        ],
    }
    network_path.write_text(json.dumps(network))

    client = app_factory().test_client()
    r = client.post("/api/wizard/google/personal/reconsent", json={
        "bot_id": "ada",
        "additional_services": [
            "https://www.googleapis.com/auth/drive.file",
        ],
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert "accounts.google.com" in data["authorize_url"]
    # The URL must include all 3 scopes (2 existing + 1 new)
    assert "gmail.send" in data["authorize_url"]
    assert "calendar" in data["authorize_url"]
    assert "drive.file" in data["authorize_url"]


def test_reconsent_no_client_configured(app_factory, network_path: Path):
    network = json.loads(network_path.read_text())
    del network["googleOAuthClient"]
    network_path.write_text(json.dumps(network))

    client = app_factory().test_client()
    r = client.post("/api/wizard/google/personal/reconsent", json={
        "bot_id": "ada",
        "additional_services": ["https://www.googleapis.com/auth/gmail.send"],
    })
    assert r.status_code == 412
    assert "not_configured" in r.get_json()["error"]


def test_reconsent_resolves_store_backed_client(app_factory, network_path: Path):
    """G7: reconsent resolves a store-backed client (no inline secret).

    Before G7 the reconsent helper read only inline network.json secrets, so a
    bot configured the canonical way (network holds {client_id, secret_bot};
    secret in the credential store) wrongly 412'd "client not configured" — the
    same defect that broke token refresh for live atlas.
    """
    network = json.loads(network_path.read_text())
    network.pop("googleOAuthClient", None)  # no pod-level inline client
    network["bots"]["ada"]["googleOAuthClient"] = {
        "mode": "self_hosted",
        "client_id": "ada-client.apps.googleusercontent.com",
        "secret_bot": "ada",
    }
    network["bots"]["ada"]["google_integration"] = {
        "mode": "free_gmail_oauth",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
    }
    network_path.write_text(json.dumps(network))
    # The secret the configure step writes to the Evolve credential store.
    store = Path(network["sharedDir"]) / "credentials" / "ada" / "google_oauth_client.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({
        "schema_version": 1,
        "client_id": "ada-client.apps.googleusercontent.com",
        "client_secret": "store-secret",
    }))

    client = app_factory().test_client()
    r = client.post("/api/wizard/google/personal/reconsent", json={"bot_id": "ada"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert "ada-client.apps.googleusercontent.com" in data["authorize_url"]
    assert "gmail.send" in data["authorize_url"]


def test_reconsent_no_scopes_to_reconsent(app_factory, network_path: Path):
    """Bot with no existing scopes + no additional → clean error, not URL."""
    client = app_factory().test_client()
    r = client.post("/api/wizard/google/personal/reconsent", json={
        "bot_id": "ada",
    })
    assert r.status_code == 400
    assert "no_scopes_to_reconsent" in r.get_json()["error"]


# ── _validate_finalize_payload edge cases ───────────────────────────────────


def test_validate_finalize_default_consent_state():
    errors, cleaned = _validate_finalize_payload({"bot_id": "ada"})
    assert errors == []
    assert cleaned["consent_screen_state"] == "testing"  # default


def test_validate_finalize_dropped_empty_contact():
    errors, cleaned = _validate_finalize_payload({
        "bot_id": "ada",
        "reauth_contact": {"channel": "", "user_external_id": ""},
    })
    assert errors == []
    assert cleaned["reauth_contact"] == {}


def test_validate_finalize_partial_contact():
    """Channel set, user_external_id missing → preserve what's there."""
    errors, cleaned = _validate_finalize_payload({
        "bot_id": "ada",
        "reauth_contact": {"channel": "telegram"},
    })
    assert errors == []
    assert cleaned["reauth_contact"] == {"channel": "telegram"}
