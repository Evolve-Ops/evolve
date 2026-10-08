"""Tests for the tracked-repos API surface (Phase 3 of Issue Inbox).

Endpoints under test:
  GET    /api/inbox/repos                 list with per-target permission
  POST   /api/inbox/repos                 add a target
  DELETE /api/inbox/repos/<target_name>   remove a target

Strategy: stub the keystore + the permissions transport so tests don't
hit the network or write real secrets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin.intake import permissions as perms  # noqa: E402


@pytest.fixture(autouse=True)
def clear_perm_caches():
    perms.clear_caches()
    yield
    perms.clear_caches()


@pytest.fixture
def repo_app(tmp_path):
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "intake": {
            "github": {
                "default": "evolve",
                "targets": {
                    "evolve": {
                        "owner": "evolve-ops", "repo": "evolve",
                        "token_slot": "github_intake",
                    },
                    "openclaw": {
                        "owner": "openclaw", "repo": "openclaw",
                        "token_slot": "github_intake_openclaw",
                    },
                },
            },
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Stub the keystore so the routes don't crash trying to read a real
    # keystore file. Two slots → two different tokens.
    class _StubKeystore:
        def get_value(self, slot):
            return {
                "github_intake": "token-for-evolve",
                "github_intake_openclaw": "token-for-openclaw",
            }.get(slot)

    class _StubMgr:
        def __init__(self, *a, **kw):
            self.ks = _StubKeystore()
            self._values = _StubKeystore()
        def get_value(self, slot):
            return self._values.get_value(slot)

    # Stub the permissions transport to return a deterministic map.
    def _stub_tx(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "cjalden"}
        if url.endswith("/repos/evolve-ops/evolve/collaborators/cjalden/permission"):
            return 200, {"permission": "admin"}
        if url.endswith("/repos/openclaw/openclaw/collaborators/cjalden/permission"):
            return 404, {"message": "Not a collaborator"}
        return 500, {"error": f"unstubbed: {url}"}

    # The route does ``from ..keystore import KeystoreManager`` lazily;
    # patch it at the module path the route imports from.
    with patch("evolve_admin.keystore.KeystoreManager", _StubMgr), \
         patch.object(perms, "_default_transport", _stub_tx):
        app = Flask(__name__)
        app.config["TESTING"] = True
        register_evo_routes(app, network_path)
        app.config["_NETWORK_PATH"] = network_path
        app.config["_SHARED_DIR"] = tmp_path
        yield app


def _read_network(path):
    return json.loads(Path(path).read_text())


# ─── GET ──────────────────────────────────────────────────────────────────


def test_list_returns_configured_targets_with_perms(repo_app):
    client = repo_app.test_client()
    r = client.get("/api/inbox/repos")
    assert r.status_code == 200
    body = r.get_json()
    by_name = {x["name"]: x for x in body["repos"]}

    assert set(by_name.keys()) == {"evolve", "openclaw"}

    # evolve-ops/evolve → cjalden has admin (maintainer tier)
    e = by_name["evolve"]
    assert e["owner"] == "evolve-ops"
    assert e["repo"] == "evolve"
    assert e["token_slot"] == "github_intake"
    assert e["is_default"] is True
    assert e["self_login"] == "cjalden"
    assert e["permission"] == "admin"
    assert e["is_maintainer"] is True
    assert e["tier_label"] == "maintainer"

    # openclaw/openclaw → cjalden gets 404 → "none" (non-maintainer)
    o = by_name["openclaw"]
    assert o["is_default"] is False
    assert o["permission"] == "none"
    assert o["is_maintainer"] is False
    assert o["tier_label"] == "not a collaborator"


def test_list_handles_unconfigured_pod(tmp_path):
    """No intake.github in network.json → empty list, 200 OK."""
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path)}))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    r = app.test_client().get("/api/inbox/repos")
    assert r.status_code == 200
    assert r.get_json()["repos"] == []


# ─── POST add ─────────────────────────────────────────────────────────────


def test_add_target_persists_and_responds(repo_app):
    client = repo_app.test_client()
    r = client.post("/api/inbox/repos", json={
        "owner": "anthropic", "repo": "claude-cli",
        "name": "claude_cli", "token_slot": "github_intake_claude",
    })
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["added"] == "claude_cli"

    data = _read_network(repo_app.config["_NETWORK_PATH"])
    targets = data["intake"]["github"]["targets"]
    assert "claude_cli" in targets
    assert targets["claude_cli"]["owner"] == "anthropic"
    assert targets["claude_cli"]["repo"] == "claude-cli"
    assert targets["claude_cli"]["token_slot"] == "github_intake_claude"
    # The new target didn't become default (existing default was "evolve").
    assert data["intake"]["github"]["default"] == "evolve"


def test_add_target_defaults_name_to_lowercased_repo(repo_app):
    """If the caller omits ``name``, the repo name (lowercased) is used."""
    r = repo_app.test_client().post("/api/inbox/repos", json={
        "owner": "Anthropic", "repo": "Claude-CLI",
    })
    assert r.status_code == 200
    data = _read_network(repo_app.config["_NETWORK_PATH"])
    assert "claude-cli" in data["intake"]["github"]["targets"]


def test_add_target_with_make_default_flips_default(repo_app):
    r = repo_app.test_client().post("/api/inbox/repos", json={
        "owner": "anthropic", "repo": "claude-cli",
        "name": "claude_cli", "make_default": True,
    })
    assert r.status_code == 200
    data = _read_network(repo_app.config["_NETWORK_PATH"])
    assert data["intake"]["github"]["default"] == "claude_cli"


def test_add_target_rejects_duplicate_name(repo_app):
    r = repo_app.test_client().post("/api/inbox/repos", json={
        "owner": "any", "repo": "any",
        "name": "evolve",  # already in pre-seeded targets
    })
    assert r.status_code == 409
    assert "already exists" in r.get_json()["error"]


def test_add_target_400_on_missing_owner(repo_app):
    r = repo_app.test_client().post("/api/inbox/repos", json={"repo": "x"})
    assert r.status_code == 400


def test_add_target_400_on_missing_repo(repo_app):
    r = repo_app.test_client().post("/api/inbox/repos", json={"owner": "x"})
    assert r.status_code == 400


def test_add_target_migrates_v1_schema_forward(tmp_path):
    """If the pod is on the legacy v1 single-target schema, adding a
    named target must migrate the existing entry forward as the
    'default' target (mirrors the CLI's intake_configure behavior)."""
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "intake": {
            "github": {
                "owner": "evolve-ops", "repo": "evolve",
                "token_slot": "github_intake",
            },
        },
    }))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)

    r = app.test_client().post("/api/inbox/repos", json={
        "owner": "openclaw", "repo": "openclaw", "name": "openclaw",
    })
    assert r.status_code == 200

    data = json.loads(network_path.read_text())
    targets = data["intake"]["github"]["targets"]
    # Both the original (now "default") AND the new entry should exist.
    assert "default" in targets
    assert "openclaw" in targets
    assert targets["default"]["owner"] == "evolve-ops"
    # No top-level owner/repo leak from the legacy shape.
    assert "owner" not in data["intake"]["github"]


