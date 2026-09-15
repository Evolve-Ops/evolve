"""tests/test_onboarding.py — guided bot integration onboarding (github + brave).

Covers:
  - `_ensure_brave_wired_in_dict()` — scaffold + provider opt-out semantics
  - `_ensure_github_remote()` — add new remote / update existing / no-op
  - Nonce store: `_store_discovered_pat_nonce` / `_redeem_discovered_pat_nonce`
  - Brave verify endpoint — eligibility per bot
  - Brave onboard endpoint — wiring + skip list for opted-out bots
  - Github verify endpoint — collision data, override routing, bad-PAT 200-not-500
  - Github onboard endpoint — preflight 409 collision gate, idempotency
  - Discover-default-pat endpoint — nonce returned, no plaintext leak
  - Keys API extensions — pod_invariants, pod_default_github_account[_source],
    repo_slug, opted_out status

The github tests stub `_github_api()` so the test suite makes no real HTTP
calls. Real github wiring is exercised end-to-end by the catalog (KR6+).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from evolve_admin.web.server import (  # noqa: E402
    _ensure_brave_wired_in_dict,
    _store_discovered_pat_nonce,
    _redeem_discovered_pat_nonce,
    _resolve_credential,
    _DISCOVERED_PAT_NONCES,
    _resolve_bot_user,
    _mask_key,
    _read_oc_json,
    _write_oc_json,
    _discover_github_remote,
)


# ─────────────────────────────────────────────────────────────────────────────
# _ensure_brave_wired_in_dict
# ─────────────────────────────────────────────────────────────────────────────


def test_brave_wired_creates_scaffold_on_empty_dict():
    cfg = {}
    out = _ensure_brave_wired_in_dict(cfg)
    assert cfg["plugins"]["entries"]["brave"]["config"]["webSearch"] == {}
    assert cfg["tools"]["web"]["search"]["provider"] == "brave"
    assert out["provider_overridden"] is False
    assert out["current_provider"] == "brave"


def test_brave_wired_idempotent_when_already_brave():
    cfg = {
        "tools": {"web": {"search": {"provider": "brave"}}},
        "plugins": {"entries": {"brave": {"config": {"webSearch": {"apiKey": "BSA-x"}}}}},
    }
    snapshot = json.dumps(cfg, sort_keys=True)
    out = _ensure_brave_wired_in_dict(cfg)
    assert out["provider_overridden"] is False
    assert out["current_provider"] == "brave"
    # Existing apiKey untouched
    assert cfg["plugins"]["entries"]["brave"]["config"]["webSearch"]["apiKey"] == "BSA-x"


def test_brave_wired_preserves_non_brave_provider():
    cfg = {"tools": {"web": {"search": {"provider": "tavily"}}}}
    out = _ensure_brave_wired_in_dict(cfg)
    # Provider preserved per v3 design: opt-out is a deliberate choice
    assert cfg["tools"]["web"]["search"]["provider"] == "tavily"
    assert out["provider_overridden"] is True
    assert out["current_provider"] == "tavily"
    # But the brave plugin scaffold is still created (so a future flip to brave works)
    assert "webSearch" in cfg["plugins"]["entries"]["brave"]["config"]


def test_brave_wired_treats_empty_string_as_unset():
    cfg = {"tools": {"web": {"search": {"provider": ""}}}}
    out = _ensure_brave_wired_in_dict(cfg)
    assert cfg["tools"]["web"]["search"]["provider"] == "brave"
    assert out["provider_overridden"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Nonce store
# ─────────────────────────────────────────────────────────────────────────────


def test_nonce_store_redeems_within_ttl():
    _DISCOVERED_PAT_NONCES.clear()
    nonce = _store_discovered_pat_nonce("ghp_xxx", "alice", "team_bot_a", ttl_seconds=60)
    assert nonce
    result = _redeem_discovered_pat_nonce(nonce)
    assert result == ("ghp_xxx", "alice", "team_bot_a")


def test_nonce_store_rejects_expired_nonce():
    import time

    # Re-fetch the nonce store + helpers via the *current* module globals
    # rather than the module-level captures at top of this file. A different
    # test in this suite (test_google_oauth_state_persistence.py) calls
    # importlib.reload(evolve_admin.web.server), which re-binds
    # _DISCOVERED_PAT_NONCES to a fresh dict on the module. The OLD
    # _store_discovered_pat_nonce function (captured at this test file's
    # import) writes to whatever the module's CURRENT global points at,
    # while this test's captured _DISCOVERED_PAT_NONCES still refers to the
    # original dict — the two disagree and we KeyError on lookup.
    from evolve_admin.web import server as _srv

    _srv._DISCOVERED_PAT_NONCES.clear()
    nonce = _srv._store_discovered_pat_nonce(
        "ghp_xxx", "alice", "team_bot_a", ttl_seconds=1
    )
    # Force expiry by mutating the stored expires_at directly.
    with patch("evolve_admin.web.server._DISCOVERED_PAT_LOCK"):
        token, login, src, exp = _srv._DISCOVERED_PAT_NONCES[nonce]
        _srv._DISCOVERED_PAT_NONCES[nonce] = (token, login, src, time.time() - 10)
    assert _srv._redeem_discovered_pat_nonce(nonce) is None
    # And it gets purged from the store on the failed redeem
    assert nonce not in _srv._DISCOVERED_PAT_NONCES


def test_nonce_store_unknown_nonce_returns_none():
    _DISCOVERED_PAT_NONCES.clear()
    assert _redeem_discovered_pat_nonce("does-not-exist") is None


def test_resolve_credential_recognizes_pat_prefix():
    # Raw github PAT prefixes pass through unchanged.
    tok, login, src = _resolve_credential("ghp_realtoken")
    assert tok == "ghp_realtoken"
    assert login is None and src is None


def test_resolve_credential_redeems_nonce():
    _DISCOVERED_PAT_NONCES.clear()
    nonce = _store_discovered_pat_nonce("ghp_secret", "alice", "team_bot_a", ttl_seconds=60)
    tok, login, src = _resolve_credential(nonce)
    assert tok == "ghp_secret"
    assert login == "alice"
    assert src == "team_bot_a"


def test_resolve_credential_handles_empty():
    assert _resolve_credential("") == (None, None, None)
    assert _resolve_credential(None) == (None, None, None)
    assert _resolve_credential("   ") == (None, None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Flask fixtures (reuse the rotation-test pattern)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_bot(tmp_path, monkeypatch):
    """Single-bot fake home tree at /Users/team_bot_a/.openclaw, with a backing
    `.git/config` so onboarding-related code paths can read/write it."""
    bot_id = "team_bot_a"
    bot_home = tmp_path / "Users" / bot_id
    oc_dir = bot_home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"
    git_dir = workspace / ".git"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)
    git_dir.mkdir()

    oc_path = oc_dir / "openclaw.json"
    oc_path.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(workspace)}},
        "channels": {"telegram": {"botToken": "old-tg"}},
        "plugins": {"entries": {}},
    }, indent=2))

    auth_path = agent_dir / "auth-profiles.json"
    auth_path.write_text(json.dumps({"profiles": {}}, indent=2))

    git_config_path = git_dir / "config"
    git_config_path.write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n\turl = https://ghp_oldtoken@github.com/evolve-ops/team_bot_a-evolve.git\n'
    )

    paths_dict = {
        "oc_config": str(oc_path),
        "workspace": str(workspace),
        "agent_dir": str(agent_dir),
        "auth_profiles": str(auth_path),
        "turns_dir": str(workspace / "turns"),
        "turns_dir_fallback": str(workspace / "turns"),
        "turns_dir_candidates": [str(workspace / "turns")],
        "logs_dir": str(oc_dir / "logs"),
        "user": bot_id,
    }

    from evolve_admin.web import server as srv
    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: paths_dict)

    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and len(args) >= 2 and args[0] == "sudo":
            # Skip the optional ``-n`` (non-interactive) flag — production
            # paths use it so missing sudoers grants fail fast on the
            # launchd-spawned admin server (no TTY for the password prompt).
            idx = 1
            if idx < len(args) and args[idx] == "-n":
                idx += 1
            if idx >= len(args):
                return real_run(args, *a, **kw)
            verb = args[idx]
            rest = list(args[idx + 1:])
            try:
                if verb in ("/bin/cat", "cat"):
                    text = Path(rest[0]).read_text()
                    return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
                if verb == "/bin/cp":
                    src, dst = rest[0], rest[1]
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, dst)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "/bin/mkdir":
                    target = rest[-1]
                    Path(target).mkdir(parents=True, exist_ok=True)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "/bin/chmod":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb in ("/usr/sbin/chown", "chown"):
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            except Exception as exc:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
        return real_run(args, *a, **kw)

    monkeypatch.setattr("subprocess.run", fake_run)

    return {
        "bot_id": bot_id,
        "oc_path": oc_path,
        "auth_path": auth_path,
        "git_config_path": git_config_path,
        "paths": paths_dict,
    }


@pytest.fixture
def app(tmp_path, fake_bot):
    from evolve_admin.web.server import create_app

    network = {
        "primary": fake_bot["bot_id"],
        "bots": {fake_bot["bot_id"]: {"user": fake_bot["bot_id"]}},
        # Keep keystore writes (github_pat persistence, 2.8) inside tmp.
        "sharedDir": str(tmp_path / "shared"),
    }
    (tmp_path / "shared").mkdir(exist_ok=True)
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


# ─────────────────────────────────────────────────────────────────────────────
# Keys API extensions: pod_invariants, pod_default_github_account, repo_slug
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# _ensure_github_remote (closure inside _register_admin_routes — exercise via
# the github onboard route which calls it as part of the per-bot loop)
# ─────────────────────────────────────────────────────────────────────────────


def test_github_onboard_initializes_evolve_backup_remote(app, fake_bot):
    """After onboard, .git/config has an `evolve-backup` remote pointing
    at the new https://<token>@github.com/{login}/{repo}.git URL."""
    pubkey = "ssh-ed25519 AAAAremote-test-pubkey evolve-team_bot_a"

    class FakeResp:
        def __init__(self, status, body, headers=None):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers or {}
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        if "/user/repos" in url and method == "POST":
            return FakeResp(201, {"name": "team_bot_a-workspace"}, {})
        if "/repos/" in url and "/keys" in url and method == "GET":
            return FakeResp(200, [], {})
        if "/repos/" in url and "/keys" in url and method == "POST":
            return FakeResp(201, {"id": 1}, {})
        if "/repos/" in url and method == "GET":
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_remote_test", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace", "reuse_confirmed": False}],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["results"][0]["ok"] is True

    # Verify .git/config now has the evolve-backup section pointing at the new repo
    cfg_text = fake_bot["git_config_path"].read_text()
    assert '[remote "evolve-backup"]' in cfg_text
    assert "https://ghp_remote_test@github.com/evolve-ops/team_bot_a-workspace.git" in cfg_text


