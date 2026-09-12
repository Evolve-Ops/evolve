"""tests/test_messaging_integrations.py — Discord + WhatsApp integration_token rows.

Covers (all via the Flask test client, since discovery helpers live inside
the _register_admin_routes closure — same pattern as test_onboarding.py):
  - Keys API rows for Discord + WhatsApp surface the right `status`,
    `credential_class`, shape metadata, masked-token, guild count, etc.
  - Integration-token check routes (200 / 404 / multi-account)
  - Discord rotate route — first-time setup, replace existing, multi-account
    refusal with 400, validation-failure 400 (no openclaw write), validation
    success records identity, missing key_value 400
  - WhatsApp rotate always returns 400 with the actionable Baileys explanation
  - WhatsApp setup writes the scaffold, idempotent no-op when already at target,
    can disable
  - Regression guards: Discord row appears exactly once (not duplicated as
    both token_pair + integration_token), `_RUNTIME_MIRROR_PATH` no longer
    has the broken `discord -> botToken` entry
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


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (mirror the test_onboarding.py shape)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_bot(tmp_path, monkeypatch):
    """Single-bot fake home tree at /Users/team_bot_a/.openclaw with both
    openclaw.json and auth-profiles.json in place. Tests can mutate openclaw.json
    via `fake_bot["oc_path"].write_text(...)` to simulate different channel
    configurations.
    """
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
        "channels": {},
        "plugins": {"entries": {}},
    }, indent=2))

    auth_path = agent_dir / "auth-profiles.json"
    auth_path.write_text(json.dumps({"profiles": {}}, indent=2))

    git_config_path = git_dir / "config"
    git_config_path.write_text(
        '[core]\n\trepositoryformatversion = 0\n'
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
            verb = args[1]
            rest = list(args[2:])
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
        "paths": paths_dict,
    }


@pytest.fixture
def app(tmp_path, fake_bot):
    from evolve_admin.web.server import create_app

    network = {
        "primary": fake_bot["bot_id"],
        "bots": {fake_bot["bot_id"]: {"user": fake_bot["bot_id"]}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def _set_oc_channels(fake_bot, channels: dict) -> None:
    cfg = json.loads(fake_bot["oc_path"].read_text())
    cfg["channels"] = channels
    fake_bot["oc_path"].write_text(json.dumps(cfg, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Module-level: registry guard
# ─────────────────────────────────────────────────────────────────────────────


def test_runtime_mirror_no_longer_includes_discord():
    """Regression: removing the discord entry from _RUNTIME_MIRROR_PATH
    prevents the generic auth-profiles rotate flow from writing to the wrong
    openclaw key (channels.discord.botToken — rejected by openclaw's strict
    schema). Discord rotation now goes through its dedicated route."""
    from evolve_admin.web.server import _RUNTIME_MIRROR_PATH, _apply_credential_to_oc_dict
    assert "discord" not in _RUNTIME_MIRROR_PATH
    cfg: dict = {}
    applied = _apply_credential_to_oc_dict(cfg, "discord", "bot_token", "x")
    assert applied is False
    assert cfg == {}


# ─────────────────────────────────────────────────────────────────────────────
# Keys API rows — Discord + WhatsApp surface in the messaging category
# ─────────────────────────────────────────────────────────────────────────────


def test_keys_api_discord_missing_when_channels_absent(app, fake_bot):
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        dc = next((k for k in body["keys"] if k["provider"] == "discord"), None)
        assert dc is not None
        assert dc["status"] == "missing"
        assert dc["credential_class"] == "integration_token"
        assert dc["category"] == "messaging"
        assert dc["setup_endpoint"].endswith("/discord/rotate")


def test_keys_api_discord_active_for_single_account(app, fake_bot):
    _set_oc_channels(fake_bot, {
        "discord": {
            "enabled": True,
            "token": "MTQ3OTUxNjE5NDg3OTk2MzEzNw.G5HWwf.example_token_value",
            "guilds": {"g1": {}, "g2": {}},
        }
    })
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        dc = next((k for k in body["keys"] if k["provider"] == "discord"), None)
        assert dc["status"] == "active"
        assert dc["discord_shape"] == "single"
        assert dc["discord_enabled"] is True
        assert dc["discord_guild_count"] == 2
        # Masked, not full token.
        assert dc["masked"] and "..." in dc["masked"] and "example_token_value" not in dc["masked"]


def test_keys_api_discord_multi_account_disables_rotate(app, fake_bot):
    _set_oc_channels(fake_bot, {
        "discord": {
            "token": "def",
            "accounts": {"alt": {"token": "alt"}, "main": {"token": "main"}},
        }
    })
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        dc = next((k for k in body["keys"] if k["provider"] == "discord"), None)
        assert dc["status"] == "active"
        assert dc["discord_shape"] == "multi_account"
        assert dc["discord_accounts"] == ["alt", "main"]
        assert "2 accounts" in dc["type_label"]


def test_keys_api_whatsapp_missing_when_channels_absent(app, fake_bot):
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        wa = next((k for k in body["keys"] if k["provider"] == "whatsapp"), None)
        assert wa is not None
        assert wa["status"] == "missing"
        assert wa["credential_class"] == "integration_token"
        assert wa["category"] == "messaging"


def test_keys_api_whatsapp_active_when_enabled(app, fake_bot):
    _set_oc_channels(fake_bot, {"whatsapp": {"enabled": True}})
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        wa = next((k for k in body["keys"] if k["provider"] == "whatsapp"), None)
        assert wa["status"] == "active"
        assert wa["whatsapp_enabled"] is True


def test_keys_api_whatsapp_configured_disabled_when_present_but_disabled(app, fake_bot):
    """Placeholder shape (present but enabled=false) must surface as a distinct
    `configured_disabled` status so the frontend renders Enable instead of Set up."""
    _set_oc_channels(fake_bot, {
        "whatsapp": {"enabled": False, "dmPolicy": "pairing", "groupPolicy": "allowlist"}
    })
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        wa = next((k for k in body["keys"] if k["provider"] == "whatsapp"), None)
        assert wa["status"] == "configured_disabled"
        assert wa["whatsapp_enabled"] is False


def test_keys_api_no_longer_lists_discord_as_token_pair(app, fake_bot):
    """Regression guard: removing Discord from _KEY_REGISTRY means the row
    must come from the integration-token block, not from the seeded
    token_pair flow that reads auth-profiles.json. If Discord ever shows up
    twice (once token_pair, once integration_token) the operator gets two
    confusing rows."""
    with app.test_client() as c:
        body = c.get(f"/api/admin/keys/{fake_bot['bot_id']}").get_json()
        discord_rows = [k for k in body["keys"] if k["provider"] == "discord"]
        assert len(discord_rows) == 1
        assert discord_rows[0]["credential_class"] == "integration_token"


# ─────────────────────────────────────────────────────────────────────────────
# Discord integration-token check + rotate routes
# ─────────────────────────────────────────────────────────────────────────────


def test_discord_check_returns_404_when_missing(app, fake_bot):
    with app.test_client() as c:
        resp = c.get(f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/check")
        assert resp.status_code == 404
        assert resp.get_json()["configured"] is False


def test_discord_check_returns_200_for_single_account(app, fake_bot):
    _set_oc_channels(fake_bot, {
        "discord": {"enabled": True, "token": "MTQ3-tok-xxxxxxxx", "guilds": {}}
    })
    with app.test_client() as c:
        resp = c.get(f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/check")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["configured"] is True
        assert body["shape"] == "single"
        assert body["enabled"] is True


def test_discord_rotate_writes_token_when_missing(app, fake_bot):
    """Rotate handles the first-time-setup case identically — the operation is
    just 'write the token into openclaw.json'."""
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/rotate",
            json={"key_value": "new-bot-token", "skip_validation": True},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["ok"] is True
        assert body["was_configured_before"] is False
        assert body["openclaw_updated"] is True
        assert body["validated"] is False
    # Verify the side effect actually landed
    cfg = json.loads(fake_bot["oc_path"].read_text())
    assert cfg["channels"]["discord"]["token"] == "new-bot-token"
    assert cfg["channels"]["discord"]["enabled"] is True


def test_discord_rotate_replaces_existing_single_token(app, fake_bot):
    _set_oc_channels(fake_bot, {
        "discord": {"enabled": True, "token": "old-tok", "groupPolicy": "allowlist"}
    })
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/rotate",
            json={"key_value": "new-tok", "skip_validation": True},
        )
        assert resp.status_code == 200
        assert resp.get_json()["was_configured_before"] is True
    cfg = json.loads(fake_bot["oc_path"].read_text())
    assert cfg["channels"]["discord"]["token"] == "new-tok"
    # Sibling preserved
    assert cfg["channels"]["discord"]["groupPolicy"] == "allowlist"


def test_discord_rotate_refuses_multi_account_with_400(app, fake_bot):
    _set_oc_channels(fake_bot, {
        "discord": {
            "token": "def",
            "accounts": {"alt": {"token": "alt"}},
        }
    })
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/rotate",
            json={"key_value": "should-not-land", "skip_validation": True},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["shape"] == "multi_account"
        assert "multi-account" in body["error"] or "accounts" in body["error"]
    # Confirm we didn't rewrite anything
    cfg = json.loads(fake_bot["oc_path"].read_text())
    assert cfg["channels"]["discord"]["token"] == "def"


def test_discord_rotate_400_when_validation_fails(app, fake_bot):
    """Validation against discord.com/users/@me must run by default; if the
    token is rejected, return 400 BEFORE any write to openclaw.json. We patch
    urllib.request.urlopen (which _validate_discord_token uses internally) to
    raise the 401 — this exercises the full validation path."""
    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with patch.object(urllib.request, "urlopen", fake_urlopen):
        with app.test_client() as c:
            resp = c.post(
                f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/rotate",
                json={"key_value": "bad-token"},
            )
            assert resp.status_code == 400
            assert "401" in resp.get_json()["error"] or "Unauthorized" in resp.get_json()["error"]
    # Crucially, openclaw.json must NOT have been mutated
    cfg = json.loads(fake_bot["oc_path"].read_text())
    assert "discord" not in cfg.get("channels", {})


def test_discord_rotate_validation_success_records_identity(app, fake_bot):
    """Successful validation surfaces the bot's discord identity (id, username)
    so the operator can confirm they pasted the right bot's token."""
    import io
    import urllib.request

    fake_identity_body = json.dumps({
        "id": "12345",
        "username": "team_bot_b_bot",
        "discriminator": "0",
    }).encode("utf-8")

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return fake_identity_body

    def fake_urlopen(req, timeout=None):
        # Sanity-check that the route actually called the discord users/@me URL
        assert "discord.com" in req.full_url
        assert req.headers.get("Authorization", "").startswith("Bot ")
        return FakeResp()

    with patch.object(urllib.request, "urlopen", fake_urlopen):
        with app.test_client() as c:
            resp = c.post(
                f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/rotate",
                json={"key_value": "good-token"},
            )
            assert resp.status_code == 200, resp.get_json()
            body = resp.get_json()
            assert body["validated"] is True
            assert body["identity"]["id"] == "12345"
            assert body["identity"]["username"] == "team_bot_b_bot"