# ─── DELETE ───────────────────────────────────────────────────────────────


def test_delete_target_removes_from_targets(repo_app):
    r = repo_app.test_client().delete("/api/inbox/repos/openclaw")
    assert r.status_code == 200
    data = _read_network(repo_app.config["_NETWORK_PATH"])
    assert "openclaw" not in data["intake"]["github"]["targets"]
    assert "evolve" in data["intake"]["github"]["targets"]


def test_delete_default_target_promotes_another(repo_app):
    """Deleting the currently-default target leaves one of the
    remaining targets as the new default. We don't promise WHICH
    survivor wins (first-iter), but it must be one of them."""
    client = repo_app.test_client()
    r = client.delete("/api/inbox/repos/evolve")
    assert r.status_code == 200
    data = _read_network(repo_app.config["_NETWORK_PATH"])
    assert data["intake"]["github"]["default"] in data["intake"]["github"]["targets"]
    assert data["intake"]["github"]["default"] != "evolve"


def test_delete_last_target_clears_github_block(repo_app):
    """Removing the only remaining target should clear the whole
    intake.github block — there's nothing to be 'default' for."""
    client = repo_app.test_client()
    client.delete("/api/inbox/repos/openclaw")
    client.delete("/api/inbox/repos/evolve")
    data = _read_network(repo_app.config["_NETWORK_PATH"])
    assert "github" not in data.get("intake", {})