def test_github_onboard_persists_pat_to_keystore(app, fake_bot, tmp_path):
    """After a successful onboard, the default PAT lands in the KEYSTORE
    (roadmap 2.8) — so the periodic backup_visibility monitor can verify
    each repo without a separate config step — and network.json stays
    free of the plaintext credential.

    Closes the wizard ↔ monitor gap that fired "GitHub PAT missing — N bot
    backup repos cannot be verified" right after the wizard succeeded
    (the wizard previously only embedded the PAT in per-bot .git/config).
    """
    pubkey = "ssh-ed25519 AAAApersist-pubkey evolve-team_bot_a"

    class FakeResp:
        def __init__(self, status, body, headers=None):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers or {}
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        if "/user/repos" in url and method == "POST":
            return FakeResp(201, {"name": "team_bot_a-workspace"}, {})
        if "/repos/" in url and "/keys" in url and method == "GET":
            return FakeResp(200, [], {})
        if "/repos/" in url and "/keys" in url and method == "POST":
            return FakeResp(201, {"id": 1}, {})
        if "/repos/" in url and method == "GET":
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    network_path = tmp_path / "network.json"
    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_pat_to_persist", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace", "reuse_confirmed": False}],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["results"][0]["ok"] is True
            assert body["pat_persisted"] is True

    # Fresh-pod proof grep (roadmap 2.8): no live credential in any
    # plaintext config file — the PAT is in the keystore instead.
    net = json.loads(network_path.read_text())
    assert "ghp_pat_to_persist" not in network_path.read_text()
    assert net.get("github", {}).get("pat") in (None, "")
    from evolve_admin.keystore import load_github_pat
    assert load_github_pat(tmp_path / "shared") == "ghp_pat_to_persist"


