"""tests/test_keys_rotation.py — per-field key rotation + openclaw.json mirror.

Covers:
  - `_apply_credential_to_oc_dict()` registry semantics (telegram, slack, discord, brave)
  - Round-trip through the rotate endpoint with a fake bot tmpdir
  - 400 when `field_key` is missing for a token_pair provider (KR5)
  - Token_pair rotate updates auth-profiles AND mirrors to openclaw.json (KR6)
  - Per-field rollback restores only the rotated field (KR7)
  - Response includes `requires_restart` + `restart_endpoint` (KR8)

The Flask routes are exercised via the test client with `subprocess.run` patched
so `sudo /bin/cat` / `sudo /bin/cp` become plain file ops on the tmpdir.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# _apply_credential_to_oc_dict — pure function tests
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_telegram_bot_token_creates_path():
    from evolve_admin.web.server import _apply_credential_to_oc_dict

    cfg: dict = {}
    applied = _apply_credential_to_oc_dict(cfg, "telegram", "bot_token", "tok-123")
    assert applied is True
    assert cfg == {"channels": {"telegram": {"botToken": "tok-123"}}}


def test_apply_slack_bot_token_overwrites_existing():
    from evolve_admin.web.server import _apply_credential_to_oc_dict

    cfg = {"channels": {"slack": {"botToken": "old", "userToken": "stays"}}}
    applied = _apply_credential_to_oc_dict(cfg, "slack", "bot_token", "new")
    assert applied is True
    assert cfg["channels"]["slack"]["botToken"] == "new"
    # Sibling keys preserved
    assert cfg["channels"]["slack"]["userToken"] == "stays"


def test_apply_brave_api_key_deep_path():
    from evolve_admin.web.server import _apply_credential_to_oc_dict

    cfg: dict = {}
    applied = _apply_credential_to_oc_dict(cfg, "brave", "api_key", "BSAabc")
    assert applied is True
    assert cfg["plugins"]["entries"]["brave"]["config"]["webSearch"]["apiKey"] == "BSAabc"


def test_apply_discord_no_longer_in_runtime_mirror():
    """Discord credentials live entirely in openclaw.json — they're handled
    by the dedicated /api/admin/integration-token/<bot>/discord/rotate route
    that writes channels.discord.token directly. The earlier _RUNTIME_MIRROR_PATH
    entry pointed at channels.discord.botToken, which openclaw's strict schema
    rejects. Removing it prevents future regressions where someone wires a
    Discord rotation through the generic auth-profiles flow.
    """
    from evolve_admin.web.server import _apply_credential_to_oc_dict, _RUNTIME_MIRROR_PATH

    assert "discord" not in _RUNTIME_MIRROR_PATH
    cfg: dict = {}
    applied = _apply_credential_to_oc_dict(cfg, "discord", "bot_token", "dctok")
    assert applied is False
    assert cfg == {}


def test_apply_unknown_provider_is_noop():
    from evolve_admin.web.server import _apply_credential_to_oc_dict

    cfg: dict = {}
    applied = _apply_credential_to_oc_dict(cfg, "unknown", "bot_token", "x")
    assert applied is False
    assert cfg == {}


def test_apply_unknown_field_key_is_noop():
    from evolve_admin.web.server import _apply_credential_to_oc_dict

    cfg: dict = {}
    # telegram registry only has bot_token; chat_id has no mirror entry.
    applied = _apply_credential_to_oc_dict(cfg, "telegram", "chat_id", "12345")
    assert applied is False
    assert cfg == {}


def test_apply_is_idempotent():
    from evolve_admin.web.server import _apply_credential_to_oc_dict

    cfg = {"channels": {"telegram": {"botToken": "v1"}}}
    snapshot = json.dumps(cfg, sort_keys=True)
    _apply_credential_to_oc_dict(cfg, "telegram", "bot_token", "v1")
    assert json.dumps(cfg, sort_keys=True) == snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Flask route tests — fake bot tmpdir + patched subprocess
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_bot(tmp_path, monkeypatch):
    """Create a fake bot home tree and patch resolve_bot_paths + subprocess."""
    bot_id = "team_bot_a"
    bot_home = tmp_path / "Users" / bot_id
    oc_dir = bot_home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)

    oc_path = oc_dir / "openclaw.json"
    oc_path.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(workspace)}},
        "channels": {"telegram": {"enabled": True, "botToken": "old-tg-token"}},
        "plugins": {"entries": {"brave": {"config": {"webSearch": {"apiKey": "old-brave"}}}}},
    }, indent=2))

    auth_path = agent_dir / "auth-profiles.json"
    auth_path.write_text(json.dumps({
        "profiles": {
            "telegram_token_pair": {
                "provider": "telegram",
                "type": "token_pair",
                "bot_token": "old-tg-token",
                "chat_id": "123456",
            },
            "slack_token_pair": {
                "provider": "slack",
                "type": "token_pair",
                "bot_token": "xoxb-old",
                "app_token": "xapp-old",
            },
            "brave_api_key": {
                "provider": "brave",
                "type": "api_key",
                "key": "old-brave",
            },
        },
    }, indent=2))

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

    # Replace `sudo /bin/cat` / `sudo /bin/cp` / `sudo /bin/mkdir` /
    # `sudo /bin/chmod` / `sudo /usr/sbin/chown` with plain filesystem ops on
    # the tmpdir. Other subprocess calls fall through to the real subprocess.
    real_run = subprocess.run

    def fake_run(args, *a, **kw):  # noqa: D401 - test helper
        # Allow either positional `args` (list) or kwargs.
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
                    # form: sudo /bin/mkdir -p <dir>
                    target = rest[-1]
                    Path(target).mkdir(parents=True, exist_ok=True)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "/bin/chmod":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb in ("/usr/sbin/chown", "chown"):
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "find":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            except Exception as exc:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
        return real_run(args, *a, **kw)

    monkeypatch.setattr("subprocess.run", fake_run)

    return {"bot_id": bot_id, "oc_path": oc_path, "auth_path": auth_path, "paths": paths_dict}


@pytest.fixture
def app(tmp_path, fake_bot):
    from evolve_admin.web.server import create_app

    network = {"bots": {fake_bot["bot_id"]: {"user": fake_bot["bot_id"]}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def _read_auth(auth_path: Path) -> dict:
    return json.loads(auth_path.read_text())["profiles"]


def _read_oc(oc_path: Path) -> dict:
    return json.loads(oc_path.read_text())


def test_token_pair_rotate_without_field_key_returns_400(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/telegram/rotate",
            json={"key_value": "new-tg", "profile_id": "telegram_token_pair"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "field_key" in body["error"]
        assert "valid_fields" in body
        assert "bot_token" in body["valid_fields"]


def test_token_pair_rotate_with_field_key_updates_both_stores(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/telegram/rotate",
            json={
                "key_value": "new-tg-token",
                "profile_id": "telegram_token_pair",
                "field_key": "bot_token",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["field_key"] == "bot_token"
        assert body["mirrored"] is True
        assert body["requires_restart"] is True
        assert body["restart_endpoint"] == f"/api/admin/gateway/{fake_bot['bot_id']}/restart"

        profiles = _read_auth(fake_bot["auth_path"])
        tg = profiles["telegram_token_pair"]
        assert tg["bot_token"] == "new-tg-token"
        assert tg["_evolve_prev_bot_token"] == "old-tg-token"
        # chat_id is untouched
        assert tg["chat_id"] == "123456"

        oc = _read_oc(fake_bot["oc_path"])
        assert oc["channels"]["telegram"]["botToken"] == "new-tg-token"


def test_token_pair_rotate_unknown_field_key_returns_400(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/slack/rotate",
            json={
                "key_value": "x",
                "profile_id": "slack_token_pair",
                "field_key": "not_a_field",
            },
        )
        assert resp.status_code == 400


def test_per_field_rollback_restores_only_rotated_field(app, fake_bot):
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        # First rotate slack bot_token (app_token is untouched)
        r1 = c.post(
            f"/api/admin/keys/{bot_id}/slack/rotate",
            json={
                "key_value": "xoxb-NEW",
                "profile_id": "slack_token_pair",
                "field_key": "bot_token",
            },
        )
        assert r1.status_code == 200

        profiles = _read_auth(fake_bot["auth_path"])
        slack = profiles["slack_token_pair"]
        assert slack["bot_token"] == "xoxb-NEW"
        assert slack["_evolve_prev_bot_token"] == "xoxb-old"
        assert slack["app_token"] == "xapp-old"
        assert "_evolve_prev_app_token" not in slack

        # Now roll back ONLY bot_token
        r2 = c.post(
            f"/api/admin/keys/{bot_id}/slack/rollback",
            json={
                "profile_id": "slack_token_pair",
                "field_key": "bot_token",
            },
        )
        assert r2.status_code == 200
        body = r2.get_json()
        assert body["ok"] is True
        assert body["field_key"] == "bot_token"

        profiles = _read_auth(fake_bot["auth_path"])
        slack = profiles["slack_token_pair"]
        assert slack["bot_token"] == "xoxb-old"
        # The swap stores the just-rotated value as the new prev
        assert slack["_evolve_prev_bot_token"] == "xoxb-NEW"
        # app_token sibling is fully untouched
        assert slack["app_token"] == "xapp-old"
        assert "_evolve_prev_app_token" not in slack


def test_rotate_rejects_kr4_placeholder_shape(app, fake_bot):
    """KR4 incident (2026-05-04): a verification-catalog probe POSTed
    `BSAxxxKR4TEST<epoch>xxxx…` to /rotate; rollback never ran, 4 bots
    spent two weeks on the placeholder. Server now refuses placeholders.

    Real-key writes still work (covered by the existing brave-rotate test).
    """
    bot_id = fake_bot["bot_id"]
    placeholders = [
        "BSAxxxKR4TEST1777866739xxxxxxxxxxxxxxxxxxxxxxx",  # the actual incident value
        "your-key-here",
        "REPLACEME-with-real-token",
        "this-is-a-PLACEHOLDER",
        "sk-ant-example-key",
        "BSA-dummy-test-fixture",
    ]
    with app.test_client() as c:
        for bad in placeholders:
            resp = c.post(
                f"/api/admin/keys/{bot_id}/brave/rotate",
                json={"value": bad, "profile_id": "brave_api_key"},
            )
            assert resp.status_code == 400, f"{bad!r} should be rejected"
            assert "placeholder" in (resp.get_json() or {}).get("error", "").lower()

        # auth-profiles + openclaw.json untouched. Profile is still
        # under its original legacy key because rejection short-circuits
        # before the rotate-time rename can fire.
        profiles = _read_auth(fake_bot["auth_path"])
        assert profiles["brave_api_key"]["key"] == "old-brave"
        assert "_evolve_prev_key" not in profiles["brave_api_key"]
        assert "brave:api_key" not in profiles


def test_add_key_rejects_placeholder_shape(app, fake_bot):
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/openai",
            json={"key_value": "sk-PLACEHOLDER", "key_type": "api_key"},
        )
        assert resp.status_code == 400
        assert "placeholder" in (resp.get_json() or {}).get("error", "").lower()


def test_brave_api_key_rotate_mirrors_to_openclaw(app, fake_bot):
    """Rotating the brave key:
      1. Updates the value
      2. Mirrors to openclaw.json
      3. Renames the profile from the legacy `brave_api_key` underscore
         shape to the canonical `brave:api_key` colon shape (rotate-as-
         migration). The response surfaces `renamed_from`/`renamed_to`
         so the UI can confirm the shape fix to the operator.
    """
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/brave/rotate",
            json={"key_value": "BSA-new", "profile_id": "brave_api_key"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["mirrored"] is True
        assert body["requires_restart"] is True
        assert body["renamed_from"] == "brave_api_key"
        assert body["renamed_to"] == "brave:api_key"
        assert body["profile_id"] == "brave:api_key"

        profiles = _read_auth(fake_bot["auth_path"])
        # Old key is gone, canonical key holds the new value + prev backup
        assert "brave_api_key" not in profiles
        assert profiles["brave:api_key"]["key"] == "BSA-new"
        assert profiles["brave:api_key"]["_evolve_prev_key"] == "old-brave"

        oc = _read_oc(fake_bot["oc_path"])
        assert oc["plugins"]["entries"]["brave"]["config"]["webSearch"]["apiKey"] == "BSA-new"


def test_brave_rotate_audit_log_credits_drift_via_oc_keys(app, fake_bot, tmp_path, monkeypatch):
    """End-to-end follow-on to PR #1738: rotating a credential that mirrors
    into openclaw.json must write an audit-log entry with the matching
    top-level oc_keys, and heal.py's drift detector must credit those keys.

    Without the writer-self-declared oc_keys, the next heal cycle after an
    operator-authorized key rotation would fire a false-positive
    `security.config_drift` alert on the bot — exactly the regression PR
    #1738 closed for model-config edits and this follow-up extends to
    credential rotations.
    """
    from datetime import datetime, timezone
    from evolve_admin.web import server as srv

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    audit_path = shared_dir / "audit-log.jsonl"

    # The production `_audit_log_entry` writes to a hardcoded
    # `/Users/Shared/evolve/audit-log.jsonl`. Redirect it at the module
    # level so the rotate route exercises the real call site (we want to
    # catch a missing oc_keys kwarg as a regression here, not stub past it)
    # while landing the entry where the heal reader can find it.
    def fake_audit(action, bot_id, details, *, oc_keys=None):
        entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "bot_id": bot_id,
            "details": details,
        }
        if oc_keys:
            entry["oc_keys"] = sorted(set(oc_keys))
        with open(audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    monkeypatch.setattr(srv, "_audit_log_entry", fake_audit)

    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/brave/rotate",
            json={"key_value": "BSA-credit-drift", "profile_id": "brave_api_key"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["mirrored"] is True

    rotate_entries = [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line and json.loads(line).get("action") == "keys.rotate"
    ]
    assert len(rotate_entries) == 1, (
        "exactly one keys.rotate audit entry should land per rotation"
    )
    assert rotate_entries[0].get("oc_keys") == ["plugins"], (
        "brave's auth_profiles rotation mirrors into "
        "plugins.entries.brave.config.webSearch.apiKey, so the audit "
        "entry must declare oc_keys=['plugins'] — otherwise heal's "
        "drift detector fires a false-positive on the next 5-minute cycle"
    )

    # And the heal-side reader credits it: a drift cycle that sees the
    # 'plugins' key shift in openclaw.json will find this entry within
    # the TTL window and suppress the alert.
    import heal
    keys = heal._get_recent_admin_ui_writes(bot_id, shared_dir)
    assert keys == {"plugins"}, (
        f"heal.py must credit operator-authorized brave rotation; "
        f"_get_recent_admin_ui_writes returned {keys}"
    )


def test_telegram_rotate_does_not_rename_token_pair_profile(app, fake_bot):
    """Rotate-time normalization is scoped to wizard-verify's
    `_LEGACY_KEY_RE` set (anthropic/openai/brave × api_key/auth_token).
    Token_pair profiles like `telegram_token_pair` legitimately use
    underscores and verify never flags them — rotate must NOT rename
    them. Regression guard for the over-broad normalize rule that an
    early draft of this PR almost shipped."""
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/telegram/rotate",
            json={
                "key_value": "new-tg-token",
                "profile_id": "telegram_token_pair",
                "field_key": "bot_token",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        # No rename surfaced — the response stays clean of the migration
        # fields when nothing was migrated.
        assert "renamed_from" not in body
        assert "renamed_to" not in body
        assert body["profile_id"] == "telegram_token_pair"

        profiles = _read_auth(fake_bot["auth_path"])
        # The legitimate underscore key still holds the entry — no
        # phantom `telegram:token_pair` was created.
        assert "telegram_token_pair" in profiles
        assert "telegram:token_pair" not in profiles


def test_rotate_skips_rename_when_canonical_collides(app, fake_bot):
    """If both the legacy underscore key AND the canonical colon key
    already exist (operator manually pre-created the canonical), rotate
    must update the targeted entry and NOT silently merge the two —
    leave the duplicate-resolution decision to the operator."""
    # Seed both shapes side-by-side
    auth = json.loads(fake_bot["auth_path"].read_text())
    auth["profiles"]["brave:api_key"] = {
        "provider": "brave", "type": "api_key", "key": "pre-existing-canonical",
    }
    fake_bot["auth_path"].write_text(json.dumps(auth, indent=2))

    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/brave/rotate",
            json={"key_value": "BSA-rotated", "profile_id": "brave_api_key"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # No rename — both shapes preserved, operator resolves manually
        assert "renamed_from" not in body
        assert body["profile_id"] == "brave_api_key"

        profiles = _read_auth(fake_bot["auth_path"])
        assert profiles["brave_api_key"]["key"] == "BSA-rotated"
        assert profiles["brave:api_key"]["key"] == "pre-existing-canonical"


# ─────────────────────────────────────────────────────────────────────────────
# Catalog-aligned API surface tests (KR1, KR2, KR3 probes)
# ─────────────────────────────────────────────────────────────────────────────


def test_per_provider_get_returns_telegram_row_with_rotatable(app, fake_bot):
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}/telegram")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["provider"] == "telegram"
        fields = {f["key"]: f for f in body["fields"]}
        # Telegram bot_token is secret + active → rotatable=True (KR1)
        assert fields["bot_token"]["rotatable"] is True
        assert fields["bot_token"]["secret"] is True
        # chat_id is not secret → rotatable=False (KR1)
        assert fields["chat_id"]["rotatable"] is False
        assert fields["chat_id"]["secret"] is False


def test_per_provider_get_exposes_chat_id_value_not_secret(app, fake_bot):
    """KR7 reads field.value to confirm chat_id wasn't clobbered by a bot_token rotation."""
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}/telegram")
        body = resp.get_json()
        fields = {f["key"]: f for f in body["fields"]}
        assert fields["chat_id"]["value"] == "123456"
        # Secret field's raw value must NOT be exposed as `value`.
        assert fields["bot_token"].get("value") is None