def test_delete_unknown_target_returns_404(repo_app):
    r = repo_app.test_client().delete("/api/inbox/repos/nonexistent")
    assert r.status_code == 404


def test_delete_invalidates_permission_cache(repo_app):
    """After a target is removed (or any change), the perm cache must
    be cleared so a stale entry can't show in a future re-add of the
    same name."""
    # Prime the cache via a list.
    repo_app.test_client().get("/api/inbox/repos")
    # Permission for openclaw is in the cache now.
    cached_key = ("cjalden", "openclaw", "openclaw")
    assert cached_key in perms._permission_cache

    repo_app.test_client().delete("/api/inbox/repos/openclaw")
    assert cached_key not in perms._permission_cache


# ─── Suggestions (one-click presets for empty state) ──────────────────────


def _suggestions_app(tmp_path, network):
    """Build a Flask app with the given network.json — no keystore stubs,
    no perm stubs (suggestions don't need them; the configured-target
    permission path is exercised by the existing GET tests)."""
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    return app


def test_suggestions_unconfigured_pod_offers_openclaw(tmp_path):
    """An empty pod (no intake.github) gets openclaw/openclaw as a preset."""
    app = _suggestions_app(tmp_path, {
        "sharedDir": str(tmp_path),
        "pod": {"repo_url": "https://github.com/evolve-ops/evolve"},
    })
    body = app.test_client().get("/api/inbox/repos").get_json()
    assert body["repos"] == []
    by_label = {s["label"]: s for s in body["suggestions"]}
    assert "openclaw/openclaw" in by_label
    o = by_label["openclaw/openclaw"]
    assert o["owner"] == "openclaw" and o["repo"] == "openclaw"
    assert o["make_default"] is False


def test_suggestions_include_pod_repo_when_url_set(tmp_path):
    """``network.pod.repo_url`` drives the dynamic 'this pod' suggestion."""
    app = _suggestions_app(tmp_path, {
        "sharedDir": str(tmp_path),
        "pod": {"repo_url": "https://github.com/evolve-ops/evolve"},
    })
    body = app.test_client().get("/api/inbox/repos").get_json()
    by_label = {s["label"]: s for s in body["suggestions"]}
    # Dynamic preset uses the configured URL, not a hardcoded fork name.
    assert "evolve-ops/evolve (this pod)" in by_label
    pod = by_label["evolve-ops/evolve (this pod)"]
    assert pod["owner"] == "evolve-ops" and pod["repo"] == "evolve"
    # The pod's own repo is the natural default target.
    assert pod["make_default"] is True