def test_github_onboard_does_not_persist_pat_on_total_failure(app, fake_bot, tmp_path):
    """If every bot's onboard call fails (no repo created), the wizard must
    NOT persist an untrusted PAT (keystore or otherwise). Persist only
    when at least one bot succeeded with the token.
    """
    pubkey = "ssh-ed25519 AAAAfail-pubkey evolve-team_bot_a"

    class FakeResp:
        def __init__(self, status, body, headers=None):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers or {}
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        # Repo create returns 5xx — the per-bot _onboard_one_github_bot
        # path returns ok=False, so the post-loop persist guard skips.
        if "/user/repos" in url and method == "POST":
            return FakeResp(500, {"message": "internal error"}, {})
        if "/repos/" in url and method == "GET":
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    network_path = tmp_path / "network.json"
    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_should_not_persist", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace", "reuse_confirmed": False}],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["results"][0]["ok"] is False
            assert body["pat_persisted"] is False

    net = json.loads(network_path.read_text())
    assert net.get("github", {}).get("pat") in (None, "")
    from evolve_admin.keystore import load_github_pat
    assert load_github_pat(tmp_path / "shared") is None


def test_keys_api_includes_pod_invariants_default(app, fake_bot):
    # Brave was demoted to optional (2026-06-24): the default invariant set is
    # github-only. A fresh pod (no podInvariantIntegrations key) inherits this.
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{fake_bot['bot_id']}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["pod_invariants"] == ["github"]
        assert "brave" not in body["pod_invariants"]


def _brave_row(body: dict) -> dict:
    return next(k for k in body["keys"] if k["provider"] == "brave")


def test_keys_api_brave_active_from_canonical_oc_key(app, fake_bot):
    # A Brave key written to the CANONICAL openclaw.json location (where the
    # wizard / rotate path writes) must read ACTIVE on the Credentials tab —
    # not the "Setup required" the auth-profiles-only read used to produce.
    oc = json.loads(fake_bot["oc_path"].read_text())
    oc.setdefault("plugins", {}).setdefault("entries", {})["brave"] = {
        "config": {"webSearch": {"apiKey": "BSA-canonical-key"}}
    }
    fake_bot["oc_path"].write_text(json.dumps(oc))
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        row = _brave_row(body)
        assert row["status"] == "active"
        assert row.get("oc_only") is True


def test_keys_api_brave_active_from_legacy_oc_key(app, fake_bot):
    # A Brave key at the LEGACY tools.web.search.apiKey location (hand-edited /
    # older configs) must also read ACTIVE — detection honours both paths.
    oc = json.loads(fake_bot["oc_path"].read_text())
    oc.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})[
        "apiKey"
    ] = "BSA-legacy-key"
    fake_bot["oc_path"].write_text(json.dumps(oc))
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        assert _brave_row(body)["status"] == "active"


def test_keys_api_brave_missing_when_unconfigured(app, fake_bot):
    # Baseline: no Brave key anywhere → the row is still present (registry
    # always lists brave) but reads "missing", not active.
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        assert _brave_row(body)["status"] == "missing"


def test_keys_api_respects_pod_invariants_override(tmp_path, fake_bot):
    from evolve_admin.web.server import create_app
    network = {
        "primary": fake_bot["bot_id"],
        "bots": {fake_bot["bot_id"]: {"user": fake_bot["bot_id"]}},
        "podInvariantIntegrations": ["github"],  # exclude brave
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app2 = create_app(network_path)
    app2.config["TESTING"] = True
    with app2.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        assert body["pod_invariants"] == ["github"]


def test_keys_api_surfaces_repo_slug_for_github(app, fake_bot):
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        gh = next((k for k in body["keys"] if k["provider"] == "github"), None)
        assert gh is not None
        # The fake bot's .git/config has a token-bearing URL, so discovery hits.
        assert gh["repo_slug"] == "cjalden/team_bot_a-evolve"
        assert gh["status"] == "active"


def test_keys_api_pod_default_github_account_from_cascade(app, fake_bot):
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        assert body["pod_default_github_account"] == "cjalden"
        assert body["pod_default_github_account_source"] == "discovered_from_team_bot_a"


def test_keys_api_github_active_for_plain_https_remote(app, fake_bot):
    """Bots with a plain HTTPS remote (no embedded PAT — credentials live in
    a credential helper / keychain) must surface as Active. Real-world case:
    Admin_bot's `url = https://github.com/evolve-ops/admin_bot-workspace.git`."""
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n\turl = https://github.com/evolve-ops/admin_bot-workspace.git\n'
    )
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        gh = next((k for k in body["keys"] if k["provider"] == "github"), None)
        assert gh is not None
        assert gh["status"] == "active"
        assert gh["auth_type"] == "https_credhelper"
        assert gh["type_label"] == "HTTPS + credential helper"
        assert gh["repo_slug"] == "cjalden/admin_bot-workspace"


def test_keys_api_github_active_for_ssh_remote(app, fake_bot):
    """Bots whose .git/config uses an SSH remote (git@github.com:...) — the
    pattern documented by analyzer/backup.py and used by Admin_bot/Security_bot — must
    surface as Active, not Setup required."""
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n\turl = git@github.com:cjalden/team_bot_a-workspace.git\n'
    )
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        gh = next((k for k in body["keys"] if k["provider"] == "github"), None)
        assert gh is not None
        assert gh["status"] == "active"
        assert gh["auth_type"] == "ssh"
        assert gh["type_label"] == "SSH deploy key"
        assert gh["repo_slug"] == "cjalden/team_bot_a-workspace"


def test_keys_api_github_missing_when_no_remote(app, fake_bot):
    """If .git/config has no github remote at all, the row stays missing."""
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
    )
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        gh = next((k for k in body["keys"] if k["provider"] == "github"), None)
        assert gh is not None
        assert gh["status"] == "missing"


def test_keys_api_brave_opted_out_when_provider_set_to_other(app, fake_bot):
    # Mutate openclaw.json to set provider=tavily, then re-query.
    cfg = json.loads(fake_bot["oc_path"].read_text())
    cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})["provider"] = "tavily"
    fake_bot["oc_path"].write_text(json.dumps(cfg, indent=2))
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        brave = next((k for k in body["keys"] if k["provider"] == "brave"), None)
        assert brave is not None
        assert brave["status"] == "opted_out"
        assert "tavily" in brave["opted_out_reason"]


def test_keys_api_brave_active_when_only_openclaw_has_key(app, fake_bot):
    """Bot with apiKey wired into openclaw.json plugin slot but missing from
    auth-profiles.json must report status=active with oc_only=True so the
    onboarding wizard skips it instead of overwriting."""
    cfg = json.loads(fake_bot["oc_path"].read_text())
    full_key = "BSA-from-cli-1234567890abcdef"
    cfg.setdefault("plugins", {}).setdefault("entries", {})["brave"] = {
        "config": {"webSearch": {"apiKey": full_key}}
    }
    cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})["provider"] = "brave"
    fake_bot["oc_path"].write_text(json.dumps(cfg, indent=2))
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        brave = next((k for k in body["keys"] if k["provider"] == "brave"), None)
        assert brave is not None
        assert brave["status"] == "active"
        assert brave["oc_only"] is True
        # masked must redact the middle of the key (first-8 + ... + last-4)
        assert brave.get("masked") and "..." in brave["masked"]
        assert full_key not in brave["masked"]