def test_per_provider_get_returns_404_for_unknown_provider(app, fake_bot):
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{fake_bot['bot_id']}/notarealprovider")
        assert resp.status_code == 404


def test_slack_rotatable_only_on_secret_active_fields(app, fake_bot):
    """KR2 — Slack bot_token + app_token rotatable; user_token not rotatable
    when not configured (status=missing makes rotatable=False)."""
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}/slack")
        body = resp.get_json()
        fields = {f["key"]: f for f in body["fields"]}
        assert fields["bot_token"]["rotatable"] is True
        assert fields["app_token"]["rotatable"] is True
        # user_token is missing in the fixture — rotatable False because not active.
        assert fields["user_token"]["rotatable"] is False


def test_rotate_accepts_value_alias(app, fake_bot):
    """The catalog probes POST `{"value": ...}` instead of `{"key_value": ...}`."""
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/telegram/rotate",
            json={"value": "tg-via-value-alias", "field_key": "bot_token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True

        profiles = _read_auth(fake_bot["auth_path"])
        assert profiles["telegram_token_pair"]["bot_token"] == "tg-via-value-alias"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.5: storage="openclaw_channels" — rotation for bots whose Telegram
# token lives only in openclaw.json. Covers the live-pod case (4 of 5
# Telegram-using bots have this shape).
# ─────────────────────────────────────────────────────────────────────────────


def test_storage_openclaw_channels_writes_to_oc_json_only(app, fake_bot):
    """Posting `storage=openclaw_channels` updates `channels.telegram.botToken`
    and intentionally leaves auth-profiles.json untouched. Decision B: write
    where the runtime reads — adding a stale auth-profiles entry would let a
    future routine-rotate clobber the operator's value via the mirror.
    """
    bot_id = fake_bot["bot_id"]
    auth_before = _read_auth(fake_bot["auth_path"])
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/telegram/rotate",
            json={
                "value": "tg-via-oc-channels",
                "field_key": "bot_token",
                "storage": "openclaw_channels",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["storage"] == "openclaw_channels"
        assert body["oc_field"] == "botToken"
        assert body["requires_restart"] is True
        assert body["restart_endpoint"] == f"/api/admin/gateway/{bot_id}/restart"

    oc = _read_oc(fake_bot["oc_path"])
    assert oc["channels"]["telegram"]["botToken"] == "tg-via-oc-channels"
    # `enabled` and any other sibling keys are preserved.
    assert oc["channels"]["telegram"]["enabled"] is True

    # auth-profiles.json must be byte-for-byte unchanged. This is the
    # decision-B invariant for the openclaw_channels path.
    assert _read_auth(fake_bot["auth_path"]) == auth_before


def test_storage_openclaw_channels_creates_block_when_absent(tmp_path, monkeypatch):
    """No `channels.telegram` at all on disk yet — rotation should create the
    block rather than 500. Covers a freshly-deployed bot that hasn't had
    openclaw.json hand-edited but still wants the dashboard to onboard
    Telegram in-place.
    """
    bot_id = "fresh_bot"
    bot_home = tmp_path / "Users" / bot_id
    oc_dir = bot_home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)
    oc_path = oc_dir / "openclaw.json"
    oc_path.write_text(json.dumps(
        {"agents": {"defaults": {"workspace": str(workspace)}}}, indent=2,
    ))
    auth_path = agent_dir / "auth-profiles.json"
    auth_path.write_text(json.dumps({"profiles": {}}, indent=2))

    paths_dict = {
        "oc_config": str(oc_path), "workspace": str(workspace),
        "agent_dir": str(agent_dir), "auth_profiles": str(auth_path),
        "turns_dir": str(workspace / "turns"),
        "turns_dir_fallback": str(workspace / "turns"),
        "turns_dir_candidates": [str(workspace / "turns")],
        "logs_dir": str(oc_dir / "logs"), "user": bot_id,
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
                    return subprocess.CompletedProcess(args, 0, stdout=Path(rest[0]).read_text(), stderr="")
                if verb == "/bin/cp":
                    Path(rest[1]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(rest[0], rest[1])
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb in ("/bin/mkdir", "/bin/chmod", "/usr/sbin/chown", "chown"):
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            except Exception as exc:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
        return real_run(args, *a, **kw)

    monkeypatch.setattr("subprocess.run", fake_run)

    network = {"bots": {bot_id: {"user": bot_id}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    from evolve_admin.web.server import create_app
    app = create_app(network_path)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/telegram/rotate",
            json={
                "value": "fresh-tg",
                "field_key": "bot_token",
                "storage": "openclaw_channels",
            },
        )
        assert resp.status_code == 200

    oc = json.loads(oc_path.read_text())
    assert oc["channels"]["telegram"]["botToken"] == "fresh-tg"


def test_storage_openclaw_channels_rejects_chat_id(app, fake_bot):
    """chat_id is NOT a rotatable secret in any storage — only the secret
    fields (botToken / appToken / userToken) accept openclaw_channels writes.
    """
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/telegram/rotate",
            json={
                "value": "78910",
                "field_key": "chat_id",
                "storage": "openclaw_channels",
            },
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "chat_id" in body["error"]
        assert "bot_token" in body["valid_fields"]


def test_storage_openclaw_channels_requires_field_key(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/telegram/rotate",
            json={"value": "tok", "storage": "openclaw_channels"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "field_key" in body["error"]


def test_storage_openclaw_channels_unsupported_provider(app, fake_bot):
    """Discord rotation has its own dedicated route (channels.discord.token);
    the generic keys/rotate path with storage=openclaw_channels should refuse
    discord rather than silently writing to a wrong-shape JSON path.
    """
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/discord/rotate",
            json={
                "value": "x",
                "field_key": "bot_token",
                "storage": "openclaw_channels",
            },
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "discord" in body["error"]
        # telegram and slack are the supported providers for this storage.
        assert set(body["valid_providers"]) == {"telegram", "slack"}


def test_storage_unsupported_value_returns_400(app, fake_bot):
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/telegram/rotate",
            json={
                "value": "x",
                "field_key": "bot_token",
                "storage": "made_up_store",
            },
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "made_up_store" in body["error"]
        # Each shipped storage shape must show up in the error so the
        # operator's tooling can self-diagnose.
        assert set(body["valid_storage"]) == {"auth_profiles", "openclaw_channels", "dotenv"}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.5 follow-up: storage="dotenv" — rotation for bots whose Slack /
# Telegram / Discord token lives only in `~/.openclaw/workspace/.env` (team_bot_a's
# shape). Resolves the deferral noted in #724.
# ─────────────────────────────────────────────────────────────────────────────


def test_rewrite_workspace_dotenv_value_replaces_only_target_line():
    from evolve_admin.web.server import _rewrite_workspace_dotenv_value

    existing = (
        "# leading comment\n"
        "DATABASE_PASSWORD=should-stay-untouched\n"
        'SLACK_BOT_TOKEN="xoxb-old-1234"\n'
        "OTHER=irrelevant\n"
    )
    new_text, err = _rewrite_workspace_dotenv_value(
        existing, "SLACK_BOT_TOKEN", "xoxb-NEW-9999",
    )
    assert err is None
    # Quote style preserved, target line rewritten, every other line byte-
    # for-byte identical.
    assert new_text == (
        "# leading comment\n"
        "DATABASE_PASSWORD=should-stay-untouched\n"
        'SLACK_BOT_TOKEN="xoxb-NEW-9999"\n'
        "OTHER=irrelevant\n"
    )


def test_rewrite_workspace_dotenv_value_preserves_export_prefix_and_unquoted():
    from evolve_admin.web.server import _rewrite_workspace_dotenv_value

    existing = "export TELEGRAM_BOT_TOKEN=12345:abcdef\nUNRELATED=x\n"
    new_text, err = _rewrite_workspace_dotenv_value(
        existing, "TELEGRAM_BOT_TOKEN", "67890:zzz",
    )
    assert err is None
    assert new_text == "export TELEGRAM_BOT_TOKEN=67890:zzz\nUNRELATED=x\n"


def test_rewrite_workspace_dotenv_value_idempotent():
    from evolve_admin.web.server import _rewrite_workspace_dotenv_value

    existing = 'DISCORD_BOT_TOKEN="mfa.same-value"\n'
    new_text, err = _rewrite_workspace_dotenv_value(
        existing, "DISCORD_BOT_TOKEN", "mfa.same-value",
    )
    assert err is None
    assert new_text == existing


def test_rewrite_workspace_dotenv_value_refuses_missing_var():
    """Rotation is rewrite-only — the helper never invents new lines (would
    silently widen the file's secret surface). Caller propagates this as a
    400 from the rotate endpoint."""
    from evolve_admin.web.server import _rewrite_workspace_dotenv_value

    new_text, err = _rewrite_workspace_dotenv_value(
        "OTHER=x\n", "SLACK_BOT_TOKEN", "v",
    )
    assert new_text is None
    assert err is not None and "SLACK_BOT_TOKEN" in err


def test_rewrite_workspace_dotenv_value_rejects_newline_in_value():
    """Embedded newlines would invalidate the surrounding syntax. Reject."""
    from evolve_admin.web.server import _rewrite_workspace_dotenv_value

    new_text, err = _rewrite_workspace_dotenv_value(
        "SLACK_BOT_TOKEN=v\n", "SLACK_BOT_TOKEN", "abc\ndef",
    )
    assert new_text is None
    assert err is not None and "newline" in err.lower()


def test_rewrite_workspace_dotenv_value_skips_commented_match():
    """A `# SLACK_BOT_TOKEN=` line is a comment, not an assignment — must not
    be rewritten or counted as the matching slot."""
    from evolve_admin.web.server import _rewrite_workspace_dotenv_value

    existing = "# SLACK_BOT_TOKEN=disabled-comment\nOTHER=v\n"
    new_text, err = _rewrite_workspace_dotenv_value(
        existing, "SLACK_BOT_TOKEN", "v2",
    )
    assert new_text is None
    assert err is not None


@pytest.fixture
def fake_bot_with_dotenv(tmp_path, monkeypatch, fake_bot):
    """Extend `fake_bot` with a synthetic `.env` containing both adjacent
    unrelated secrets (DATABASE_PASSWORD) and the slack token we'll rotate.

    The dotenv read+write helpers in production hardcode `/Users/<bot>/...`,
    so the test rebinds them on the server module to operate against the
    synthetic tmpdir env file. The pure rewrite helper is reused so the
    line-preservation invariants ride the same code path as production.
    """
    workspace = Path(fake_bot["paths"]["workspace"])
    env_path = workspace / ".env"
    env_path.write_text(
        "# Mixed secrets — rotation must not disturb the database password.\n"
        "DATABASE_PASSWORD=very-secret-pg-pass\n"
        'SLACK_BOT_TOKEN="xoxb-old-OLD-OLD"\n'
        "OTHER=irrelevant\n"
    )

    from evolve_admin.web import server as srv

    def _read_dotenv_synth(b: str, names: tuple[str, ...], network_path=None) -> list[str]:
        if not env_path.is_file() or not names:
            return []
        wanted = set(names)
        out: list[str] = []
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            name, _, value = line.partition("=")
            name = name.strip()
            if name not in wanted:
                continue
            v = value.strip().strip('"').strip("'")
            if v and name not in out:
                out.append(name)
        return out

    def _write_dotenv_synth(
        b: str, env_var_name: str, new_value: str, network_path=None,
    ) -> tuple[bool, str | None]:
        existing = env_path.read_text()
        new_text, err = srv._rewrite_workspace_dotenv_value(
            existing, env_var_name, new_value,
        )
        if err or new_text is None:
            return False, err
        env_path.write_text(new_text)
        return True, None

    monkeypatch.setattr(srv, "_detect_workspace_dotenv_keys", _read_dotenv_synth)
    monkeypatch.setattr(srv, "_write_workspace_dotenv_value", _write_dotenv_synth)
    return {**fake_bot, "env_path": env_path}


def test_storage_dotenv_rewrites_only_target_line(app, fake_bot_with_dotenv):
    """Posting `storage=dotenv` rewrites the matching `SLACK_BOT_TOKEN=...`
    line; every adjacent line — including unrelated secrets — is preserved
    byte-for-byte. auth-profiles.json and openclaw.json are not touched.
    """
    bot_id = fake_bot_with_dotenv["bot_id"]
    auth_before = _read_auth(fake_bot_with_dotenv["auth_path"])
    oc_before = _read_oc(fake_bot_with_dotenv["oc_path"])
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/slack/rotate",
            json={
                "value": "xoxb-NEW-FROM-DASHBOARD-999",
                "field_key": "bot_token",
                "storage": "dotenv",
            },
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["ok"] is True
        assert body["storage"] == "dotenv"
        assert body["env_var"] == "SLACK_BOT_TOKEN"
        assert body["requires_restart"] is True

    new_env = fake_bot_with_dotenv["env_path"].read_text()
    assert new_env == (
        "# Mixed secrets — rotation must not disturb the database password.\n"
        "DATABASE_PASSWORD=very-secret-pg-pass\n"
        'SLACK_BOT_TOKEN="xoxb-NEW-FROM-DASHBOARD-999"\n'
        "OTHER=irrelevant\n"
    )
    # Decision-B invariant: never touch other stores from a dotenv rotation.
    assert _read_auth(fake_bot_with_dotenv["auth_path"]) == auth_before
    assert _read_oc(fake_bot_with_dotenv["oc_path"]) == oc_before


def test_storage_dotenv_refuses_missing_env_var(app, fake_bot_with_dotenv):
    """SLACK_APP_TOKEN isn't currently in the .env. Rotation must refuse —
    the rewrite helper only updates existing lines, never invents new ones.
    """
    bot_id = fake_bot_with_dotenv["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/slack/rotate",
            json={
                "value": "xapp-cannot-be-invented",
                "field_key": "app_token",
                "storage": "dotenv",
            },
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert "SLACK_APP_TOKEN" in body["error"]


def test_storage_dotenv_unsupported_provider(app, fake_bot):
    """No bot-on-this-pod uses anthropic in a .env, but the endpoint must
    still refuse cleanly when called with a provider that has no DOTENV
    var registry entry rather than 500."""
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{fake_bot['bot_id']}/anthropic/rotate",
            json={
                "value": "x",
                "field_key": "api_key",
                "storage": "dotenv",
            },
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "dotenv" in body["error"]
        assert set(body["valid_providers"]) == {"slack", "telegram", "discord"}


def test_storage_dotenv_unknown_field_key(app, fake_bot_with_dotenv):
    """A field_key the provider doesn't support (e.g. chat_id on slack) must
    400 with the list of valid env var names so the operator's tooling can
    self-correct."""
    bot_id = fake_bot_with_dotenv["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/slack/rotate",
            json={
                "value": "x",
                "field_key": "chat_id",
                "storage": "dotenv",
            },
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "chat_id" in body["error"]
        assert "SLACK_BOT_TOKEN" in body["valid_env_vars"]


def test_storage_dotenv_requires_field_key(app, fake_bot_with_dotenv):
    bot_id = fake_bot_with_dotenv["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/slack/rotate",
            json={"value": "tok", "storage": "dotenv"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "field_key" in body["error"]


def test_storage_default_is_auth_profiles_for_back_compat(app, fake_bot):
    """Omitting `storage` keeps the legacy behavior — auth-profiles + mirror.
    This is the back-compat guarantee for older catalog probes / the existing
    rotate modal pre-Phase-2.5.
    """
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/telegram/rotate",
            json={
                "value": "tg-default-storage",
                "field_key": "bot_token",
                "profile_id": "telegram_token_pair",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["storage"] == "auth_profiles"
        # Both stores updated, prev backed up — full legacy behavior.
        profiles = _read_auth(fake_bot["auth_path"])
        assert profiles["telegram_token_pair"]["bot_token"] == "tg-default-storage"
        assert profiles["telegram_token_pair"]["_evolve_prev_bot_token"] == "old-tg-token"
        oc = _read_oc(fake_bot["oc_path"])
        assert oc["channels"]["telegram"]["botToken"] == "tg-default-storage"


def test_add_key_mirrors_to_openclaw(app, fake_bot):
    """Adding a brave key must reach the RUNTIME path, not just auth-profiles.

    Live-pod regression (2026-07-31): `+ Add` wrote auth-profiles.json only.
    The brave plugin reads
    ``plugins.entries.brave.config.webSearch.apiKey``, so the key never
    became live — while the keys API (auth-profiles first) reported ACTIVE.
    Operator saw a green row and search stayed broken. Rotate mirrored from
    the start; add did not, so the two paths produced different on-disk
    state for the same provider.
    """
    bot_id = fake_bot["bot_id"]
    # Start from a config with no brave key at all.
    oc = _read_oc(fake_bot["oc_path"])
    oc["plugins"]["entries"]["brave"]["config"]["webSearch"].pop("apiKey", None)
    fake_bot["oc_path"].write_text(json.dumps(oc, indent=2))

    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/brave",
            json={"key_value": "BSA-added", "key_type": "api_key"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["mirrored"] is True
        # A mirrored provider is not live until the gateway restarts.
        assert body["requires_restart"] is True

        # Canonical store written...
        profiles = _read_auth(fake_bot["auth_path"])
        assert profiles["brave:api_key"]["key"] == "BSA-added"
        # ...AND the runtime path the plugin actually reads.
        oc_after = _read_oc(fake_bot["oc_path"])
        runtime = oc_after["plugins"]["entries"]["brave"]["config"]["webSearch"]
        assert runtime["apiKey"] == "BSA-added"


def test_add_key_unmirrored_provider_is_not_a_failure(app, fake_bot):
    """A provider with no _RUNTIME_MIRROR_PATH entry must still succeed.

    Guards the mirror from becoming a hard dependency: openai has no
    runtime mirror path, so `mirrored` is a no-op True and no restart is
    implied. Without this, adding the mirror could have turned every
    non-mirrored add into a 500.
    """
    bot_id = fake_bot["bot_id"]
    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot_id}/openai",
            json={"key_value": "sk-proj-realish-value", "key_type": "api_key"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["mirrored"] is True      # no-op, not an error
        assert body["requires_restart"] is False
        assert _read_auth(fake_bot["auth_path"])["openai:api_key"]["key"] == "sk-proj-realish-value"
