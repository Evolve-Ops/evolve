"""tests/test_embedding_config_api.py — Phase 4 embedding-config endpoints.

Exercises GET + PUT /api/admin/embedding-config/<bot> via Flask's test client.

Covers:
  - GET resolves the chain from network.json + advertises providers
    with credentials vs without
  - GET surfaces a fragile-chain warning when no remote provider configured
  - PUT validates payload shape (chain must be a list, providers must be
    embedding-capable)
  - PUT writes to network.json models.embedding.per_bot[<bot>].chain
  - PUT calls oc_memory_set with the first two entries of the chain
  - Empty chain clears the per-bot override
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


def _build_bot_paths(tmp_path: Path, bot_id: str, providers: list[str]) -> dict:
    """Lay down a synthetic bot tree with auth-profiles.json under tmp_path.

    Returns a paths dict in the shape that ``resolve_bot_paths`` produces, so
    the fixture can monkeypatch the real resolver to point at this tree.
    The endpoint reads auth-profiles via ``_read_auth_profiles`` →
    ``_find_auth_profile_paths`` → ``_sudo_read``; the latter does a direct
    file read first, so as long as the synthetic file exists at the right
    path no sudo is required.
    """
    bot_root = tmp_path / "bots" / bot_id
    oc_dir = bot_root / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    auth_path = agent_dir / "auth-profiles.json"
    profiles = {
        f"{prov}:api_key": {"provider": prov, "type": "api_key", "key": f"k-{prov}"}
        for prov in providers
    }
    auth_path.write_text(json.dumps({"version": 1, "profiles": profiles, "lastGood": {}}))
    (oc_dir / "openclaw.json").write_text("{}")
    return {
        "oc_config": str(oc_dir / "openclaw.json"),
        "auth_profiles": str(auth_path),
        "agent_dir": str(agent_dir),
        "user": bot_id,
    }


@pytest.fixture
def app_and_paths(tmp_path, monkeypatch):
    """Build a Flask app with a synthetic network.json + per-bot auth-profiles.

    Mirrors the data shape on the real mini: credentials live in each bot's
    auth-profiles.json (NOT in network.json models.providers, which is empty
    on real installs).
    """
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(shared),
        "models": {
            "embedding": {"default_chain": ["gemini", "openai", "local"]},
        },
        "bots": {"team_bot_a": {"role": "team"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # team_bot_a has openai + google credentials in its auth-profiles.json.
    # google credential resolves to gemini embedding via the credential alias.
    bot_paths = _build_bot_paths(tmp_path, "team_bot_a", ["openai", "google", "anthropic"])
    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: bot_paths)

    # Stub oc_memory_set so PUT doesn't try to sudo into a non-existent user.
    calls: list[dict] = []

    def _stub_oms(bot_id, provider, fallback=None, network_path=None):
        calls.append({"bot_id": bot_id, "provider": provider, "fallback": fallback})
        return True

    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_memory_set", _stub_oms)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, network_path, calls


# ── GET ──────────────────────────────────────────────────────────────────────


def test_get_returns_resolved_chain(app_and_paths):
    app, _path, _calls = app_and_paths
    with app.test_client() as c:
        resp = c.get("/api/admin/embedding-config/team_bot_a")
        assert resp.status_code == 200
        data = resp.get_json()
        # Both gemini (via google cred) and openai are configured.
        assert data["resolved_chain"][0] == "gemini"
        assert "openai" in data["resolved_chain"]
        assert data["bot"] == "team_bot_a"


def test_get_marks_providers_as_configured_or_not(app_and_paths):
    app, _path, _calls = app_and_paths
    with app.test_client() as c:
        data = c.get("/api/admin/embedding-config/team_bot_a").get_json()
        by_id = {p["id"]: p for p in data["providers"]}
        # gemini configured (via google credential alias), openai configured.
        assert by_id["gemini"]["is_configured"] is True
        assert by_id["openai"]["is_configured"] is True
        # voyage and mistral have no credentials in the synthetic network.
        assert by_id["voyage"]["is_configured"] is False
        assert by_id["mistral"]["is_configured"] is False
        # No-key providers are always configured.
        assert by_id["local"]["is_configured"] is True
        assert by_id["github-copilot"]["is_configured"] is True


def test_get_includes_capabilities_for_ui(app_and_paths):
    app, _path, _calls = app_and_paths
    with app.test_client() as c:
        data = c.get("/api/admin/embedding-config/team_bot_a").get_json()
        gemini = next(p for p in data["providers"] if p["id"] == "gemini")
        assert "multimodal" in gemini["capabilities"]


def test_get_returns_warning_when_no_remote_provider(tmp_path, monkeypatch):
    """Bot with only anthropic credentials (no embedding-capable provider)
    → chain falls to local → warning fires."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["bot"],
        "sharedDir": str(shared),
        "bots": {"bot": {"role": "team"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # anthropic has no embedding model — bot has no embedding-capable creds.
    bot_paths = _build_bot_paths(tmp_path, "bot", ["anthropic"])
    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: bot_paths)
    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_memory_set", lambda *a, **kw: True)

    app = create_app(network_path)
    with app.test_client() as c:
        data = c.get("/api/admin/embedding-config/bot").get_json()
        assert data["warning"] is not None
        assert "Gemini" in data["warning"] or "OpenAI" in data["warning"]


def test_get_per_bot_override_takes_precedence(tmp_path, monkeypatch):
    """A per-bot chain in network.json overrides the pod default in resolved_chain."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["security_bot"],
        "sharedDir": str(shared),
        "models": {
            "embedding": {
                "default_chain": ["gemini", "openai", "local"],
                "per_bot": {"security_bot": {"chain": ["openai", "local"]}},
            },
        },
        "bots": {"security_bot": {"role": "team"}},
    }
    np = tmp_path / "network.json"
    np.write_text(json.dumps(network))

    bot_paths = _build_bot_paths(tmp_path, "security_bot", ["openai", "google"])
    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: bot_paths)
    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_memory_set", lambda *a, **kw: True)

    app = create_app(np)
    with app.test_client() as c:
        data = c.get("/api/admin/embedding-config/security_bot").get_json()
        assert data["resolved_chain"][0] == "openai"
        assert data["per_bot_chain"] == ["openai", "local"]


# ── PUT ──────────────────────────────────────────────────────────────────────


def test_put_writes_chain_to_network_json(app_and_paths):
    app, network_path, calls = app_and_paths
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/embedding-config/team_bot_a",
            json={"chain": ["openai", "local"]},
        )
        assert resp.status_code == 200
        net = json.loads(network_path.read_text())
        assert net["models"]["embedding"]["per_bot"]["team_bot_a"]["chain"] == ["openai", "local"]


def test_put_calls_oc_memory_set_with_primary_and_fallback(app_and_paths):
    app, _path, calls = app_and_paths
    with app.test_client() as c:
        c.put(
            "/api/admin/embedding-config/team_bot_a",
            json={"chain": ["openai", "local"]},
        )
    assert len(calls) == 1
    assert calls[0]["bot_id"] == "team_bot_a"
    assert calls[0]["provider"] == "openai"
    assert calls[0]["fallback"] == "local"


def test_put_calls_oc_memory_set_without_fallback_for_single_chain(app_and_paths):
    app, _path, calls = app_and_paths
    with app.test_client() as c:
        c.put("/api/admin/embedding-config/team_bot_a", json={"chain": ["openai"]})
    assert calls[0]["provider"] == "openai"
    assert calls[0]["fallback"] is None


def test_put_rejects_non_list_chain(app_and_paths):
    app, _path, _calls = app_and_paths
    with app.test_client() as c:
        resp = c.put("/api/admin/embedding-config/team_bot_a", json={"chain": "openai"})
        assert resp.status_code == 400
        assert "chain" in resp.get_json()["error"]


def test_put_rejects_unknown_provider(app_and_paths):
    """Catch typos / chat-only providers (anthropic, xai) at the API boundary."""
    app, _path, _calls = app_and_paths
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/embedding-config/team_bot_a",
            json={"chain": ["openai", "anthropic"]},
        )
        assert resp.status_code == 400
        assert "anthropic" in resp.get_json()["error"]


def test_put_empty_chain_clears_override(app_and_paths):
    """Empty chain removes the per-bot override; resolution falls back to pod default."""
    app, network_path, _calls = app_and_paths
    with app.test_client() as c:
        # First set an override.
        c.put("/api/admin/embedding-config/team_bot_a", json={"chain": ["openai"]})
        # Now clear it.
        c.put("/api/admin/embedding-config/team_bot_a", json={"chain": []})
    net = json.loads(network_path.read_text())
    per_bot = net.get("models", {}).get("embedding", {}).get("per_bot", {})
    assert "team_bot_a" not in per_bot


def test_get_reads_credentials_from_auth_profiles_not_models_providers(app_and_paths):
    """Regression for the post-#913 bug: real evolve installs have empty
    network.json models.providers; credentials live per-bot in
    auth-profiles.json. The endpoint must read from there.
    """
    app, _path, _calls = app_and_paths
    with app.test_client() as c:
        data = c.get("/api/admin/embedding-config/team_bot_a").get_json()
        configured_ids = {p["id"] for p in data["providers"] if p["is_configured"]}
        # The fixture lays down openai+google+anthropic in auth-profiles.json.
        # google → gemini via credential alias; openai stays openai;
        # anthropic has no embedding product so it doesn't appear at all.
        assert "openai" in configured_ids
        assert "gemini" in configured_ids
        # Voyage / mistral have no credentials — must NOT be marked configured.
        assert "voyage" not in configured_ids
        assert "mistral" not in configured_ids


def test_put_rejects_oc_memory_set_failure(tmp_path, monkeypatch):
    """If oc_memory_set returns False, PUT must surface a 500."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["b"],
        "sharedDir": str(shared),
        "bots": {"b": {"role": "team"}},
    }
    np = tmp_path / "network.json"
    np.write_text(json.dumps(network))

    bot_paths = _build_bot_paths(tmp_path, "b", ["openai"])
    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: bot_paths)
    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_memory_set", lambda *a, **kw: False)

    app = create_app(np)
    with app.test_client() as c:
        resp = c.put("/api/admin/embedding-config/b", json={"chain": ["openai"]})
        assert resp.status_code == 500