def test_keys_api_brave_oc_drift_flagged_when_keys_differ(app, fake_bot):
    """When auth-profiles and openclaw.json carry DIFFERENT brave keys,
    auth-profiles wins (canonical) but the row carries oc_drift=True."""
    cfg = json.loads(fake_bot["oc_path"].read_text())
    cfg.setdefault("plugins", {}).setdefault("entries", {})["brave"] = {
        "config": {"webSearch": {"apiKey": "BSA-from-oc"}}
    }
    fake_bot["oc_path"].write_text(json.dumps(cfg, indent=2))
    auth = json.loads(fake_bot["auth_path"].read_text())
    auth.setdefault("profiles", {})["brave_api_key"] = {
        "provider": "brave", "type": "api_key", "key": "BSA-from-auth",
    }
    fake_bot["auth_path"].write_text(json.dumps(auth, indent=2))
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        brave = next((k for k in body["keys"] if k["provider"] == "brave"), None)
        assert brave is not None
        assert brave["status"] == "active"
        assert brave.get("oc_drift") is True
        assert brave.get("oc_only", False) is False


# ─────────────────────────────────────────────────────────────────────────────
# Discover-default-pat endpoint
# ─────────────────────────────────────────────────────────────────────────────


def test_discover_default_pat_returns_nonce_and_masked(app, fake_bot):
    _DISCOVERED_PAT_NONCES.clear()
    with app.test_client() as c:
        body = c.get("/api/admin/onboard/github/discover-default-pat").get_json()
        assert body is not None
        assert body["source_bot"] == "team_bot_a"
        assert body["login"] == "cjalden"
        # masked must NOT contain the raw token
        assert "ghp_oldtoken" not in body["masked"]
        assert body["nonce"]
        # Body must not include a plaintext token field
        assert "token" not in body


def test_discover_default_pat_returns_null_when_no_bots(tmp_path, monkeypatch):
    """When no PAT is discoverable, the response carries nonce=null (the
    frontend guards on `r.nonce`) but still returns the new available_orgs
    and bot_owners shapes so the modal can render an empty-state."""
    from evolve_admin.web.server import create_app
    network = {"bots": {}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        body = c.get("/api/admin/onboard/github/discover-default-pat").get_json()
        assert body is not None
        assert body.get("nonce") is None
        assert body.get("available_orgs") == []
        assert body.get("bot_owners") == {}


# ─────────────────────────────────────────────────────────────────────────────
# Brave verify + onboard endpoints
# ─────────────────────────────────────────────────────────────────────────────


def test_brave_verify_eligible_for_unset_provider(app, fake_bot):
    with patch("evolve_admin.web.server._register_admin_routes.__wrapped__", create=True):
        # Patch _brave_verify by monkeypatching urllib at the boundary.
        import urllib.request

        class FakeResp:
            status = 200
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=None):
            return FakeResp()

        with patch("urllib.request.urlopen", fake_urlopen):
            with app.test_client() as c:
                body = c.post(
                    "/api/admin/onboard/brave/verify",
                    json={"key": "BSA-test", "bots": [fake_bot["bot_id"]]},
                ).get_json()
                assert body["brave_ok"] is True
                assert body["bots"][0]["eligible"] is True
                assert body["bots"][0]["current_provider"] is None


def test_brave_verify_ineligible_when_already_configured_in_openclaw(app, fake_bot):
    """A bot that already has the apiKey in openclaw.json (e.g. wired by OC
    CLI) must report eligible=False, already_configured=True, oc_only=True so
    the wizard explicitly tells the operator instead of silently overwriting."""
    cfg = json.loads(fake_bot["oc_path"].read_text())
    cfg.setdefault("plugins", {}).setdefault("entries", {})["brave"] = {
        "config": {"webSearch": {"apiKey": "BSA-existing"}}
    }
    cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})["provider"] = "brave"
    fake_bot["oc_path"].write_text(json.dumps(cfg, indent=2))
    import urllib.request

    class FakeResp:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", lambda req, timeout=None: FakeResp()):
        with app.test_client() as c:
            body = c.post(
                "/api/admin/onboard/brave/verify",
                json={"key": "BSA-test", "bots": [fake_bot["bot_id"]]},
            ).get_json()
            row = body["bots"][0]
            assert row["eligible"] is False
            assert row["already_configured"] is True
            assert row["oc_only"] is True
            assert row["current_provider"] == "brave"


def test_brave_verify_ineligible_when_already_in_auth_profiles(app, fake_bot):
    """A bot that already has a brave key in auth-profiles.json must report
    eligible=False, already_configured=True, oc_only=False."""
    auth = json.loads(fake_bot["auth_path"].read_text())
    auth.setdefault("profiles", {})["brave_api_key"] = {
        "provider": "brave", "type": "api_key", "key": "BSA-existing",
    }
    fake_bot["auth_path"].write_text(json.dumps(auth, indent=2))
    import urllib.request

    class FakeResp:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", lambda req, timeout=None: FakeResp()):
        with app.test_client() as c:
            body = c.post(
                "/api/admin/onboard/brave/verify",
                json={"key": "BSA-test", "bots": [fake_bot["bot_id"]]},
            ).get_json()
            row = body["bots"][0]
            assert row["eligible"] is False
            assert row["already_configured"] is True
            assert row["oc_only"] is False


def test_brave_verify_ineligible_for_opted_out_bot(app, fake_bot):
    cfg = json.loads(fake_bot["oc_path"].read_text())
    cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})["provider"] = "tavily"
    fake_bot["oc_path"].write_text(json.dumps(cfg, indent=2))
    import urllib.request

    class FakeResp:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", lambda req, timeout=None: FakeResp()):
        with app.test_client() as c:
            body = c.post(
                "/api/admin/onboard/brave/verify",
                json={"key": "BSA-test", "bots": [fake_bot["bot_id"]]},
            ).get_json()
            assert body["bots"][0]["eligible"] is False
            assert body["bots"][0]["current_provider"] == "tavily"