def test_suggestions_filter_already_configured(tmp_path):
    """A suggestion disappears once its (owner, repo) is configured."""
    app = _suggestions_app(tmp_path, {
        "sharedDir": str(tmp_path),
        "pod": {"repo_url": "https://github.com/evolve-ops/evolve"},
        "intake": {
            "github": {
                "default": "openclaw",
                "targets": {
                    "openclaw": {
                        "owner": "openclaw", "repo": "openclaw",
                        "token_slot": "github_intake",
                    },
                },
            },
        },
    })
    # Stub the keystore + perms transport so the configured target's
    # row computation doesn't crash on missing keystore — we only care
    # about the suggestions field.
    with patch("evolve_admin.keystore.KeystoreManager") as mgr_cls, \
            patch.object(perms, "_default_transport", lambda *a, **kw: (500, {})):
        mgr_cls.return_value.get_value.return_value = None
        body = app.test_client().get("/api/inbox/repos").get_json()
    labels = {s["label"] for s in body["suggestions"]}
    # openclaw is now configured → drops from suggestions
    assert "openclaw/openclaw" not in labels
    # evolve repo is still unconfigured → still suggested
    assert "evolve-ops/evolve (this pod)" in labels


def test_suggestions_skip_pod_repo_for_non_github_url(tmp_path):
    """Self-hosted forks (gitlab, gitea, etc.) get no dynamic preset; the
    openclaw preset still appears because it's universal."""
    app = _suggestions_app(tmp_path, {
        "sharedDir": str(tmp_path),
        "pod": {"repo_url": "https://gitlab.example.com/team/evolve-fork"},
    })
    body = app.test_client().get("/api/inbox/repos").get_json()
    labels = {s["label"] for s in body["suggestions"]}
    assert labels == {"openclaw/openclaw"}


# ─── Intake token save (pod-wide keystore) ────────────────────────────────


def _token_save_app(tmp_path, *, transport):
    """Build a Flask app wired with a stubbed permissions transport and
    a real KeystoreManager pointed at tmp_path. The keystore writes land
    in `{tmp_path}/keystore/` which the test can inspect."""
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path)}))
    with patch.object(perms, "_default_transport", transport):
        app = Flask(__name__)
        app.config["TESTING"] = True
        register_evo_routes(app, network_path)
        yield app


def test_intake_token_saves_to_default_slot(tmp_path):
    """Happy path: POST with valid token saves to github_intake."""
    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "octocat"}
        return 500, {}
    gen = _token_save_app(tmp_path, transport=_tx)
    app = next(gen)
    try:
        r = app.test_client().post("/api/inbox/intake-token", json={
            "token": "ghp_validtoken",
        })
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["slot"] == "github_intake"
        assert body["login"] == "octocat"
        # The token actually landed in the keystore
        from evolve_admin.keystore import KeystoreManager
        assert KeystoreManager(tmp_path).get_value("github_intake") == "ghp_validtoken"
    finally:
        try: next(gen)
        except StopIteration: pass


def test_intake_token_custom_slot(tmp_path):
    """A per-target slot like github_intake_openclaw is honored."""
    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "octocat"}
        return 500, {}
    gen = _token_save_app(tmp_path, transport=_tx)
    app = next(gen)
    try:
        r = app.test_client().post("/api/inbox/intake-token", json={
            "token": "ghp_validtoken",
            "slot": "github_intake_openclaw",
        })
        assert r.status_code == 200
        from evolve_admin.keystore import KeystoreManager
        assert KeystoreManager(tmp_path).get_value("github_intake_openclaw") == "ghp_validtoken"
        # Default slot was NOT written
        assert KeystoreManager(tmp_path).get_value("github_intake") is None
    finally:
        try: next(gen)
        except StopIteration: pass


def test_intake_token_rejects_empty(tmp_path):
    """Missing token → 400, nothing written."""
    def _tx(*a, **kw):
        return 500, {}
    gen = _token_save_app(tmp_path, transport=_tx)
    app = next(gen)
    try:
        r = app.test_client().post("/api/inbox/intake-token", json={"token": ""})
        assert r.status_code == 400
        assert "required" in r.get_json()["error"].lower()
    finally:
        try: next(gen)
        except StopIteration: pass


