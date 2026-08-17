"""Tests for evolve_admin.skills — Spec 12: Skills inventory.

Coverage:
  TestGetBotSkills (10)   — unit tests for get_bot_skills()
  TestGetPodSkills (3)    — unit tests for get_pod_skills()
  TestSkillsEndpoints (4) — Flask route smoke-tests for /api/skills/*
"""

from __future__ import annotations

import json
import sys
import types
import importlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── Worktree import isolation ─────────────────────────────────────────────────
# Ensure we load the worktree's evolve_admin, not whatever is editable-installed
# from the main repo (see CLAUDE.md note on editable-install shadowing).

_WORKTREE = Path(__file__).parent.parent  # packages/admin
if str(_WORKTREE / "src") not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

import evolve_admin.skills as _skills
from evolve_admin.skills import (
    SkillEntry,
    SkillInventory,
    get_bot_skills,
    get_pod_skills,
    _resolve_plugin_status,
    _resolve_mcp_status,
    _read_app_skill_deps,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_oc(
    plugin_entries: dict | None = None,
    plugin_installs: dict | None = None,
    mcp_servers: dict | None = None,
    channels: dict | None = None,
) -> dict:
    """Build a minimal openclaw.json-shaped dict."""
    oc: dict = {}
    if plugin_entries is not None or plugin_installs is not None:
        oc["plugins"] = {}
        if plugin_entries is not None:
            oc["plugins"]["entries"] = plugin_entries
        if plugin_installs is not None:
            oc["plugins"]["installs"] = plugin_installs
    if mcp_servers is not None:
        oc["mcp"] = {"servers": mcp_servers}
    if channels is not None:
        oc["channels"] = channels
    return oc


def _write_oc(tmp_path: Path, oc_data: dict, bot_user: str = "testbot") -> Path:
    """Write openclaw.json into a tmp_path home-like tree. Returns home dir."""
    home = tmp_path / bot_user
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps(oc_data))
    return home


# ── TestGetBotSkills ──────────────────────────────────────────────────────────

