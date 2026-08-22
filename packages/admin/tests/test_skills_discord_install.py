"""tests/test_skills_discord_install.py — Discord skill install flow.

Covers the contract for the V2.3-2 Discord skill:

  - resolve_status routes by (credentials present, token present, verify_token
    result) into the five-state machine.
  - build_install_plan returns the right ordered steps for each state.
  - The plain-language access panel ships with the invite step.
  - build_invite_url constructs the correct Discord authorization URL.
  - Hostile inputs: missing credentials, network errors.
  - Token write/read helpers call the right paths (no hardcoded /Users/{bot_id}).

Discord vs. Slack differences that affect these tests:
  - Discord has three keystore keys (client_id + client_secret + bot_token)
    vs. Slack's two (client_id + client_secret).
  - credentials_missing triggers when EITHER client_id OR bot_token is missing.
  - The per-bot config does NOT store the bot token (it's pod-level in keystore).
  - verify_token returns (ok, error, user_info_dict) — three values vs. Slack's two.
  - The state token store uses discord_state_* functions (not slack_state_*).

OAuth is mocked at the reader boundary — no real Discord API traffic.
No filesystem writes in tests — write_token_config is stubbed via injectable
read_token / check_token callables.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import discord_install  # noqa: E402


# ── Fixture bot-token values ──────────────────────────────────────────────────
#
# These are opaque placeholders, NOT credentials, and deliberately do NOT
# resemble a real Discord bot token (no ``<base64 id>.<crc>.<hmac>`` triple).
# ``enable_channel_in_oc_config`` is a pure pass-through — it assigns
# ``dc["token"] = bot_token`` with no validation of length, charset or shape —
# so the tests below assert config ROUTING (which key the value lands under,
# that the dead ``botToken`` key is gone, that operator-set policy fields
# survive), never token validity.
#
# They previously used a synthetic but correctly-SHAPED Discord token. GitHub
# secret scanning matched the shape and raised three "Discord Bot Token"
# alerts on the public mirror (Evolve-Ops/evolve). Because the public repo
# republishes as a fresh single commit with no shared history, every republish
# looks to push protection like a NEW introduction of those tokens — so a
# shape-matching fixture here can block the publish force-push outright.
# Keep these unmistakably non-credential: if you need a new one, add another
# ``NOT-A-REAL-TOKEN-*`` value rather than anything token-shaped.
_FIXTURE_TOKEN_ADD = "NOT-A-REAL-TOKEN-discord-fixture-add"
_FIXTURE_TOKEN_ROTATE = "NOT-A-REAL-TOKEN-discord-fixture-rotate"
_FIXTURE_TOKEN_LEGACY_STRIP = "NOT-A-REAL-TOKEN-discord-fixture-legacy-strip"
_FIXTURE_TOKEN_READ_FAILURE = "NOT-A-REAL-TOKEN-discord-fixture-read-failure"


# ── Reader/checker helpers ────────────────────────────────────────────────────


def _no_token(_bot_id):
    """No Discord config stored yet."""
    return None


def _valid_token_config(_bot_id):
    """Valid per-bot config stored on disk (bot token is in keystore, not here)."""
    return {
        "bot_user_id": "123456789012345678",
        "bot_username": "EvolveBot#1234",
        "invited_guilds": ["987654321098765432"],
        "activated_at": 1_700_000_000,
    }


def _token_ok(_token):
    """verify_token says token is valid."""
    return True, None, {"id": "123456789012345678", "username": "EvolveBot#1234"}


def _token_revoked(_token):
    """verify_token says token is invalid."""
    return False, "invalid_token", None


def _token_network_error(_token):
    """verify_token raises a network error."""
    raise ConnectionError("simulated network failure")


def _make_shared_dir_with_creds(tmp_path, *, client_id="C123", bot_token="Bot.token.abc"):
    """Create a keystore with full Discord credentials."""
    ks = tmp_path / "keystore"
    ks.mkdir(exist_ok=True)
    (ks / "discord-client-id").write_text(client_id)
    (ks / "discord-client-secret").write_text("S456")
    (ks / "discord-bot-token").write_text(bot_token)
    return tmp_path


# ── TestResolveStatus ─────────────────────────────────────────────────────────


class TestResolveStatus:
    """State machine: credentials_missing → missing → valid / revoked / unknown."""

    def test_credentials_missing_when_no_shared_dir_creds(self, tmp_path):
        """credentials_missing when keystore files are absent."""
        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,  # empty tmp dir — no keystore subdir
            read_token=_no_token,
            check_token=_token_ok,
        )
        assert status.token_state == "credentials_missing"
        assert status.error is not None
        assert "discord-setup.md" in status.error

    def test_credentials_missing_when_client_id_absent(self, tmp_path):
        """credentials_missing when client_id file is missing (bot_token present)."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "discord-client-secret").write_text("S456")
        (ks / "discord-bot-token").write_text("Bot.token.abc")
        # No discord-client-id file

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_no_token,
            check_token=_token_ok,
        )
        assert status.token_state == "credentials_missing"

    def test_credentials_missing_when_bot_token_absent(self, tmp_path):
        """credentials_missing when bot_token file is missing (client_id present)."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "discord-client-id").write_text("C123")
        (ks / "discord-client-secret").write_text("S456")
        # No discord-bot-token file

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_no_token,
            check_token=_token_ok,
        )
        assert status.token_state == "credentials_missing"

    def test_credentials_missing_preempts_token_check(self, tmp_path):
        """credentials_missing pre-empts even when per-bot config exists."""
        # Even if a per-bot config exists, credentials check comes first
        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,  # no creds
            read_token=_valid_token_config,
            check_token=_token_ok,
        )
        assert status.token_state == "credentials_missing"

    def test_missing_when_no_per_bot_config(self, tmp_path):
        """missing when credentials are present but no per-bot config stored."""
        _make_shared_dir_with_creds(tmp_path)

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_no_token,
            check_token=_token_ok,
        )
        assert status.token_state == "missing"
        # invite_url should be populated because client_id is known
        assert status.invite_url is not None
        assert "discord.com" in status.invite_url

    def test_valid_when_token_passes_check(self, tmp_path):
        """valid when config stored and bot token validates."""
        _make_shared_dir_with_creds(tmp_path)

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_valid_token_config,
            check_token=_token_ok,
        )
        assert status.token_state == "valid"
        assert status.bot_user_id == "123456789012345678"
        assert status.bot_username == "EvolveBot#1234"

    def test_revoked_when_token_check_fails(self, tmp_path):
        """revoked when config stored but token is invalid."""
        _make_shared_dir_with_creds(tmp_path)

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_valid_token_config,
            check_token=_token_revoked,
        )
        assert status.token_state == "revoked"
        assert status.error == "invalid_token"

    def test_unknown_when_token_check_raises(self, tmp_path):
        """unknown when verify_token raises (network error)."""
        _make_shared_dir_with_creds(tmp_path)

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_valid_token_config,
            check_token=_token_network_error,
        )
        assert status.token_state == "unknown"
        assert "token_check_failed" in (status.error or "")

    def test_resolve_without_shared_dir_skips_credential_check(self):
        """When shared_dir is None, credential check is skipped; caller owns it."""
        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=None,
            read_token=_no_token,
            check_token=_token_ok,
        )
        assert status.token_state == "missing"

    def test_invite_url_in_missing_status(self, tmp_path):
        """invite_url is populated even in missing status (so UI can show it)."""
        _make_shared_dir_with_creds(tmp_path, client_id="MYCLIENTID")

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_no_token,
            check_token=_token_ok,
        )
        assert status.token_state == "missing"
        assert status.invite_url is not None
        assert "MYCLIENTID" in status.invite_url

    def test_to_dict_shape(self, tmp_path):
        """to_dict() includes all required keys."""
        _make_shared_dir_with_creds(tmp_path)

        status = discord_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_valid_token_config,
            check_token=_token_ok,
        )
        d = status.to_dict()
        assert d["skill_id"] == "discord"
        assert d["token_state"] == "valid"
        assert d["status"] == "valid"  # alias field
        assert "bot_user_id" in d
        assert "bot_username" in d
        assert "invite_url" in d
        assert "invited_guilds" in d


# ── TestBuildInstallPlan ──────────────────────────────────────────────────────


class TestBuildInstallPlan:
    """build_install_plan returns the right steps for each token_state."""

    def _make_status(self, token_state, *, bot_id="admin_bot", invite_url=None, error=None):
        return discord_install.InstallStatus(
            bot_id=bot_id,
            token_state=token_state,
            invite_url=invite_url,
            error=error,
        )

    def test_credentials_missing_has_one_configure_step(self):
        st = self._make_status("credentials_missing")
        plan = discord_install.build_install_plan(st)
        assert len(plan) == 1
        assert plan[0].id == "configure_credentials"
        assert "discord-setup.md" in (plan[0].payload.get("hint") or "")

    def test_missing_has_invite_and_confirm_steps(self):
        st = self._make_status("missing")
        plan = discord_install.build_install_plan(st)
        assert len(plan) == 2
        ids = [s.id for s in plan]
        assert "invite" in ids
        assert "confirm" in ids

    def test_missing_invite_step_has_access_panel(self):
        st = self._make_status("missing")
        plan = discord_install.build_install_plan(st)
        invite_step = next(s for s in plan if s.id == "invite")
        assert invite_step.access_panel is not None
        assert "will" in invite_step.access_panel
        assert "wont" in invite_step.access_panel

    def test_revoked_has_same_steps_as_missing(self):
        st_missing = self._make_status("missing")
        st_revoked = self._make_status("revoked")
        plan_missing = discord_install.build_install_plan(st_missing)
        plan_revoked = discord_install.build_install_plan(st_revoked)
        assert [s.id for s in plan_missing] == [s.id for s in plan_revoked]

    def test_valid_returns_empty_plan(self):
        st = self._make_status("valid")
        plan = discord_install.build_install_plan(st)
        assert plan == []

    def test_unknown_returns_empty_plan(self):
        st = self._make_status("unknown", error="network_error")
        plan = discord_install.build_install_plan(st)
        assert plan == []

    def test_step_to_dict_shape(self):
        st = self._make_status("missing")
        plan = discord_install.build_install_plan(st)
        for step in plan:
            d = step.to_dict()
            assert "id" in d
            assert "label" in d
            assert "endpoint" in d
            assert "payload" in d
            assert "access_panel" in d

    def test_invite_endpoint_points_to_discord_route(self):
        st = self._make_status("missing")
        plan = discord_install.build_install_plan(st)
        invite_step = next(s for s in plan if s.id == "invite")
        assert "/api/skills/install/discord" in (invite_step.endpoint or "")

    def test_confirm_endpoint_points_to_discord_status(self):
        st = self._make_status("missing")
        plan = discord_install.build_install_plan(st)
        confirm_step = next(s for s in plan if s.id == "confirm")
        assert "/api/skills/install/discord" in (confirm_step.endpoint or "")
        assert confirm_step.payload.get("bot_id") == "admin_bot"


# ── TestBuildInviteUrl ────────────────────────────────────────────────────────


class TestBuildInviteUrl:
    """build_invite_url constructs a valid Discord bot invite URL."""

    def test_url_starts_with_discord_authorize(self):
        url = discord_install.build_invite_url("MY_CLIENT_ID_123")
        assert url.startswith("https://discord.com/oauth2/authorize?")

    def test_url_includes_client_id(self):
        url = discord_install.build_invite_url("CLIENT_XYZ")
        assert "CLIENT_XYZ" in url

    def test_url_includes_bot_scope(self):
        url = discord_install.build_invite_url("C123")
        assert "bot" in url

    def test_url_includes_permissions(self):
        url = discord_install.build_invite_url("C123")
        assert str(discord_install.DISCORD_DEFAULT_PERMISSIONS) in url

    def test_custom_permissions_included(self):
        url = discord_install.build_invite_url("C123", permissions=8)  # administrator
        assert "8" in url

    def test_custom_scopes_included(self):
        url = discord_install.build_invite_url("C123", scopes=("bot",))
        assert "bot" in url

    def test_url_is_well_formed(self):
        """URL must have no spaces and be parseable."""
        import urllib.parse as _up
        url = discord_install.build_invite_url("C123")
        assert " " not in url
        parsed = _up.urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "discord.com"
        params = dict(_up.parse_qsl(parsed.query))
        assert "client_id" in params
        assert "permissions" in params
        assert "scope" in params


# ── TestStateStore ────────────────────────────────────────────────────────────


class TestStateStore:
    """In-memory state store: create, get, set_result, consume."""

    def test_create_and_get(self):
        state = discord_install.discord_state_create("bot-1", "https://discord.com/oauth2/authorize?...")
        entry = discord_install.discord_state_get(state)
        assert entry is not None
        assert entry["bot_id"] == "bot-1"
        assert entry["result"]["status"] == "pending"

    def test_unknown_state_returns_none(self):
        assert discord_install.discord_state_get("not-a-state") is None

    def test_set_result_updates_entry(self):
        state = discord_install.discord_state_create("bot-2", "https://discord.com/oauth2/authorize?...")
        ok = discord_install.discord_state_set_result(state, {"status": "success", "bot_username": "EvolveBot"})
        assert ok is True
        entry = discord_install.discord_state_get(state)
        assert entry["result"]["status"] == "success"

    def test_consume_pops_state(self):
        state = discord_install.discord_state_create("bot-3", "https://discord.com/oauth2/authorize?...")
        discord_install.discord_state_set_result(state, {"status": "success"})
        popped = discord_install.discord_state_consume(state)
        assert popped is not None
        # After consume, it's gone
        assert discord_install.discord_state_get(state) is None

    def test_expired_state_returns_none(self):
        """An entry that expired in the past is rejected."""
        import secrets
        state = secrets.token_urlsafe(24)
        with discord_install._DISCORD_OAUTH_STATE_LOCK:
            discord_install._DISCORD_OAUTH_STATE[state] = {
                "bot_id": "test-bot-expired",
                "invite_url": "https://discord.com/oauth2/authorize?...",
                "expires_at": time.time() - 1,  # already expired
                "result": {"status": "pending"},
            }
        assert discord_install.discord_state_get(state) is None


# ── TestAccessPanel ───────────────────────────────────────────────────────────


class TestAccessPanel:
    """The plain-language access panel must be Plex-test friendly."""

    def test_access_panel_has_will_and_wont_lists(self):
        panel = discord_install.DISCORD_ACCESS_PANEL
        assert "will" in panel
        assert "wont" in panel
        assert len(panel["will"]) > 0
        assert len(panel["wont"]) > 0

    def test_access_panel_no_oauth_jargon(self):
        panel = discord_install.DISCORD_ACCESS_PANEL
        full_text = " ".join([
            panel.get("summary", ""),
            *panel.get("will", []),
            *panel.get("wont", []),
        ])
        for jargon in ("scope", "token", "oauth", "grant"):
            assert jargon.lower() not in full_text.lower(), (
                f"Access panel contains jargon word '{jargon}'"
            )

    def test_access_panel_mentions_discord(self):
        panel = discord_install.DISCORD_ACCESS_PANEL
        assert "Discord" in (panel.get("summary") or "") or "Discord" in panel.get("skill_display_name", "")

    def test_wont_list_mentions_no_dm_reading(self):
        """Users should see that the bot won't read other users' DMs."""
        wont = " ".join(discord_install.DISCORD_ACCESS_PANEL.get("wont", []))
        assert any(word in wont.lower() for word in ("direct message", "dm", "between other"))


# ── TestCredentialHelpers ─────────────────────────────────────────────────────


class TestCredentialHelpers:
    """read_discord_credentials reads from the keystore correctly."""

    def test_reads_all_three_credentials(self, tmp_path):
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "discord-client-id").write_text("MY_CLIENT_ID\n")
        (ks / "discord-client-secret").write_text("MY_CLIENT_SECRET\n")
        (ks / "discord-bot-token").write_text("Bot.MY_TOKEN\n")

        client_id, client_secret, bot_token = discord_install.read_discord_credentials(tmp_path)
        assert client_id == "MY_CLIENT_ID"
        assert client_secret == "MY_CLIENT_SECRET"
        assert bot_token == "Bot.MY_TOKEN"

    def test_returns_none_when_files_missing(self, tmp_path):
        client_id, client_secret, bot_token = discord_install.read_discord_credentials(tmp_path)
        assert client_id is None
        assert client_secret is None
        assert bot_token is None

    def test_returns_none_when_files_empty(self, tmp_path):
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "discord-client-id").write_text("")
        (ks / "discord-client-secret").write_text("")
        (ks / "discord-bot-token").write_text("")

        client_id, client_secret, bot_token = discord_install.read_discord_credentials(tmp_path)
        assert client_id is None
        assert client_secret is None
        assert bot_token is None

    def test_partial_credentials_still_read(self, tmp_path):
        """Partial credentials return what exists; missing ones are None."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "discord-client-id").write_text("C123")
        # No client-secret, no bot-token

        client_id, client_secret, bot_token = discord_install.read_discord_credentials(tmp_path)
        assert client_id == "C123"
        assert client_secret is None
        assert bot_token is None