def test_intake_token_rejects_malformed(tmp_path):
    """Whitespace / absurdly long tokens are rejected before validation."""
    def _tx(*a, **kw):
        # If this transport is ever called, the malformed check failed to gate
        raise AssertionError("transport should not be called for malformed input")
    gen = _token_save_app(tmp_path, transport=_tx)
    app = next(gen)
    try:
        # Whitespace inside
        r = app.test_client().post("/api/inbox/intake-token", json={
            "token": "ghp_has space",
        })
        assert r.status_code == 400
        assert "malformed" in r.get_json()["error"].lower()
        # Way too long
        r = app.test_client().post("/api/inbox/intake-token", json={
            "token": "g" * 500,
        })
        assert r.status_code == 400
    finally:
        try: next(gen)
        except StopIteration: pass


def test_intake_token_rejects_on_github_401(tmp_path):
    """GitHub /user → 401: surface a friendly error, do NOT save."""
    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 401, {"message": "Bad credentials"}
        return 500, {}
    gen = _token_save_app(tmp_path, transport=_tx)
    app = next(gen)
    try:
        r = app.test_client().post("/api/inbox/intake-token", json={
            "token": "ghp_expired",
        })
        assert r.status_code == 400
        err = r.get_json()["error"]
        assert "401" in err or "rejected" in err.lower()
        # And the token was NOT written
        from evolve_admin.keystore import KeystoreManager
        assert KeystoreManager(tmp_path).get_value("github_intake") is None
    finally:
        try: next(gen)
        except StopIteration: pass


def test_intake_token_saves_despite_transport_failure(tmp_path):
    """If GitHub /user can't be reached at all (network down), save anyway
    and surface a validation_warning. The watcher will report the real
    problem on its next poll — better to save and try than block on a
    transient validation failure."""
    def _tx(method, url, headers, body):
        raise OSError("simulated network down")
    gen = _token_save_app(tmp_path, transport=_tx)
    app = next(gen)
    try:
        r = app.test_client().post("/api/inbox/intake-token", json={
            "token": "ghp_someopaque",
        })
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["login"] is None
        assert "validation_warning" in body
        from evolve_admin.keystore import KeystoreManager
        assert KeystoreManager(tmp_path).get_value("github_intake") == "ghp_someopaque"
    finally:
        try: next(gen)
        except StopIteration: pass


def test_intake_token_clears_perm_cache(tmp_path):
    """After saving a token the perm cache must be cleared so the next
    /api/inbox/repos list call uses the new token (not stale 'no token'
    state from earlier polls)."""
    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "octocat"}
        return 500, {}
    gen = _token_save_app(tmp_path, transport=_tx)
    app = next(gen)
    try:
        # Prime the perm cache with a None login for an old token
        perms._self_login_cache["old-token"] = perms._CacheEntry(
            value="ghost", expires_at=9999999999.0,
        )
        app.test_client().post("/api/inbox/intake-token", json={
            "token": "ghp_valid",
        })
        # Cache should now be empty
        assert len(perms._self_login_cache) == 0
    finally:
        try: next(gen)
        except StopIteration: pass


def test_suggestions_omitted_when_both_presets_configured(tmp_path):
    """Once both presets are added, suggestions is empty (chip area hides)."""
    app = _suggestions_app(tmp_path, {
        "sharedDir": str(tmp_path),
        "pod": {"repo_url": "https://github.com/evolve-ops/evolve"},
        "intake": {
            "github": {
                "default": "evolve",
                "targets": {
                    "openclaw": {
                        "owner": "openclaw", "repo": "openclaw",
                        "token_slot": "github_intake",
                    },
                    "evolve": {
                        "owner": "evolve-ops", "repo": "evolve",
                        "token_slot": "github_intake",
                    },
                },
            },
        },
    })
    with patch("evolve_admin.keystore.KeystoreManager") as mgr_cls, \
            patch.object(perms, "_default_transport", lambda *a, **kw: (500, {})):
        mgr_cls.return_value.get_value.return_value = None
        body = app.test_client().get("/api/inbox/repos").get_json()
    assert body["suggestions"] == []