def test_brave_verify_returns_200_on_invalid_key(app, fake_bot):
    """Bad credentials must return 200 with brave_ok=False, NOT 500."""
    import urllib.error

    def raises(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with patch("urllib.request.urlopen", raises):
        with app.test_client() as c:
            resp = c.post(
                "/api/admin/onboard/brave/verify",
                json={"key": "BSA-bad", "bots": [fake_bot["bot_id"]]},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["brave_ok"] is False
            assert body["status"] == 401


def test_brave_onboard_skips_opted_out_bot(app, fake_bot):
    cfg = json.loads(fake_bot["oc_path"].read_text())
    cfg.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})["provider"] = "tavily"
    fake_bot["oc_path"].write_text(json.dumps(cfg, indent=2))
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/onboard/brave",
            json={"key": "BSA-real", "bots": [fake_bot["bot_id"]]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["results"] == []
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["bot"] == "team_bot_a"
        assert "tavily" in body["skipped"][0]["reason"]


def test_brave_onboard_writes_key_and_sets_provider(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            "/api/admin/onboard/brave",
            json={"key": "BSA-real-key", "bots": [fake_bot["bot_id"]]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["results"][0]["ok"] is True
        # openclaw.json now has both the key AND provider=brave
        oc = json.loads(fake_bot["oc_path"].read_text())
        assert oc["plugins"]["entries"]["brave"]["config"]["webSearch"]["apiKey"] == "BSA-real-key"
        assert oc["tools"]["web"]["search"]["provider"] == "brave"
        # auth-profiles also written under the canonical colon shape
        # (every provider, including brave, uses `<provider>:<type>` now —
        # see _canonical_profile_id docstring for the history)
        ap = json.loads(fake_bot["auth_path"].read_text())
        assert ap["profiles"]["brave:api_key"]["key"] == "BSA-real-key"
        assert "brave_api_key" not in ap["profiles"]


# ─────────────────────────────────────────────────────────────────────────────
# Github verify + onboard endpoints (with stubbed _github_api)
# ─────────────────────────────────────────────────────────────────────────────


def _stub_github_api(responses):
    """Build a side_effect function for _github_api that returns the next
    response from a list of (status, body, headers) tuples in order.

    `responses` may also include a 'match' key on each entry with a substring
    of the requested path; if present, the function returns the entry only
    when the path matches. Useful for asserting the request order.
    """
    state = {"i": 0}

    def fake(method, path, token, body=None):
        # Strict by-order; tests document the expected sequence.
        if state["i"] >= len(responses):
            raise AssertionError(f"unexpected github API call: {method} {path}")
        entry = responses[state["i"]]
        state["i"] += 1
        return entry

    return fake


def test_github_verify_returns_collision_data_for_existing_repo(app, fake_bot, monkeypatch):
    """When the repo exists at github, verify reports it with last-pushed and
    a has_evolve_pubkey flag derived from the deploy keys list."""
    fake = _stub_github_api([
        # GET /user
        (200, {"login": "cjalden"}, {"x-oauth-scopes": "repo, workflow"}),
        # GET /repos/cjalden/team_bot_a-workspace
        (200, {
            "html_url": "https://github.com/evolve-ops/team_bot_a-workspace",
            "private": True,
            "default_branch": "main",
            "pushed_at": "2025-12-01T00:00:00Z",
        }, {}),
        # GET /repos/.../keys
        (200, [{"title": "evolve-team_bot_a", "key": "ssh-ed25519 AAAA..."}], {}),
    ])
    from evolve_admin.web import server as srv
    with patch.object(srv, "_register_admin_routes"):
        pass  # placeholder
    # Patch _github_api inside the closure-scoped routes by patching at module call time.
    # The simplest way is to patch the module attribute the closure resolves at call time.
    # Since _github_api is defined inside _register_admin_routes (closure), we need to
    # patch the request-handler closure's __globals__. We fall back to patching urlopen.
    # → Just patch urllib.request.urlopen.
    import urllib.request

    seq_state = {"i": 0}
    seq = [
        # /user
        (200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo, workflow"}),
        # /repos
        (200, {
            "html_url": "https://github.com/evolve-ops/team_bot_a-workspace",
            "private": True,
            "default_branch": "main",
            "pushed_at": "2025-12-01T00:00:00Z",
        }, {}),
        # /keys
        (200, [{"title": "evolve-team_bot_a", "key": "ssh-ed25519 AAAA..."}], {}),
    ]

    class FakeResp:
        def __init__(self, status, body, headers):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        i = seq_state["i"]
        seq_state["i"] += 1
        status, body, hdrs = seq[i]
        return FakeResp(status, body, hdrs)

    with patch("urllib.request.urlopen", fake_urlopen):
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github/verify", json={
                "default": {"token": "ghp_test", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace"}],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["bots"][0]["ok"] is True
            assert body["bots"][0]["login"] == "cjalden"
            assert body["bots"][0]["has_repo_scope"] is True
            assert body["bots"][0]["repo"]["exists"] is True
            assert body["bots"][0]["repo"]["has_evolve_pubkey"] is True


def test_github_verify_no_collision_when_repo_404(app, fake_bot):
    import urllib.error, urllib.request

    seq_state = {"i": 0}
    seq = [
        (200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"}),
    ]

    class FakeResp:
        def __init__(self, status, body, headers):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        if "/repos/" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        i = seq_state["i"]
        seq_state["i"] += 1
        status, body, hdrs = seq[i]
        return FakeResp(status, body, hdrs)

    with patch("urllib.request.urlopen", fake_urlopen):
        with app.test_client() as c:
            body = c.post("/api/admin/onboard/github/verify", json={
                "default": {"token": "ghp_test", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "fresh-repo"}],
            }).get_json()
            assert body["bots"][0]["ok"] is True
            assert body["bots"][0]["repo"]["exists"] is False


def test_github_verify_returns_200_on_invalid_pat(app, fake_bot):
    """Bad PAT → per-bot ok=False, NOT 500."""
    import urllib.error

    def raises(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with patch("urllib.request.urlopen", raises):
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github/verify", json={
                "default": {"token": "ghp_bad", "github_login": "anyone"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "any"}],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["bots"][0]["ok"] is False


def test_github_onboard_collision_gate_409(app, fake_bot):
    """Repo exists, no evolve deploy key, no reuse_confirmed → 409."""
    import urllib.request

    class FakeResp:
        def __init__(self, status, body, headers):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if "/repos/" in url and "/keys" in url:
            return FakeResp(200, [{"title": "some-other-key", "key": "ssh-rsa AAA..."}], {})
        if "/repos/" in url:
            return FakeResp(200, {
                "html_url": "https://github.com/evolve-ops/team_bot_a-workspace",
                "pushed_at": "2024-01-01T00:00:00Z",
            }, {})
        return FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    # Pubkey generation needs to succeed for the preflight comparison.
    pubkey = "ssh-ed25519 AAAA-evolve-pubkey"
    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_real", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace", "reuse_confirmed": False}],
            })
            assert resp.status_code == 409
            body = resp.get_json()
            assert "unresolved" in body
            assert body["unresolved"][0]["bot"] == "team_bot_a"
            assert body["unresolved"][0]["repo"] == "team_bot_a-workspace"


def test_github_onboard_reuses_when_evolve_pubkey_present(app, fake_bot):
    """Repo exists AND has our evolve deploy key → succeeds without reuse_confirmed."""
    pubkey = "ssh-ed25519 AAAAtest-pubkey-content evolve-team_bot_a"
    pubkey_blob = " ".join(pubkey.split()[:2])

    class FakeResp:
        def __init__(self, status, body, headers=None):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers or {}
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if "/repos/" in url and "/keys" in url:
            # Return an existing key matching our pubkey blob → has_evolve_pubkey=True
            return FakeResp(200, [{"title": "evolve-team_bot_a", "key": pubkey_blob}], {})
        if "/repos/" in url:
            return FakeResp(200, {"html_url": "https://github.com/evolve-ops/team_bot_a-workspace"}, {})
        return FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_real", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace", "reuse_confirmed": False}],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["results"][0]["ok"] is True
            assert body["results"][0]["repo_reused"] is True