# ── TestSkillRegistryEntry ────────────────────────────────────────────────────


class TestSkillRegistryEntry:
    """SKILL_REGISTRY_ENTRY has the right shape for inventory.py."""

    def test_entry_has_required_keys(self):
        reg = discord_install.SKILL_REGISTRY_ENTRY
        assert reg["id"] == "discord"
        assert "display_name" in reg
        assert "summary" in reg
        assert "access_panel" in reg
        assert "default_scopes" in reg

    def test_default_scopes_include_bot(self):
        scopes = discord_install.DISCORD_DEFAULT_SCOPES
        assert "bot" in scopes

    def test_default_permissions_conservative(self):
        """Default permissions must NOT include the Administrator bit (0x8)."""
        perms = discord_install.DISCORD_DEFAULT_PERMISSIONS
        ADMINISTRATOR_BIT = 0x8
        assert not (perms & ADMINISTRATOR_BIT), (
            f"Default permissions 0x{perms:x} include Administrator bit — should not"
        )

    def test_skill_id_constant(self):
        assert discord_install.DISCORD_SKILL_ID == "discord"


# ── TestEnableChannelInOcConfig ──────────────────────────────────────────────
#
# Discord's install was a P0 dead-end before this fix: the /confirm endpoint
# wrote skills/discord.json but never merged channels.discord +
# plugins.entries.discord into openclaw.json. Without those, the gateway
# never loaded the Discord channel plugin, even though the install
# completed and returned ok=true. Modeled exactly on the telegram channel-
# wiring tests added with the original Telegram fix.