def test_discord_rotate_400_when_key_value_missing(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/discord/rotate",
            json={},
        )
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"]


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp integration-token routes — check + rotate-is-400 + setup
# ─────────────────────────────────────────────────────────────────────────────


def test_whatsapp_check_returns_404_when_missing(app, fake_bot):
    with app.test_client() as c:
        resp = c.get(f"/api/admin/integration-token/{fake_bot['bot_id']}/whatsapp/check")
        assert resp.status_code == 404


def test_whatsapp_check_returns_200_when_present(app, fake_bot):
    _set_oc_channels(fake_bot, {"whatsapp": {"enabled": True}})
    with app.test_client() as c:
        body = c.get(f"/api/admin/integration-token/{fake_bot['bot_id']}/whatsapp/check").get_json()
        assert body["configured"] is True
        assert body["enabled"] is True


def test_whatsapp_rotate_always_returns_400_with_baileys_explanation(app, fake_bot):
    """No silent no-op: WhatsApp has no rotatable token. The route must
    actively explain what to do instead of returning a fake success."""
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/whatsapp/rotate",
            json={"key_value": "ignored"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "Baileys" in body["error"]
        assert "oc whatsapp pair" in body["error"]


def test_whatsapp_setup_creates_scaffold_when_missing(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/whatsapp/setup",
            json={"enabled": True},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["openclaw_updated"] is True
        assert body["was_present_before"] is False
        assert body["enabled"] is True
        assert "oc whatsapp pair" in body["next_steps"]
    cfg = json.loads(fake_bot["oc_path"].read_text())
    assert cfg["channels"]["whatsapp"]["enabled"] is True
    assert cfg["channels"]["whatsapp"]["dmPolicy"] == "pairing"


def test_whatsapp_setup_idempotent_no_op_when_already_at_target(app, fake_bot):
    """Repeat-flip with no other change must report noop=true rather than
    claim a write happened."""
    _set_oc_channels(fake_bot, {
        "whatsapp": {"enabled": True, "dmPolicy": "pairing", "groupPolicy": "allowlist"}
    })
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/whatsapp/setup",
            json={"enabled": True},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["openclaw_updated"] is False
        assert body["noop"] is True


def test_whatsapp_setup_can_disable(app, fake_bot):
    _set_oc_channels(fake_bot, {"whatsapp": {"enabled": True}})
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/integration-token/{fake_bot['bot_id']}/whatsapp/setup",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["enabled"] is False
        assert body["openclaw_updated"] is True
    cfg = json.loads(fake_bot["oc_path"].read_text())
    assert cfg["channels"]["whatsapp"]["enabled"] is False