def test_github_onboard_creates_repo_when_404(app, fake_bot, tmp_path):
    """Repo doesn't exist → POST /user/repos creates it; deploy key registered."""
    pubkey = "ssh-ed25519 AAAAcreate-pubkey-content evolve-team_bot_a"
    state = {"create_called": False, "key_added": False}

    class FakeResp:
        def __init__(self, status, body, headers=None):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers or {}
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        if "/user/repos" in url and method == "POST":
            state["create_called"] = True
            return FakeResp(201, {"name": "team_bot_a-workspace"}, {})
        if "/repos/" in url and "/keys" in url and method == "GET":
            return FakeResp(200, [], {})  # no deploy keys yet
        if "/repos/" in url and "/keys" in url and method == "POST":
            state["key_added"] = True
            return FakeResp(201, {"id": 123}, {})
        if "/repos/" in url and method == "GET":
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_real", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace", "reuse_confirmed": False}],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["results"][0]["ok"] is True
            assert state["create_called"] is True
            assert state["key_added"] is True


def test_github_onboard_per_bot_override_routes_alt_login(app, fake_bot, tmp_path):
    """With an override on team_bot_a, the per-bot loop uses the override creds."""
    seen_logins = []
    pubkey = "ssh-ed25519 AAAAoverride-pubkey evolve-team_bot_a"

    class FakeResp:
        def __init__(self, status, body, headers=None):
            self.status = status
            self._body = json.dumps(body).encode() if body is not None else b""
            self.headers = headers or {}
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        # Capture which login the request was made against
        if "/repos/" in url:
            for candidate in ("cjalden", "team_bot_a-special"):
                if f"/repos/{candidate}/" in url:
                    seen_logins.append(candidate)
                    break
        if "/user/repos" in url and method == "POST":
            return FakeResp(201, {"name": "team_bot_a-workspace"}, {})
        if "/repos/" in url and "/keys" in url and method == "GET":
            return FakeResp(200, [], {})
        if "/repos/" in url and "/keys" in url and method == "POST":
            return FakeResp(201, {"id": 7}, {})
        if "/repos/" in url and method == "GET":
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        # /user
        return FakeResp(200, {"login": "team_bot_a-special"}, {"X-OAuth-Scopes": "repo"})

    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_default", "github_login": "cjalden"},
                "bots": [{
                    "bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace",
                    "reuse_confirmed": False,
                    "override": {"token": "ghp_alt", "github_login": "team_bot_a-special"},
                }],
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["results"][0]["ok"] is True
            # Every /repos/ request must have hit the override login, never the default
            assert all(login == "team_bot_a-special" for login in seen_logins), seen_logins
            assert "team_bot_a-special" in seen_logins


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot github_login (feature 2026-05-04-002): point a bot at a non-default
# org without supplying a per-bot PAT — the default token must still be used,
# but the /repos/{login}/{repo} probe goes to the entry-level login.
# ─────────────────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = json.dumps(body).encode() if body is not None else b""
        self.headers = headers or {}
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_github_verify_entry_login_routes_probe_and_succeeds(app, fake_bot):
    """A multi-org PAT (token owner=cjalden, has access to evolve-ops via
    /user/orgs) verifies green when the bot's entry.github_login points at
    evolve-ops. Spec acceptance §1: no per-bot PAT override required, no
    spurious owner-mismatch error.
    """
    seen_repo_logins = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if "/repos/" in url:
            for owner in ("cjalden", "evolve-ops"):
                if f"/repos/{owner}/" in url:
                    seen_repo_logins.append(owner)
                    break
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if url.endswith("/user/orgs"):
            return _FakeResp(200, [{"login": "evolve-ops"}, {"login": "other-org"}], {})
        # /user
        return _FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    with patch("urllib.request.urlopen", fake_urlopen):
        with app.test_client() as c:
            body = c.post("/api/admin/onboard/github/verify", json={
                "default": {"token": "ghp_multi_org", "github_login": "cjalden"},
                "bots": [{
                    "bot_id": "team_bot_a",
                    "repo_name": "team_bot_a-workspace",
                    "github_login": "evolve-ops",
                }],
            }).get_json()
    # Probe routes to the per-bot org, not the cascade default — KR2 acceptance.
    assert "evolve-ops" in seen_repo_logins
    assert "cjalden" not in seen_repo_logins
    # No spurious "token belongs to X" failure — KR3 acceptance.
    assert body["bots"][0]["ok"] is True
    assert body["bots"][0]["login"] == "evolve-ops"
    # Token's actual owner is still surfaced for UI hints.
    assert body["bots"][0]["actual_login"] == "cjalden"


def test_github_verify_returns_available_orgs(app, fake_bot):
    """/verify enumerates the PAT's orgs so the modal can refresh its dropdown
    after the operator pastes a different PAT than the cascade-discovered one."""

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if url.endswith("/user/orgs"):
            return _FakeResp(200, [{"login": "evolve-ops"}, {"login": "anthropic"}], {})
        if "/repos/" in url:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    with patch("urllib.request.urlopen", fake_urlopen):
        with app.test_client() as c:
            body = c.post("/api/admin/onboard/github/verify", json={
                "default": {"token": "ghp_test", "github_login": "cjalden"},
                "bots": [{"bot_id": "team_bot_a", "repo_name": "team_bot_a-workspace"}],
            }).get_json()
    logins = [o["login"] for o in body.get("available_orgs") or []]
    assert "cjalden" in logins
    assert "evolve-ops" in logins
    assert "anthropic" in logins
    # Source tags let the frontend label entries.
    types_by_login = {o["login"]: o["source"] for o in body["available_orgs"]}
    assert types_by_login["cjalden"] == "pat_user"
    assert types_by_login["evolve-ops"] == "pat_orgs"