import os
from unittest.mock import patch


class _FakeSubprocessRun:
    """Records subprocess calls and captures the staged JSON before unlink.

    Mirrors the helper in test_skills_telegram_install.py exactly so any
    drift between Telegram's and Discord's wiring shows up as a test break.
    """

    def __init__(self):
        self.calls = []
        self.staged_payload = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if argv[:2] == ["sudo", "/bin/cp"] and len(argv) >= 4:
            try:
                with open(argv[2], "r", encoding="utf-8") as f:
                    self.staged_payload = f.read()
            except OSError:
                pass
        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return _Result()


class TestEnableChannelInOcConfig:
    """enable_channel_in_oc_config: merge channels.discord + plugins.entries.discord
    into openclaw.json, preserving operator-set policy fields. Without this
    helper firing on /confirm, the install would leave the gateway unwired."""

    def _run_with_stage(self, tmp_path):
        """Point mkstemp at a real file under tmp_path so the helper's json.dump
        writes somewhere reachable. _FakeSubprocessRun's cp handler captures
        the staged payload before the helper's finally clause unlinks it."""
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            real_tmp = tmp_path / "stage.json"
            fd = os.open(str(real_tmp), os.O_WRONLY | os.O_CREAT, 0o600)
            with patch("tempfile.mkstemp", return_value=(fd, str(real_tmp))):
                yield
        return _ctx()

    def test_adds_channels_and_plugin_entry_when_absent(self, tmp_path):
        """Empty openclaw.json gets channels.discord + plugins.entries.discord.

        This is the regression test for the install-flow dead-end: pre-fix the
        Discord /confirm route wrote only skills/discord.json and never touched
        openclaw.json, so the gateway loaded nothing and inbound DMs/guild
        messages silently dropped."""
        def _fake_read(_bot_id):
            return {}, None

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network",
                   return_value={"bots": {"team_bot_b": {"user": "personal_bot_user"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._run_with_stage(tmp_path):
            ok, err = discord_install.enable_channel_in_oc_config(
                "team_bot_b",
                _FIXTURE_TOKEN_ADD,
            )

        assert ok, err
        assert fake_run.staged_payload is not None, "expected sudo cp to fire"
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        dc = staged["channels"]["discord"]
        assert dc["enabled"] is True
        # OC's bundled discord plugin reads channels.discord.token (verified
        # live: 31x .token vs 0x botToken in dist/discord-*.js, and team_bot_b's
        # working config on the mini uses ``token``). Pre-2026-05-30 this
        # was wrongly ``botToken`` — see docs/skills-deep-audit-2026-05-30.md P0-3.
        assert dc["token"] == _FIXTURE_TOKEN_ADD
        # Default policy fields seeded — matches the canonical shape used by
        # working team_bot_b config and the telegram/slack pattern.
        assert dc["dmPolicy"] == "pairing"
        assert dc["groupPolicy"] == "allowlist"
        assert dc["streaming"] == {"mode": "off"}
        # plugins.entries.discord.enabled flipped on so the gateway actually
        # loads the plugin.
        assert staged["plugins"]["entries"]["discord"]["enabled"] is True

        cp_calls = [c for c in fake_run.calls if c[0][:2] == ["sudo", "/bin/cp"]]
        assert len(cp_calls) == 1, f"expected one sudo cp, got {fake_run.calls}"

    def test_preserves_existing_policy_fields(self, tmp_path):
        """Operator-set dmPolicy / groupPolicy / streaming survive a redo of
        the install — same idempotency contract as telegram/slack so a re-run
        of /confirm doesn't clobber per-server lockdowns."""
        existing = {
            "channels": {
                "discord": {
                    "enabled": False,
                    "dmPolicy": "owner-only",
                    "groupPolicy": "deny",
                    "streaming": {"mode": "partial"},
                    "token": "old-token",
                }
            },
            "plugins": {"entries": {"discord": {"enabled": False}}},
        }

        def _fake_read(_bot_id):
            return existing, None

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network",
                   return_value={"bots": {"team_bot_b": {"user": "personal_bot_user"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._run_with_stage(tmp_path):
            ok, err = discord_install.enable_channel_in_oc_config(
                "team_bot_b",
                _FIXTURE_TOKEN_ROTATE,
            )

        assert ok, err
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        dc = staged["channels"]["discord"]
        # Operator-set fields preserved
        assert dc["dmPolicy"] == "owner-only"
        assert dc["groupPolicy"] == "deny"
        assert dc["streaming"] == {"mode": "partial"}
        # But install-flow fields rewritten
        assert dc["enabled"] is True
        assert dc["token"] == _FIXTURE_TOKEN_ROTATE
        assert staged["plugins"]["entries"]["discord"]["enabled"] is True

    def test_strips_legacy_botToken_field(self, tmp_path):
        """A `botToken` key left by the pre-2026-05-30 wizard write (the buggy
        version that wrote the wrong field name — see
        docs/skills-deep-audit-2026-05-30.md P0-3) is removed in favour of
        ``token``. Failing to do this would leave a dead key in the config
        that confuses status-correctness drift checks."""
        existing = {
            "channels": {"discord": {"botToken": "legacy-buggy-write", "enabled": False}},
        }

        def _fake_read(_bot_id):
            return existing, None

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network",
                   return_value={"bots": {"team_bot_b": {"user": "personal_bot_user"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._run_with_stage(tmp_path):
            ok, err = discord_install.enable_channel_in_oc_config(
                "team_bot_b",
                _FIXTURE_TOKEN_LEGACY_STRIP,
            )

        assert ok, err
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        # The dead botToken key from the pre-fix wizard write must be gone.
        assert "botToken" not in staged["channels"]["discord"]
        # And the new value lands under the field OC actually reads.
        assert staged["channels"]["discord"]["token"] == _FIXTURE_TOKEN_LEGACY_STRIP

    def test_returns_error_on_oc_read_failure(self, tmp_path):
        """If reading the bot's openclaw.json fails, the helper does NOT write —
        the previous state stays intact on disk."""
        def _fake_read(_bot_id):
            return None, "oc_read_failed: PermissionError"

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run):
            ok, err = discord_install.enable_channel_in_oc_config(
                "team_bot_b",
                _FIXTURE_TOKEN_READ_FAILURE,
            )

        assert ok is False
        assert "oc_read_failed" in (err or "")
        # No sudo cp was attempted
        assert not any(c[0][:2] == ["sudo", "/bin/cp"] for c in fake_run.calls)

    def test_kickstart_gateway_reexported(self):
        """kickstart_gateway is re-exported from _oc_install_common so the
        server route can import it from discord_install directly, matching
        the telegram/slack contract."""
        from evolve_admin.skills import _oc_install_common as _oc_common
        assert discord_install.kickstart_gateway is _oc_common.kickstart_gateway