class TestGetBotSkills:

    def _get_with_home(self, tmp_path, oc_data, bot_id="testbot", bot_user="testbot"):
        """Patch pwd so bot home points at tmp_path and call get_bot_skills."""
        home = _write_oc(tmp_path, oc_data, bot_user)
        mock_pw = MagicMock()
        mock_pw.pw_dir = str(home)
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.return_value = mock_pw
                inv = get_bot_skills(bot_id, bot_user)
        return inv

    # 1. Plugin with API-key config → configured
    def test_plugin_with_config_is_configured(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "slack": {"config": {"botToken": "xoxb-real-token"}}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert len(inv.skills) == 1
        skill = inv.skills[0]
        assert skill.id == "slack"
        assert skill.status == "configured"
        assert skill.format_compliance == "proprietary"

    # 2. OAuth provider with no token → needs_oauth
    def test_oauth_provider_without_token_is_needs_oauth(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "google_workspace": {"config": {}}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert inv.skills[0].status == "needs_oauth"

    # 3. Non-OAuth plugin that is explicitly disabled → missing_config
    # (enabled=True or omitted → configured; only disabled → missing_config)
    def test_non_oauth_disabled_plugin_is_missing_config(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "brave": {"enabled": False, "config": {}}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert inv.skills[0].status == "missing_config"

    # 4. Plugin with enabled=False → disabled
    def test_disabled_plugin_reflects_enabled_false(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "slack": {"enabled": False, "config": {"botToken": "tok"}}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert inv.skills[0].enabled is False

    # 5. MCP server with real env vars → configured + standard
    def test_mcp_server_with_real_env_configured(self, tmp_path):
        oc = _make_oc(mcp_servers={
            "github-mcp": {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_abc123"}}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert len(inv.skills) == 1
        skill = inv.skills[0]
        assert skill.id == "mcp:github-mcp"
        assert skill.status == "configured"
        assert skill.format_compliance == "standard"

    # 6. MCP server with placeholder env var → missing_config
    def test_mcp_server_with_placeholder_is_missing_config(self, tmp_path):
        oc = _make_oc(mcp_servers={
            "brave-search": {"command": "npx", "env": {"BRAVE_API_KEY": "$BRAVE_API_KEY"}}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert inv.skills[0].status == "missing_config"

    # 7. Both plugins and MCP servers appear in output
    def test_plugins_and_mcp_both_present(self, tmp_path):
        oc = _make_oc(
            plugin_entries={"slack": {"config": {"botToken": "tok"}}},
            mcp_servers={"github-mcp": {"command": "npx", "env": {}}},
        )
        inv = self._get_with_home(tmp_path, oc)
        ids = {s.id for s in inv.skills}
        assert "slack" in ids
        assert "mcp:github-mcp" in ids

    # 8. Missing openclaw.json → read_error is set
    def test_missing_oc_json_sets_read_error(self, tmp_path):
        home = tmp_path / "nobot"
        home.mkdir()
        (home / ".openclaw").mkdir()
        # No openclaw.json written
        mock_pw = MagicMock()
        mock_pw.pw_dir = str(home)
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.return_value = mock_pw
                # sudo fallback will also fail; patch subprocess to avoid real sudo
                with patch("evolve_admin.skills.inventory.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1, stdout="")
                    inv = get_bot_skills("nobot", "nobot")
        assert inv.read_error is not None
        assert inv.skills == []

    # 9. install_source pulled from plugins.installs block
    def test_install_source_from_installs_block(self, tmp_path):
        oc = _make_oc(
            plugin_entries={"brave": {"config": {"apiKey": "abc"}}},
            plugin_installs={"brave": {"source": "github:openclaw-plugins/brave"}},
        )
        inv = self._get_with_home(tmp_path, oc)
        assert inv.skills[0].install_source == "github:openclaw-plugins/brave"

    # 10. Unknown plugin name gets a sensible display fallback
    def test_unknown_plugin_display_fallback(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "my_custom_tool": {"config": {"key": "val"}}
        })
        inv = self._get_with_home(tmp_path, oc)
        skill = inv.skills[0]
        assert skill.id == "my_custom_tool"
        assert skill.display == "My Custom Tool"
        assert skill.category == "tools"

    # 18. Regression: telegram enabled with token in channels.* → configured
    # On real pods, telegram.config is empty; the bot token lives in channels.telegram.
    # The new heuristic uses enabled=True as the primary signal, so this is "configured".
    def test_telegram_with_channel_config_returns_configured(self, tmp_path):
        oc = _make_oc(
            plugin_entries={"telegram": {"enabled": True}},
            channels={"telegram": {"enabled": True, "botToken": "1234:real-token"}},
        )
        inv = self._get_with_home(tmp_path, oc)
        assert len(inv.skills) == 1
        skill = inv.skills[0]
        assert skill.id == "telegram"
        assert skill.status == "configured"

    # 18a. Regression: telegram with FILESYSTEM file (no channels.telegram block) →
    # configured. New atlas-style installs via /api/skills/install/telegram/set-token
    # write ~/.openclaw/skills/telegram.json instead of openclaw.json channels.*
    # block. Inventory must pick up either source.
    def test_telegram_with_filesystem_file_returns_configured(self, tmp_path):
        # Minimal openclaw.json — no plugins.entries.telegram, no channels.telegram
        oc = _make_oc()
        home = _write_oc(tmp_path, oc, "atlasbot")
        # Write the filesystem-skill file the set-token endpoint produces
        skills_dir = home / ".openclaw" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "telegram.json").write_text(json.dumps({
            "bot_token": "1234567890:ABC-real-token",
            "bot_username": "atlasbot_evo_bot",
            "bot_first_name": "atlasbot",
            "can_join_groups": True,
            "can_read_all_group_messages": False,
            "verified_at": 1779955976.5,
        }))
        mock_pw = MagicMock()
        mock_pw.pw_dir = str(home)
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.return_value = mock_pw
                inv = get_bot_skills("atlasbot", "atlasbot")
        # Telegram should appear exactly once, configured.
        telegram_skills = [s for s in inv.skills if s.id == "telegram"]
        assert len(telegram_skills) == 1
        skill = telegram_skills[0]
        assert skill.status == "configured"
        assert skill.display == "Telegram"
        assert skill.category == "messaging"
        assert skill.install_source == "filesystem"

    # 18b. Telegram with NEITHER filesystem file NOR channels block → not present.
    def test_telegram_with_no_config_is_absent(self, tmp_path):
        oc = _make_oc()
        inv = self._get_with_home(tmp_path, oc)
        telegram_skills = [s for s in inv.skills if s.id == "telegram"]
        assert telegram_skills == []

    # 18c. Telegram with BOTH filesystem file AND channels block → registered once
    # (filesystem path wins by virtue of running first; de-dup blocks channels-path
    # from double-registering).
    def test_telegram_with_both_sources_registers_once(self, tmp_path):
        oc = _make_oc(channels={"telegram": {"botToken": "from-channels"}})
        home = _write_oc(tmp_path, oc, "atlasbot")
        skills_dir = home / ".openclaw" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "telegram.json").write_text(json.dumps({
            "bot_token": "from-filesystem",
            "bot_username": "atlasbot_evo_bot",
        }))
        mock_pw = MagicMock()
        mock_pw.pw_dir = str(home)
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.return_value = mock_pw
                inv = get_bot_skills("atlasbot", "atlasbot")
        telegram_skills = [s for s in inv.skills if s.id == "telegram"]
        assert len(telegram_skills) == 1
        # The filesystem path runs first so it wins. install_source reflects that.
        assert telegram_skills[0].install_source == "filesystem"

    # 19. Regression: LLM providers (anthropic, openai, xai, google) with only
    # enabled=True and no plugins.entries[].config → configured.
    # API keys live in env / auth.profiles, not in plugins.entries[].config.
    def test_anthropic_enabled_returns_configured(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "anthropic": {"enabled": True},
            "openai": {"enabled": True},
            "xai": {"enabled": True},
            "google": {"enabled": True},
        })
        inv = self._get_with_home(tmp_path, oc)
        statuses = {s.id: s.status for s in inv.skills}
        assert statuses["anthropic"] == "configured"
        assert statuses["openai"] == "configured"
        assert statuses["xai"] == "configured"
        # google is the Gemini LLM provider, NOT google_workspace — must not be needs_oauth
        assert statuses["google"] == "configured"

    # 20. Regression: google_workspace (true OAuth provider) without auth profile → needs_oauth
    # google_workspace is the only entry in _OAUTH_PROVIDERS that correctly maps to OAuth.
    def test_oauth_skill_uses_oauth_provider_check(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "google_workspace": {"enabled": True}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert inv.skills[0].status == "needs_oauth"

    # 21. Regression: existing plugins.entries[].config credential check preserved
    # for plugins that do store non-enabled config (e.g. brave's nested apiKey).
    # Under the new heuristic, enabled=True → configured regardless of config presence.
    def test_plain_config_skill_still_returns_configured_when_enabled(self, tmp_path):
        oc = _make_oc(plugin_entries={
            "brave": {"enabled": True, "config": {"webSearch": {"apiKey": "real-key"}}}
        })
        inv = self._get_with_home(tmp_path, oc)
        assert inv.skills[0].status == "configured"


# ── TestGetPodSkills ──────────────────────────────────────────────────────────

class TestGetPodSkills:

    @pytest.fixture(autouse=True)
    def _stub_apple_local_probe(self):
        """Stub apple_local.resolve_status by default for ALL tests in this
        class. After P2, get_pod_skills calls the apple resolver once per
        matrix refresh — and the real resolver runs 4 osascript subprocesses
        with 5s timeouts each, which would make every TestGetPodSkills test
        wall-clock 20s+. Individual tests that care about the apple status
        can patch.object inside the test (which takes precedence over this
        autouse fixture).
        """
        import evolve_admin.skills.apple_local_install as _apple
        with patch.object(
            _apple, "resolve_status",
            lambda *, bot_id, **kw: _apple.InstallStatus(
                bot_id=bot_id, status="unknown", granted={},
            ),
        ):
            yield

    def _pod(self, tmp_path, bot_oc_map: dict[str, dict]) -> dict:
        """Build a pod rollup with fake home dirs for each bot."""
        # Write each bot's openclaw.json and set up pwd patches
        homes = {}
        for bot_id, oc_data in bot_oc_map.items():
            home = _write_oc(tmp_path, oc_data, bot_id)
            homes[bot_id] = home

        bots = {bid: {"user": bid} for bid in bot_oc_map}

        def _fake_getpwnam(user):
            pw = MagicMock()
            pw.pw_dir = str(homes[user])
            return pw

        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.side_effect = _fake_getpwnam
                result = get_pod_skills(bots)
        return result

    # 11. Matrix includes all bots as column headers
    def test_matrix_has_all_bots_as_columns(self, tmp_path):
        result = self._pod(tmp_path, {
            "team_bot_a": _make_oc(plugin_entries={"slack": {"config": {"botToken": "tok"}}}),
            "admin_bot": _make_oc(plugin_entries={}),
        })
        assert result["all_bot_ids"] == ["team_bot_a", "admin_bot"]

    # 12. Skill on one bot, None for other in matrix
    def test_matrix_none_for_missing_skill_on_bot(self, tmp_path):
        result = self._pod(tmp_path, {
            "team_bot_a": _make_oc(plugin_entries={"slack": {"config": {"botToken": "tok"}}}),
            "admin_bot": _make_oc(plugin_entries={}),
        })
        matrix = result["matrix"]
        assert "slack" in matrix
        assert matrix["slack"]["team_bot_a"] == "configured"
        assert matrix["slack"]["admin_bot"] is None

    # 13. Skill present on both bots → both have status in matrix
    def test_matrix_both_bots_have_status_when_skill_shared(self, tmp_path):
        # Key path must be the CANONICAL runtime location
        # (plugins.entries.brave.config.webSearch.apiKey) — the bare
        # `config.apiKey` this fixture used until 2026-07-31 is a path nothing
        # reads, and it only passed because status was derived from `enabled`
        # alone. It now resolves to "missing_config", correctly.
        result = self._pod(tmp_path, {
            "team_bot_a": _make_oc(
                plugin_entries={"brave": {"config": {"webSearch": {"apiKey": "k1"}}}},
            ),
            "admin_bot": _make_oc(
                plugin_entries={"brave": {"config": {"webSearch": {"apiKey": "k2"}}}},
            ),
        })
        matrix = result["matrix"]
        assert matrix["brave"]["team_bot_a"] == "configured"
        assert matrix["brave"]["admin_bot"] == "configured"

    # ── Regression: channels-only messaging skills surface in the matrix ─────
    # Before this fix, bots configured via channels.<provider> without a
    # corresponding plugins.entries.<provider> were missing from the inventory,
    # so the "Add a skill" catalog falsely showed them as installable on bots
    # where they already worked (personal_bot has channels.telegram → Telegram works
    # → catalog should show ✓ personal_bot, not "+ Add to personal_bot").

    def test_channels_only_telegram_detected_as_configured(self, tmp_path):
        """personal_bot-style: channels.telegram has a bot token, no plugins entry.
        The bot still receives Telegram messages; the inventory must show it."""
        oc = _make_oc(
            plugin_entries={},
            channels={"telegram": {"botToken": "1234:abcdefghijklmnopqrstuvwxyz"}},
        )
        result = self._pod(tmp_path, {"personal_bot": oc})
        matrix = result["matrix"]
        assert "telegram" in matrix
        assert matrix["telegram"]["personal_bot"] == "configured"

    def test_channels_only_slack_detected_as_configured(self, tmp_path):
        oc = _make_oc(
            plugin_entries={},
            channels={"slack": {"botToken": "xoxb-real-token", "workspace_id": "W1"}},
        )
        result = self._pod(tmp_path, {"team_bot_c": oc})
        matrix = result["matrix"]
        assert "slack" in matrix
        assert matrix["slack"]["team_bot_c"] == "configured"

    def test_channels_block_without_token_is_missing_config(self, tmp_path):
        """Empty channel block (placeholder during partial setup) → user must
        finish, so we surface missing_config rather than nothing at all."""
        oc = _make_oc(
            plugin_entries={},
            channels={"discord": {"workspace_id": "W1"}},  # no token fields
        )
        result = self._pod(tmp_path, {"team_bot_b": oc})
        matrix = result["matrix"]
        assert matrix["discord"]["team_bot_b"] == "missing_config"

    def test_channels_detection_does_not_double_register_when_plugin_present(self, tmp_path):
        """team_bot_a has plugins.entries.slack AND channels.slack — must produce a
        single matrix entry, not two."""
        oc = _make_oc(
            plugin_entries={"slack": {"enabled": True}},
            channels={"slack": {"botToken": "xoxb-y"}},
        )
        result = self._pod(tmp_path, {"team_bot_a": oc})
        # Only one entry under 'slack' in the inventory list.
        slack_entries = [s for s in result["bots"]["team_bot_a"]["skills"] if s["id"] == "slack"]
        assert len(slack_entries) == 1
        assert slack_entries[0]["status"] == "configured"

    def test_nested_channel_token_still_detected(self, tmp_path):
        """Some channels nest config by sub-bot (channels.telegram.evolve = {…}).
        Token detection must walk the tree, not just the top level."""
        oc = _make_oc(
            plugin_entries={},
            channels={"telegram": {"evolve": {"botToken": "1234:abc...xyz"}}},
        )
        result = self._pod(tmp_path, {"evolve": oc})
        assert result["matrix"]["telegram"]["evolve"] == "configured"

    # ── Bundled-channel passthrough (2026-06-04, OC coverage audit PR #2123) ──
    # These tests pin the inventory passthrough for OC-bundled / officially-
    # catalogued channels we don't (yet) have install modules for. An operator
    # who wires any of these via `openclaw channels add ...` directly must
    # show up on the Skills page as install_source="channels". Without these
    # rows in _CHANNEL_BACKED_SKILLS the inventory silently misses them — the
    # inverse-Bucket-D failure called out in
    # docs/openclaw-coverage-audit-2026-06-04.md.

    def test_channels_only_whatsapp_detected_as_configured(self, tmp_path):
        """A bot wired via `openclaw channels add --channel whatsapp` lands a
        channels.whatsapp block with an account-shaped authDir. Inventory
        must surface it even before whatsapp_install.py ships."""
        oc = _make_oc(
            plugin_entries={},
            channels={"whatsapp": {"enabled": True, "accounts": {
                "primary": {"authDir": "/Users/team-bot-a/.openclaw/whatsapp/auth",
                            "name": "wa-primary"}
            }}},
        )
        result = self._pod(tmp_path, {"team-bot-a": oc})
        assert "whatsapp" in result["matrix"]
        assert result["matrix"]["whatsapp"]["team-bot-a"] == "configured"

    def test_channels_only_signal_detected_as_configured(self, tmp_path):
        """Signal — `openclaw channels add --channel signal --number +15551234567 ...`"""
        oc = _make_oc(
            plugin_entries={},
            channels={"signal": {"enabled": True, "number": "+15551234567"}},
        )
        result = self._pod(tmp_path, {"security-bot": oc})
        assert result["matrix"]["signal"]["security-bot"] == "configured"

    def test_channels_only_matrix_detected_as_configured(self, tmp_path):
        """Matrix — `openclaw channels add --channel matrix --homeserver ... --access-token ...`"""
        oc = _make_oc(
            plugin_entries={},
            channels={"matrix": {"enabled": True,
                                 "homeserver": "https://matrix.org",
                                 "userId": "@evo:matrix.org",
                                 "accessToken": "syt_" + "x" * 40}},
        )
        result = self._pod(tmp_path, {"evolve": oc})
        assert result["matrix"]["matrix"]["evolve"] == "configured"

    def test_channels_only_imessage_detected_as_configured(self, tmp_path):
        """iMessage — `openclaw channels add --channel imessage --handle ...`
        After the 2026-05-30 withdrawal, the home-rolled imessage_install.py
        is gone but OC's bundled @openclaw/imessage plugin still consumes
        channels.imessage. This row keeps the inventory honest while the
        Phase-1 rewire (bundled-plugin pattern) is in flight."""
        oc = _make_oc(
            plugin_entries={},
            channels={"imessage": {"enabled": True,
                                   "handle": "evo@icloud.com",
                                   "service": "auto"}},
        )
        result = self._pod(tmp_path, {"team-bot-a": oc})
        assert result["matrix"]["imessage"]["team-bot-a"] == "configured"

    def test_channels_only_mattermost_detected_as_configured(self, tmp_path):
        """Mattermost — `openclaw channels add --channel mattermost --server ... --token ...`"""
        oc = _make_oc(
            plugin_entries={},
            channels={"mattermost": {"enabled": True,
                                     "serverUrl": "https://chat.example.com",
                                     "token": "personal_access_token_xyz"}},
        )
        result = self._pod(tmp_path, {"team_bot_a": oc})
        assert result["matrix"]["mattermost"]["team_bot_a"] == "configured"

    def test_channels_only_sms_detected_as_configured(self, tmp_path):
        """SMS (Twilio) — `openclaw channels add --channel sms ...`"""
        oc = _make_oc(
            plugin_entries={},
            channels={"sms": {"enabled": True,
                              "accountSid": "AC" + "x" * 30,
                              "authToken": "auth_xyz_12345",
                              "phoneNumber": "+15555550100"}},
        )
        result = self._pod(tmp_path, {"team-bot-c": oc})
        assert result["matrix"]["sms"]["team-bot-c"] == "configured"

    def test_channels_only_irc_detected_as_configured(self, tmp_path):
        """IRC — `openclaw channels add --channel irc --server ... --nick ...`"""
        oc = _make_oc(
            plugin_entries={},
            channels={"irc": {"enabled": True,
                              "server": "irc.libera.chat",
                              "nickname": "evobot"}},
        )
        result = self._pod(tmp_path, {"bot-d": oc})
        assert result["matrix"]["irc"]["bot-d"] == "configured"

    def test_channels_passthrough_empty_block_is_missing_config(self, tmp_path):
        """A schema-default channels.whatsapp block (no authDir, no number) —
        which is what `openclaw config` emits for every known channel by
        default — must NOT show as configured. The May-incident anti-pattern
        was inventory lying that 'we have it' when no credential was wired."""
        oc = _make_oc(
            plugin_entries={},
            channels={"whatsapp": {"enabled": False, "dmPolicy": "pairing",
                                   "groupPolicy": "allowlist"}},
        )
        result = self._pod(tmp_path, {"team-bot-a": oc})
        # Empty/schema-default block → present but missing_config.
        assert result["matrix"].get("whatsapp", {}).get("team-bot-a") == "missing_config"

    def test_channels_passthrough_covers_all_audit_phase1_targets(self):
        """Snapshot guard: any drop from _CHANNEL_BACKED_SKILLS for the audit's
        Phase 1 list (or the cheap Bucket-B follow-ons) is a regression. If
        this fails, an entry was removed — confirm intentional then update."""
        from evolve_admin.skills.inventory import _CHANNEL_BACKED_SKILLS
        # Wrapped channels (existing install modules)
        wrapped = {"slack", "telegram", "discord"}
        # Phase 1 + Bucket-B P2 follow-ons per
        # docs/openclaw-coverage-audit-2026-06-04.md
        bundled_or_catalogued = {
            "whatsapp", "signal", "matrix", "imessage",
            "mattermost", "sms", "irc",
            "googlechat", "msteams", "line", "feishu", "nostr",
            "synology-chat", "nextcloud-talk",
        }
        expected = wrapped | bundled_or_catalogued
        missing = expected - set(_CHANNEL_BACKED_SKILLS.keys())
        assert not missing, (
            f"_CHANNEL_BACKED_SKILLS missing: {sorted(missing)}. "
            f"These are bundled or catalogued by OpenClaw "
            f"(docs/openclaw-coverage-audit-2026-06-04.md); without entries "
            f"here, manually-wired channels.<id> blocks vanish from the "
            f"Skills page."
        )

    # ── Withdrawn filesystem-config skills (2026-05-30) ───────────────────────
    # The pre-2026-05-30 version of these tests asserted that a paste-token
    # marker file (skills/home_assistant.json, skills/notion.json, etc.) made
    # the matrix report the bot as "configured". That was the dead-end pattern:
    # the file landed, inventory said configured, but no code anywhere
    # consumed the credential at runtime. See
    # docs/design/paste-token-skills-future-2026-05-30.md.
    #
    # Withdrawal: the §3 _FILESYSTEM_SKILLS detection branch for these five
    # skills (obsidian_vault, home_assistant, notion, linear, runway) was
    # removed. The matrix now correctly excludes them — even when the marker
    # file is present — so the catalog can no longer surface a false "✓".
    # Telegram stays in _FILESYSTEM_SKILLS because it has a real runtime
    # consumer via channels.telegram.

    def test_withdrawn_filesystem_skills_do_not_surface_via_marker_file(self, tmp_path):
        """Regression guard: dropping any of the five withdrawn marker files
        (home_assistant.json, notion.json, linear.json, runway.json,
        obsidian_vault.json) must NOT make the matrix report the bot as
        configured. They were dead-ends — file existed, no runtime consumer
        ever read it — and re-adding §3 detection without a runtime path
        would resurrect the bug."""
        home = _write_oc(tmp_path, _make_oc(), bot_user="admin_bot")
        skills_dir = home / ".openclaw" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for fname, payload in (
            ("home_assistant.json", {"base_url": "http://h:8123", "access_token": "x" * 40}),
            ("notion.json",         {"access_token": "secret_" + "a" * 40}),
            ("linear.json",         {"access_token": "lin_api_" + "b" * 40}),
            ("runway.json",         {"access_token": "key_" + "c" * 40}),
            ("obsidian_vault.json", {"vault_path": "/Users/admin_bot/Documents/Vault"}),
        ):
            (skills_dir / fname).write_text(json.dumps(payload))

        def _fake_getpwnam(user):
            pw = MagicMock(); pw.pw_dir = str(home); return pw
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.side_effect = _fake_getpwnam
                result = get_pod_skills({"admin_bot": {"user": "admin_bot"}})
        # None of the withdrawn skills should appear in the matrix at all —
        # they're not detected, so the catalog has no false-positive surface.
        for withdrawn_id in (
            "home_assistant", "notion", "linear", "runway", "obsidian_vault",
        ):
            assert withdrawn_id not in result["matrix"], (
                f"{withdrawn_id} re-surfaced in inventory matrix despite "
                f"being withdrawn — check _FILESYSTEM_SKILLS in inventory.py"
            )

    def test_telegram_filesystem_marker_still_detected(self, tmp_path):
        """Telegram stays in _FILESYSTEM_SKILLS because it has a real
        runtime consumer (the OC telegram plugin loads from channels.telegram,
        which telegram_install.enable_channel_in_oc_config writes alongside
        the filesystem marker). This test guards against an over-eager
        cleanup that also removes telegram from the §3 detection."""
        home = _write_oc(tmp_path, _make_oc(), bot_user="admin_bot")
        skills_dir = home / ".openclaw" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "telegram.json").write_text(
            json.dumps({"bot_token": "1234:abc...xyz", "bot_username": "evo_bot"})
        )

        def _fake_getpwnam(user):
            pw = MagicMock(); pw.pw_dir = str(home); return pw
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.side_effect = _fake_getpwnam
                result = get_pod_skills({"admin_bot": {"user": "admin_bot"}})
        assert result["matrix"]["telegram"]["admin_bot"] == "configured"

    # ── P2: pod-wide local-system + catalog-stub skills ──────────────────────
    # apple_local and autocad don't surface via per-bot inventory detection
    # (TCC grants aren't per-bot, autocad is a v1 stub). get_pod_skills
    # injects them with the resolver-returned status applied to every bot.
    # The resolver runs ONCE per matrix refresh — much cheaper than 4 osascript
    # probes × N_bots.

    # The apple_local pod-wide-matrix tests (test_apple_local_appears_in_matrix
    # / _reports_needs_tcc / _probe_resolves_once / _unknown_when_probe_raises)
    # were removed 2026-05-30 when apple_local was WITHDRAWN from the catalog
    # in Phase 1c of the deep skills audit. See
    # docs/skills-deep-audit-2026-05-30.md for the rationale: the probe was
    # the only consumer (no plugin / mcp.servers / channels / tool surface
    # consumed any Apple app on any bot), and TCC grants land on the wrong
    # macOS user anyway (evolve admin vs the bot's user account).

    def test_apple_local_does_not_appear_in_pod_wide_matrix(self, tmp_path):
        """Regression guard: apple_local must stay OUT of the pod-wide
        matrix until it has a real runtime consumer. Re-adding it here
        without first wiring apple-mcp-server (or in-pod tool surfaces)
        would resurrect the green-active-while-broken pattern."""
        result = self._pod(tmp_path, {"admin_bot": _make_oc()})
        assert "apple_local" not in result["matrix"], (
            "apple_local was withdrawn 2026-05-30 — see "
            "docs/skills-deep-audit-2026-05-30.md. Restore inventory entry "
            "only after a real consumer ships."
        )

    def test_autocad_appears_in_matrix_as_needs_app(self, tmp_path):
        """autocad's resolver always returns needs_app in v1 (stub). Matrix
        must surface it for every bot so users can see the catalog stub."""
        result = self._pod(tmp_path, {
            "team_bot_a": _make_oc(),
            "admin_bot": _make_oc(),
        })
        assert result["matrix"]["autocad"]["team_bot_a"] == "needs_app"
        assert result["matrix"]["autocad"]["admin_bot"] == "needs_app"

    def test_pod_wide_skill_meta_carries_display_and_category(self, tmp_path):
        """skill_meta entries for autocad must populate the UI's display
        name and category bucket. apple_local used to be in this test too,
        but was withdrawn 2026-05-30 (Phase 1c)."""
        result = self._pod(tmp_path, {"admin_bot": _make_oc()})
        assert "autocad" in result["skill_meta"]
        meta = result["skill_meta"]["autocad"]
        assert meta["display"]
        assert meta["category"]


# ── TestSkillsEndpoints ───────────────────────────────────────────────────────

class TestSkillsEndpoints:
    """Smoke-tests for the Flask routes registered by _register_skills_routes."""

    @pytest.fixture()
    def client(self, tmp_path):
        """Create a Flask test client with a minimal network.json."""
        from evolve_admin.web.server import create_app

        network = {
            "bots": {
                "team_bot_a": {"user": "team_bot_a", "platform": "slack"},
            }
        }
        net_file = tmp_path / "network.json"
        net_file.write_text(json.dumps(network))

        # Write a fake openclaw.json for team_bot_a
        home = tmp_path / "team_bot_a_home"
        oc_dir = home / ".openclaw"
        oc_dir.mkdir(parents=True)
        oc_data = _make_oc(
            plugin_entries={"slack": {"config": {"botToken": "xoxb-fake"}}}
        )
        (oc_dir / "openclaw.json").write_text(json.dumps(oc_data))

        def _fake_getpwnam(user):
            pw = MagicMock()
            pw.pw_dir = str(home)
            return pw

        app = create_app(network_path=net_file)
        app.config["TESTING"] = True

        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pwd.getpwnam.side_effect = _fake_getpwnam
                with app.test_client() as c:
                    yield c

    # 14. Known bot returns 200
    def test_known_bot_returns_200(self, client):
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pw = MagicMock()
                # The test_client fixture yields inside a patch context but
                # the route resolves pwd at call time, so patch again here.
                mock_pwd.getpwnam.return_value = mock_pw
                resp = client.get("/api/skills/team_bot_a")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["bot_id"] == "team_bot_a"
        assert "skills" in data

    # 15. Unknown bot returns 404
    def test_unknown_bot_returns_404(self, client):
        resp = client.get("/api/skills/doesnotexist")
        assert resp.status_code == 404

    # 16. Pod endpoint returns 200 with matrix key
    def test_pod_endpoint_returns_200_with_matrix(self, client):
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pw = MagicMock()
                mock_pwd.getpwnam.return_value = mock_pw
                resp = client.get("/api/skills/pod")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "matrix" in data
        assert "skill_meta" in data
        assert "all_bot_ids" in data

    # 17. Pod endpoint is not shadowed by the /<bot_id> route
    def test_pod_endpoint_not_shadowed_by_bot_route(self, client):
        """Flask route ordering: /pod must match before /<bot_id>."""
        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}):
            with patch("evolve_admin.skills.inventory._pwd") as mock_pwd:
                mock_pw = MagicMock()
                mock_pwd.getpwnam.return_value = mock_pw
                resp = client.get("/api/skills/pod")
        # A 404 here would mean Flask treated "pod" as a bot_id and found
        # no bot named "pod" in the network — which is the shadowing bug.
        assert resp.status_code == 200
        data = resp.get_json()
        # Matrix key only comes from the pod endpoint, not the bot endpoint
        assert "matrix" in data

    # 18. capability_summaries covers BOTH Google config paths.
    #
    # Regression for the unified-Google catalog row showing "+ Add to <bot>"
    # for bots already configured via Path C (service_account_dwd). Those
    # bots have NO OAuth profile, so the profile-based resolver alone left
    # capability_summaries empty for them → the catalog fell through to the
    # un-installed "+ Add" state. The resolver now also derives a summary
    # from network.json::google_integration.scopes.
    def test_pod_capability_summaries_cover_both_config_paths(self, tmp_path):
        from evolve_admin.web.server import create_app
        from evolve_admin.skills.google_install import InstallStatus

        def _gscope(name):
            return f"https://www.googleapis.com/auth/{name}"

        # Three bots: Path C (DwD), Path A (OAuth profile), unconfigured.
        network = {
            "bots": {
                "team_bot_a": {  # Path C — service_account_dwd
                    "user": "team_bot_a", "platform": "slack",
                    "google_integration": {
                        "mode": "service_account_dwd",
                        "workspace_domain": "example.com",
                        "subject": "team_bot_a@example.com",
                        "service_account_secret_ref": "keystore:team_bot_a_sa",
                        "scopes": [
                            _gscope("gmail.send"), _gscope("gmail.readonly"),
                            _gscope("calendar"), _gscope("drive.file"),
                        ],
                    },
                },
                "team_bot_b": {"user": "team_bot_b", "platform": "slack"},  # Path A
                "team_bot_c": {"user": "team_bot_c", "platform": "slack"},  # unconfigured
            }
        }
        net_file = tmp_path / "network.json"
        net_file.write_text(json.dumps(network))

        homes = {}
        for user in ("team_bot_a", "team_bot_b", "team_bot_c"):
            home = tmp_path / f"{user}_home"
            (home / ".openclaw").mkdir(parents=True)
            (home / ".openclaw" / "openclaw.json").write_text(json.dumps(_make_oc()))
            homes[user] = home

        def _fake_getpwnam(user):
            pw = MagicMock()
            pw.pw_dir = str(homes.get(user, tmp_path / "missing"))
            return pw

        # Path A: only team_bot_b has an OAuth profile with consented scopes;
        # everyone else resolves not_installed (no profile). This isolates the
        # Path-C branch — it must light up team_bot_a from network.json alone.
        def _fake_resolve(bid):
            if bid == "team_bot_b":
                scopes = [_gscope("gmail.readonly"), _gscope("calendar.readonly")]
                return InstallStatus(
                    bot_id=bid, status="active",
                    granted_scopes=scopes,
                    granted_capabilities=["gmail_read", "calendar_read"],
                    capability_summary="read",
                    has_oauth_profile=True, mcp_server_present=True,
                )
            return InstallStatus(bot_id=bid, status="not_installed")

        app = create_app(network_path=net_file)
        app.config["TESTING"] = True

        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}), \
             patch("evolve_admin.skills.inventory._pwd") as mock_pwd, \
             patch(
                 "evolve_admin.skills.google_install.resolve_status_with_default_readers",
                 side_effect=_fake_resolve,
             ):
            mock_pwd.getpwnam.side_effect = _fake_getpwnam
            with app.test_client() as c:
                resp = c.get("/api/skills/pod")

        assert resp.status_code == 200
        google_summaries = resp.get_json()["capability_summaries"]["google"]

        # Path C (team_bot_a) — installed, with a scope-derived summary. send +
        # read of gmail + calendar + drive is the "custom" mixed bucket; labels
        # enumerate the granted capabilities for the tooltip.
        assert "team_bot_a" in google_summaries, "Path-C DwD bot must report installed"
        dwd = google_summaries["team_bot_a"]
        assert dwd["summary"] and dwd["summary"] != "not connected"
        assert "Send Gmail" in dwd["labels"] and "Read Gmail" in dwd["labels"]

        # Path A (team_bot_b) — still installed (no regression).
        assert "team_bot_b" in google_summaries
        assert google_summaries["team_bot_b"]["summary"] == "read"

        # Unconfigured (team_bot_c) — absent → catalog renders "+ Add".
        assert "team_bot_c" not in google_summaries

    # 18b. capability_summaries must NOT false-positive on the Gemini plugin.
    #
    # There are two distinct things keyed under the id "google": the inventory
    # matrix's "google" is OC's bundled @openclaw/google-plugin (the Gemini LLM
    # provider), while capability_summaries["google"] is the Google *Workspace*
    # catalog chip. A bot that has only Gemini enabled (matrix["google"] ==
    # "configured") but NO network.json google_integration is NOT configured for
    # Workspace and must stay out of capability_summaries — otherwise the unified
    # Google catalog row would show "✓ installed" for every Gemini bot. The Path-C
    # resolver gates on google_integration.mode (authoritative), never the matrix,
    # so this holds; lock it in.
    def test_pod_capability_summaries_excludes_gemini_only_bot(self, tmp_path):
        from evolve_admin.web.server import create_app
        from evolve_admin.skills.google_install import InstallStatus

        # gem_bot: Gemini LLM plugin enabled in openclaw.json, but no
        # google_integration in network.json → Workspace not configured.
        network = {
            "bots": {
                "gem_bot": {"user": "gem_bot", "platform": "slack"},
            }
        }
        net_file = tmp_path / "network.json"
        net_file.write_text(json.dumps(network))

        home = tmp_path / "gem_bot_home"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(
            json.dumps(_make_oc(plugin_entries={"google": {"enabled": True}}))
        )

        def _fake_getpwnam(user):
            pw = MagicMock()
            pw.pw_dir = str(home if user == "gem_bot" else tmp_path / "missing")
            return pw

        # No OAuth profile for anyone → Path A yields not_installed; the only
        # thing that could (wrongly) light up gem_bot is the Gemini matrix entry.
        def _fake_resolve(bid):
            return InstallStatus(bot_id=bid, status="not_installed")

        app = create_app(network_path=net_file)
        app.config["TESTING"] = True

        with patch("evolve_admin.skills.inventory._read_app_skill_deps", return_value={}), \
             patch("evolve_admin.skills.inventory._pwd") as mock_pwd, \
             patch(
                 "evolve_admin.skills.google_install.resolve_status_with_default_readers",
                 side_effect=_fake_resolve,
             ):
            mock_pwd.getpwnam.side_effect = _fake_getpwnam
            with app.test_client() as c:
                resp = c.get("/api/skills/pod")

        assert resp.status_code == 200
        body = resp.get_json()

        # The Gemini plugin IS present in the inventory matrix (the contrast).
        assert body["matrix"].get("google", {}).get("gem_bot") == "configured"
        # …but it must NOT leak into the Workspace capability summaries.
        assert "gem_bot" not in body["capability_summaries"]["google"]


# ── Inline-key providers: `enabled` is not proof of a credential ─────────────
# Regression guard for the fleet-wide Brave failure (2026-07-31): the Skills
# page reported brave "installed on all bots" from `enabled: true` alone while
# 6 of 9 mini bots and VPS evo had no API key, so every bot advertised a
# web_search tool that 401s at call time.

def test_brave_enabled_without_key_reads_missing_config():
    from evolve_admin.skills.inventory import _resolve_plugin_status
    oc = {"plugins": {"entries": {"brave": {"enabled": True}}}}
    assert _resolve_plugin_status("brave", {"enabled": True}, oc) == "missing_config"


def test_brave_enabled_with_canonical_key_reads_configured():
    from evolve_admin.skills.inventory import _resolve_plugin_status
    entry = {"enabled": True, "config": {"webSearch": {"apiKey": "BSA-x"}}}
    oc = {"plugins": {"entries": {"brave": entry}}}
    assert _resolve_plugin_status("brave", entry, oc) == "configured"


def test_brave_legacy_key_location_still_reads_configured():
    """A bot keyed at the legacy tools.web.search.apiKey has working search.

    Must agree with the Credentials tab (brave_key_from_oc_config), else we
    re-create the canonical/legacy mismatch #3219 fixed, pointed the other way.
    """
    from evolve_admin.skills.inventory import _resolve_plugin_status
    oc = {"tools": {"web": {"search": {"apiKey": "BSA-legacy"}}}}
    assert _resolve_plugin_status("brave", {"enabled": True}, oc) == "configured"


def test_non_inline_key_providers_keep_enabled_only_rule():
    """Plugins whose creds live in env/auth.profiles/channels.* are untouched.

    Narrowing this would re-introduce the false negatives the enabled-only
    heuristic was written to prevent.
    """
    from evolve_admin.skills.inventory import _resolve_plugin_status
    for name in ("anthropic", "google", "openai", "xai", "telegram", "slack"):
        assert _resolve_plugin_status(name, {"enabled": True}, {}) == "configured", name


def test_disabled_brave_still_reads_missing_config():
    from evolve_admin.skills.inventory import _resolve_plugin_status
    assert _resolve_plugin_status("brave", {"enabled": False}, {}) == "missing_config"