def test_github_onboard_entry_login_routes_to_alt_org(app, fake_bot):
    """entry.github_login (no per-bot PAT override) sends the create + collision
    probe to the entry-level org, not the cascade default — KR4."""
    seen_logins = []
    pubkey = "ssh-ed25519 AAAA-multi-org-pubkey evolve-team_bot_a"

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        if "/repos/" in url:
            for candidate in ("cjalden", "evolve-ops"):
                if f"/repos/{candidate}/" in url:
                    seen_logins.append(candidate)
                    break
        if "/user/repos" in url and method == "POST":
            return _FakeResp(201, {"name": "team_bot_a-workspace"}, {})
        if "/repos/" in url and "/keys" in url and method == "GET":
            return _FakeResp(200, [], {})
        if "/repos/" in url and "/keys" in url and method == "POST":
            return _FakeResp(201, {"id": 1}, {})
        if "/repos/" in url and method == "GET":
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    from evolve_admin.web import server as srv
    with patch("urllib.request.urlopen", fake_urlopen), \
         patch.object(srv, "_import_analyzer") as imp_mock, \
         patch("evolve_admin.backup_keys.read_canonical_pubkey", return_value=pubkey):
        imp_mock.return_value.generate_ssh_deploy_key = lambda b: pubkey
        with app.test_client() as c:
            resp = c.post("/api/admin/onboard/github", json={
                "default": {"token": "ghp_multi_org", "github_login": "cjalden"},
                "bots": [{
                    "bot_id": "team_bot_a",
                    "repo_name": "team_bot_a-workspace",
                    "github_login": "evolve-ops",
                    "reuse_confirmed": False,
                }],
            })
            assert resp.status_code == 200
    assert "evolve-ops" in seen_logins
    assert "cjalden" not in seen_logins


def test_discover_default_pat_includes_bot_owners(app, fake_bot):
    """discover-default-pat surfaces per-bot discovered owners so the modal
    can render the 'currently at <owner>/<repo>' line without a second call."""

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if url.endswith("/user/orgs"):
            return _FakeResp(200, [], {})
        return _FakeResp(200, {"login": "cjalden"}, {"X-OAuth-Scopes": "repo"})

    with patch("urllib.request.urlopen", fake_urlopen):
        with app.test_client() as c:
            body = c.get("/api/admin/onboard/github/discover-default-pat").get_json()
    bot_owners = body.get("bot_owners") or {}
    assert "team_bot_a" in bot_owners
    assert bot_owners["team_bot_a"]["owner"] == "cjalden"
    assert bot_owners["team_bot_a"]["repo"] == "team_bot_a-evolve"
    assert bot_owners["team_bot_a"]["auth_type"] == "https_pat"


def test_discover_default_pat_includes_ssh_bot_owners(app, fake_bot):
    """SSH-based bots (no embedded PAT) still surface their discovered owner —
    a regression guard for the _discover_github_remote refactor."""
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n\turl = git@github.com:evolve-ops/team_bot_a-workspace.git\n'
    )

    def fake_urlopen(req, timeout=None):
        # No PAT in any .git/config → cascade returns None → no /user call.
        # Defensive default in case anything else fires.
        return _FakeResp(200, {}, {})

    with patch("urllib.request.urlopen", fake_urlopen):
        with app.test_client() as c:
            body = c.get("/api/admin/onboard/github/discover-default-pat").get_json()
    assert body["nonce"] is None  # no PAT discoverable
    bot_owners = body.get("bot_owners") or {}
    assert bot_owners["team_bot_a"]["owner"] == "evolve-ops"
    assert bot_owners["team_bot_a"]["auth_type"] == "ssh"
    # SSH-only owner is still in available_orgs (source: discovered_from_bot).
    sources_by_login = {o["login"]: o["source"] for o in body.get("available_orgs") or []}
    assert sources_by_login.get("evolve-ops") == "discovered_from_bot"


# ─────────────────────────────────────────────────────────────────────────────
# /api/admin/integration-token/<bot>/github/rotate — per auth_type behavior.
# These are the regression guard for "no silent no-op buttons": every accepted
# auth_type must produce a verifiable side effect, and unsupported types must
# return a clear error rather than a vacuous ok=True.
# ─────────────────────────────────────────────────────────────────────────────


def test_rotate_github_https_pat_replaces_token_in_url(app, fake_bot):
    """Existing https_pat path: old token in URL is replaced by the new one."""
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/github/rotate",
            json={"key_value": "ghp_NEWtoken12345"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["auth_type_before"] == "https_pat"
        assert body["auth_type_after"] == "https_pat"
    cfg = fake_bot["git_config_path"].read_text()
    assert "ghp_NEWtoken12345@github.com" in cfg
    assert "ghp_oldtoken@github.com" not in cfg


def test_rotate_github_credhelper_injects_token_into_plain_url(app, fake_bot):
    """Plain-HTTPS bot: rotating injects the PAT into the URL and reports the
    auth-model switch (https_credhelper -> https_pat). This is the change that
    eliminates the silent no-op for bots like Admin_bot."""
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n\turl = https://github.com/evolve-ops/admin_bot-workspace.git\n'
    )
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/github/rotate",
            json={"key_value": "ghp_brand_new_pat_xyz"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["auth_type_before"] == "https_credhelper"
        assert body["auth_type_after"] == "https_pat"
    cfg = fake_bot["git_config_path"].read_text()
    assert "url = https://ghp_brand_new_pat_xyz@github.com/evolve-ops/admin_bot-workspace.git" in cfg


def test_rotate_github_ssh_returns_clear_error(app, fake_bot):
    """SSH bots: rotation isn't supported through this UI. Must 400 with an
    actionable error message — never silently succeed."""
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n\turl = git@github.com:cjalden/team_bot_a-workspace.git\n'
    )
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/github/rotate",
            json={"key_value": "ghp_irrelevant"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False
        assert body["auth_type"] == "ssh"
        assert "SSH" in body["error"]
        # Operator should be able to find the relevant local file from the message.
        assert "evolve-backup" in body["error"]
    # And critically — .git/config must be untouched.
    cfg = fake_bot["git_config_path"].read_text()
    assert "git@github.com:cjalden/team_bot_a-workspace.git" in cfg
    assert "ghp_irrelevant" not in cfg


def test_rotate_github_no_remote_returns_404(app, fake_bot):
    """No github remote at all: return 404 rather than ok=True."""
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
    )
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/github/rotate",
            json={"key_value": "ghp_test"},
        )
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Direct unit tests for module-level helpers (lifted out of
# _register_admin_routes closure so tests can import them without spinning up
# a Flask test client). Coverage here complements — does not replace — the
# API tests above, which exercise these helpers through their HTTP routes.
# ─────────────────────────────────────────────────────────────────────────────


def _write_network(tmp_path, bot_id, user=None):
    """Helper: minimal network.json mapping bot_id → user (default = bot_id)."""
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "bots": {bot_id: {"user": user or bot_id}},
    }))
    return network_path


# ── _mask_key ───────────────────────────────────────────────────────────────


def test_mask_key_redacts_middle_of_long_value():
    masked = _mask_key("ghp_abcdefghijklmnop")  # 20 chars
    assert masked == "ghp_abcd...mnop"
    # The full token must not appear in the masked output.
    assert "ghp_abcdefghijklmnop" not in masked


def test_mask_key_returns_short_value_unchanged():
    # Anything shorter than 13 chars is returned as-is — the redaction only
    # kicks in for keys long enough to retain useful prefix/suffix detail.
    assert _mask_key("short") == "short"
    assert _mask_key("12345678901") == "12345678901"  # 11 chars


def test_mask_key_handles_empty_or_none():
    assert _mask_key("") == "—"
    assert _mask_key(None) == "—"


# ── _resolve_bot_user ───────────────────────────────────────────────────────────────


def test_bot_user_resolves_mapped_username(tmp_path):
    network_path = _write_network(tmp_path, "team_bot_a", user="actual_user_x")
    assert _resolve_bot_user("team_bot_a", network_path) == "actual_user_x"


def test_bot_user_falls_back_to_bot_id_when_unmapped(tmp_path):
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}}))
    assert _resolve_bot_user("orphan_bot", network_path) == "orphan_bot"


def test_bot_user_falls_back_when_network_path_missing(tmp_path):
    nonexistent = tmp_path / "does-not-exist.json"
    # load_network raises on missing file; the helper must swallow and
    # default to bot_id rather than propagating the exception to callers.
    assert _resolve_bot_user("any_bot", nonexistent) == "any_bot"


# ── _read_oc_json ───────────────────────────────────────────────────────────


def test_read_oc_json_returns_parsed_dict(tmp_path, fake_bot):
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    cfg = _read_oc_json(fake_bot["bot_id"], network_path)
    # fake_bot seeds channels.telegram.botToken — confirms the JSON parsed,
    # not just that we got back a non-empty dict.
    assert cfg["channels"]["telegram"]["botToken"] == "old-tg"


def test_read_oc_json_returns_empty_dict_when_file_missing(tmp_path, fake_bot):
    fake_bot["oc_path"].unlink()
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    # Direct read fails (file gone), sudo /bin/cat fallback (mocked by
    # fake_bot.fake_run) also fails on missing file → empty dict.
    assert _read_oc_json(fake_bot["bot_id"], network_path) == {}


# ── _write_oc_json ──────────────────────────────────────────────────────────


def test_write_oc_json_persists_data(tmp_path, fake_bot):
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    new_data = {"agents": {"defaults": {}}, "marker": "rewritten"}
    assert _write_oc_json(fake_bot["bot_id"], new_data, network_path) is True
    on_disk = json.loads(fake_bot["oc_path"].read_text())
    assert on_disk["marker"] == "rewritten"
    # Old keys (channels.telegram.botToken from the fixture) are gone — write
    # is a full replacement, not a merge. Documenting the contract.
    assert "channels" not in on_disk


# ── _discover_github_remote ─────────────────────────────────────────────────


def test_discover_github_remote_https_with_pat(tmp_path, fake_bot):
    # fake_bot's default .git/config: https://ghp_oldtoken@github.com/evolve-ops/team_bot_a-evolve.git
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    info = _discover_github_remote(fake_bot["bot_id"], network_path)
    assert info == {
        "auth_type": "https_pat",
        "token": "ghp_oldtoken",
        "repo_slug": "cjalden/team_bot_a-evolve",
    }


def test_discover_github_remote_https_credhelper(tmp_path, fake_bot):
    fake_bot["git_config_path"].write_text(
        '[remote "origin"]\n\turl = https://github.com/evolve-ops/admin_bot-workspace.git\n'
    )
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    info = _discover_github_remote(fake_bot["bot_id"], network_path)
    assert info == {
        "auth_type": "https_credhelper",
        "repo_slug": "cjalden/admin_bot-workspace",
    }


def test_discover_github_remote_https_pat_with_username_prefix(tmp_path, fake_bot):
    """`https://login:ghp_token@github.com/...` form — the username prefix
    must be stripped, only the token captured."""
    fake_bot["git_config_path"].write_text(
        '[remote "origin"]\n\turl = '
        'https://cjalden:ghp_user_token@github.com/evolve-ops/team_bot_a-evolve.git\n'
    )
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    info = _discover_github_remote(fake_bot["bot_id"], network_path)
    assert info["auth_type"] == "https_pat"
    assert info["token"] == "ghp_user_token"
    assert info["repo_slug"] == "cjalden/team_bot_a-evolve"


def test_discover_github_remote_ssh(tmp_path, fake_bot):
    fake_bot["git_config_path"].write_text(
        '[remote "origin"]\n\turl = git@github.com:cjalden/team_bot_a-workspace.git\n'
    )
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    info = _discover_github_remote(fake_bot["bot_id"], network_path)
    assert info["auth_type"] == "ssh"
    assert info["repo_slug"] == "cjalden/team_bot_a-workspace"
    # ssh_key_path is None unless the deploy key actually exists on disk —
    # we don't create one in the fake bot, so it must be None.
    assert info["ssh_key_path"] is None


def test_discover_github_remote_returns_none_for_no_github_url(tmp_path, fake_bot):
    fake_bot["git_config_path"].write_text(
        '[core]\n\trepositoryformatversion = 0\n'
    )
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    assert _discover_github_remote(fake_bot["bot_id"], network_path) is None


def test_discover_github_remote_strips_trailing_dotgit(tmp_path, fake_bot):
    """Repo URLs may or may not include the `.git` suffix; the parsed
    repo_slug must not."""
    fake_bot["git_config_path"].write_text(
        '[remote "origin"]\n\turl = https://github.com/evolve-ops/no-dotgit-suffix\n'
    )
    network_path = _write_network(tmp_path, fake_bot["bot_id"])
    info = _discover_github_remote(fake_bot["bot_id"], network_path)
    assert info["repo_slug"] == "cjalden/no-dotgit-suffix"
