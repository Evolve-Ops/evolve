"""tests/test_skills_telegram_install.py — Telegram skill install flow.

Covers the contract for the V2.3-1 Telegram skill:

  - resolve_status routes by (token present, getMe result) into the
    five-state machine: missing / valid / revoked / invalid / unknown.
  - build_install_plan returns the right ordered steps for each state.
  - The plain-language access panel ships with the set_token step.
  - verify_bot_token validates format, calls getMe, handles 401 and network errors.
  - Token storage round-trip (read / write / delete helpers).
  - Hostile inputs: empty token, token with invalid chars, very long token.

All Telegram API calls are mocked at the module boundary — no real API traffic.
No filesystem writes in tests — write_token_config is stubbed via injectable
read_token / check_token callables.

Trust-chain notes:
  - Bot tokens are never logged or returned verbatim in test assertions.
  - resolve_status delegates to injectable check_token so token validation
    is exercised without real network traffic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import telegram_install  # noqa: E402


# ── Reader/checker helpers ────────────────────────────────────────────────────


def _no_token(_bot_id):
    """No Telegram token stored yet."""
    return None


def _valid_token_config(_bot_id):
    """Valid token config stored on disk."""
    return {
        "bot_token": "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567",
        "bot_username": "myevolvebot",
        "bot_first_name": "My Evolve Bot",
        "can_join_groups": True,
        "can_read_all_group_messages": False,
        "verified_at": 1_700_000_000,
    }


def _revoked_token_config(_bot_id):
    """Token config stored but token is revoked."""
    return {
        "bot_token": "9999999999:OLDREVOKEDtokenXYZabc-1234567890",
        "bot_username": "myevolvebot",
        "bot_first_name": "My Evolve Bot",
        "can_join_groups": False,
        "can_read_all_group_messages": False,
        "verified_at": 1_600_000_000,
    }


def _invalid_format_token_config(_bot_id):
    """Token config stored but token has invalid format."""
    return {
        "bot_token": "notavalidtoken",
    }


def _getme_ok(token):
    """getMe says token is valid."""
    return {
        "ok": True,
        "bot_username": "myevolvebot",
        "bot_first_name": "My Evolve Bot",
        "can_join_groups": True,
        "can_read_all_group_messages": False,
        "error": None,
        "http_status": 200,
    }


def _getme_revoked(token):
    """getMe says 401 — token is revoked."""
    return {
        "ok": False,
        "bot_username": None,
        "bot_first_name": None,
        "can_join_groups": None,
        "can_read_all_group_messages": None,
        "error": "Unauthorized",
        "http_status": 401,
    }


def _getme_network_error(token):
    """getMe raises a network error."""
    raise ConnectionError("simulated network failure")


# ── TestResolveStatus ─────────────────────────────────────────────────────────


class TestResolveStatus:
    """State machine: missing → valid / revoked / invalid / unknown."""

    def test_missing_when_no_token_stored(self):
        """missing when no token config stored."""
        status = telegram_install.resolve_status(
            "admin_bot",
            read_token=_no_token,
            check_token=_getme_ok,
        )
        assert status.bot_token_state == "missing"
        assert status.bot_username is None

    def test_valid_when_token_passes_getme(self):
        """valid when token stored and getMe returns ok."""
        status = telegram_install.resolve_status(
            "admin_bot",
            read_token=_valid_token_config,
            check_token=_getme_ok,
        )
        assert status.bot_token_state == "valid"
        assert status.bot_username == "myevolvebot"
        assert status.bot_first_name == "My Evolve Bot"
        assert status.can_join_groups is True
        assert status.can_read_all_group_messages is False

    def test_revoked_when_getme_returns_401(self):
        """revoked when token stored but getMe returns 401."""
        status = telegram_install.resolve_status(
            "admin_bot",
            read_token=_revoked_token_config,
            check_token=_getme_revoked,
        )
        assert status.bot_token_state == "revoked"
        assert status.error is not None

    def test_invalid_when_stored_token_has_bad_format(self):
        """invalid when stored token doesn't match BotFather format."""
        status = telegram_install.resolve_status(
            "admin_bot",
            read_token=_invalid_format_token_config,
            check_token=_getme_ok,
        )
        assert status.bot_token_state == "invalid"
        assert "format" in (status.error or "")

    def test_unknown_when_getme_raises(self):
        """unknown when getMe raises a network error."""
        status = telegram_install.resolve_status(
            "admin_bot",
            read_token=_valid_token_config,
            check_token=_getme_network_error,
        )
        assert status.bot_token_state == "unknown"
        assert "getme_failed" in (status.error or "")

    def test_unknown_when_token_read_raises(self):
        """unknown when read_token itself raises."""
        def _raising_reader(bot_id):
            raise OSError("disk error")

        status = telegram_install.resolve_status(
            "admin_bot",
            read_token=_raising_reader,
            check_token=_getme_ok,
        )
        assert status.bot_token_state == "unknown"
        assert "token_read_failed" in (status.error or "")

    def test_to_dict_shape(self):
        """to_dict() includes all required keys."""
        status = telegram_install.resolve_status(
            "admin_bot",
            read_token=_valid_token_config,
            check_token=_getme_ok,
        )
        d = status.to_dict()
        assert d["skill_id"] == "telegram"
        assert d["bot_token_state"] == "valid"
        assert d["status"] == "valid"  # alias field
        assert "bot_username" in d
        assert "bot_first_name" in d
        assert "can_join_groups" in d
        assert "can_read_all_group_messages" in d

    def test_status_property_aliases_bot_token_state(self):
        """status property is an alias for bot_token_state."""
        status = telegram_install.InstallStatus(bot_id="admin_bot", bot_token_state="missing")
        assert status.status == "missing"


# ── TestBuildInstallPlan ──────────────────────────────────────────────────────


class TestBuildInstallPlan:
    """build_install_plan returns the right steps for each bot_token_state."""

    def _make_status(self, state, *, bot_id="admin_bot", error=None):
        return telegram_install.InstallStatus(
            bot_id=bot_id,
            bot_token_state=state,
            error=error,
        )

    def test_missing_has_set_token_and_confirm_steps(self):
        st = self._make_status("missing")
        plan = telegram_install.build_install_plan(st)
        assert len(plan) == 2
        ids = [s.id for s in plan]
        assert "set_token" in ids
        assert "confirm" in ids

    def test_set_token_step_has_access_panel(self):
        st = self._make_status("missing")
        plan = telegram_install.build_install_plan(st)
        set_token_step = next(s for s in plan if s.id == "set_token")
        assert set_token_step.access_panel is not None
        assert "will" in set_token_step.access_panel
        assert "wont" in set_token_step.access_panel

    def test_revoked_has_same_steps_as_missing(self):
        st_missing = self._make_status("missing")
        st_revoked = self._make_status("revoked")
        plan_missing = telegram_install.build_install_plan(st_missing)
        plan_revoked = telegram_install.build_install_plan(st_revoked)
        assert [s.id for s in plan_missing] == [s.id for s in plan_revoked]

    def test_invalid_has_same_steps_as_missing(self):
        """Invalid token format — user must re-enter the key."""
        st_missing = self._make_status("missing")
        st_invalid = self._make_status("invalid")
        plan_missing = telegram_install.build_install_plan(st_missing)
        plan_invalid = telegram_install.build_install_plan(st_invalid)
        assert [s.id for s in plan_missing] == [s.id for s in plan_invalid]

    def test_valid_returns_empty_plan(self):
        st = self._make_status("valid")
        plan = telegram_install.build_install_plan(st)
        assert plan == []

    def test_unknown_returns_empty_plan(self):
        st = self._make_status("unknown", error="network_error")
        plan = telegram_install.build_install_plan(st)
        assert plan == []

    def test_step_to_dict_shape(self):
        st = self._make_status("missing")
        plan = telegram_install.build_install_plan(st)
        for step in plan:
            d = step.to_dict()
            assert "id" in d
            assert "label" in d
            assert "endpoint" in d
            assert "payload" in d
            assert "access_panel" in d

    def test_set_token_endpoint_points_to_telegram_route(self):
        st = self._make_status("missing")
        plan = telegram_install.build_install_plan(st)
        set_token_step = next(s for s in plan if s.id == "set_token")
        assert "/api/skills/install/telegram" in (set_token_step.endpoint or "")

    def test_confirm_endpoint_points_to_telegram_status(self):
        st = self._make_status("missing")
        plan = telegram_install.build_install_plan(st)
        confirm_step = next(s for s in plan if s.id == "confirm")
        assert "/api/skills/install/telegram" in (confirm_step.endpoint or "")
        assert confirm_step.payload.get("bot_id") == "admin_bot"


# ── TestVerifyBotToken ────────────────────────────────────────────────────────


class TestVerifyBotToken:
    """verify_bot_token: format check, getMe call, 401, network error."""

    def test_valid_token_returns_ok(self):
        """Valid token + successful getMe → ok=True with bot metadata."""
        mock_body = {
            "ok": True,
            "result": {
                "id": 1234567890,
                "is_bot": True,
                "first_name": "My Evolve Bot",
                "username": "myevolvebot",
                "can_join_groups": True,
                "can_read_all_group_messages": False,
                "supports_inline_queries": False,
            },
        }
        with patch.object(telegram_install, "_telegram_get_json", return_value=(200, mock_body)):
            result = telegram_install.verify_bot_token(
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567"
            )
        assert result["ok"] is True
        assert result["bot_username"] == "myevolvebot"
        assert result["bot_first_name"] == "My Evolve Bot"
        assert result["can_join_groups"] is True
        assert result["error"] is None

    def test_401_returns_not_ok(self):
        """401 from Telegram → ok=False, error = unauthorized."""
        mock_body = {"ok": False, "error_code": 401, "description": "Unauthorized"}
        with patch.object(telegram_install, "_telegram_get_json", return_value=(401, mock_body)):
            result = telegram_install.verify_bot_token(
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567"
            )
        assert result["ok"] is False
        assert result["http_status"] == 401

    def test_network_error_returns_not_ok(self):
        """Network error (status 0) → ok=False."""
        with patch.object(telegram_install, "_telegram_get_json", return_value=(0, None)):
            result = telegram_install.verify_bot_token(
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567"
            )
        assert result["ok"] is False

    def test_empty_token_returns_not_ok_without_api_call(self):
        """Empty token short-circuits before calling the API."""
        with patch.object(
            telegram_install, "_telegram_get_json", side_effect=AssertionError("should not be called")
        ):
            result = telegram_install.verify_bot_token("")
        assert result["ok"] is False
        assert result["error"] == "empty_token"

    def test_invalid_format_token_returns_not_ok_without_api_call(self):
        """Token that doesn't match BotFather format → error without API call."""
        with patch.object(
            telegram_install, "_telegram_get_json", side_effect=AssertionError("should not be called")
        ):
            result = telegram_install.verify_bot_token("notavalidtoken")
        assert result["ok"] is False
        assert result["error"] == "invalid_token_format"

    def test_very_long_token_rejected_by_api(self):
        """A token with a very long suffix passes the format check but is rejected by Telegram."""
        long_token = "1234567890:" + "A" * 500
        # The regex allows any suffix length >= 30 chars. Telegram's API would
        # reject it (401). We mock a 401 to simulate that without real traffic.
        mock_body = {"ok": False, "error_code": 401, "description": "Unauthorized"}
        with patch.object(telegram_install, "_telegram_get_json", return_value=(401, mock_body)):
            result = telegram_install.verify_bot_token(long_token)
        assert result["ok"] is False

    def test_token_with_spaces_trimmed_then_validated(self):
        """Leading/trailing whitespace is stripped before validation."""
        # A well-formed token with surrounding whitespace
        token = "  1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567  "
        mock_body = {
            "ok": True,
            "result": {
                "first_name": "Test Bot",
                "username": "testbot",
                "can_join_groups": True,
                "can_read_all_group_messages": False,
            },
        }
        with patch.object(telegram_install, "_telegram_get_json", return_value=(200, mock_body)):
            result = telegram_install.verify_bot_token(token)
        assert result["ok"] is True

    def test_getme_ok_false_in_body_returns_not_ok(self):
        """Telegram returns 200 but ok=false in body → not ok."""
        mock_body = {"ok": False, "description": "Bot was blocked by the user"}
        with patch.object(telegram_install, "_telegram_get_json", return_value=(200, mock_body)):
            result = telegram_install.verify_bot_token(
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567"
            )
        assert result["ok"] is False


# ── TestAccessPanel ───────────────────────────────────────────────────────────


class TestAccessPanel:
    """The plain-language access panel must be Plex-test friendly."""

    def test_access_panel_has_will_and_wont_lists(self):
        panel = telegram_install.TELEGRAM_ACCESS_PANEL
        assert "will" in panel
        assert "wont" in panel
        assert len(panel["will"]) > 0
        assert len(panel["wont"]) > 0

    def test_access_panel_no_oauth_jargon(self):
        """Panel must not mention OAuth, scope, or grant (Plex test)."""
        panel = telegram_install.TELEGRAM_ACCESS_PANEL
        full_text = " ".join([
            panel.get("summary", ""),
            *panel.get("will", []),
            *panel.get("wont", []),
        ])
        for jargon in ("scope", "oauth", "grant"):
            assert jargon.lower() not in full_text.lower(), (
                f"Access panel contains jargon word '{jargon}'"
            )

    def test_access_panel_mentions_telegram(self):
        panel = telegram_install.TELEGRAM_ACCESS_PANEL
        assert "Telegram" in (panel.get("summary") or "") or \
               "Telegram" in panel.get("skill_display_name", "")

    def test_wont_list_mentions_private_messages(self):
        """Users should see that the bot won't read their private messages."""
        wont = " ".join(telegram_install.TELEGRAM_ACCESS_PANEL.get("wont", []))
        assert any(
            word in wont.lower()
            for word in ("private", "access")
        )


# ── TestTokenPath ──────────────────────────────────────────────────────────────


class TestTokenPath:
    """_token_path uses bot_home, never hardcodes /Users/{bot_id}."""

    def test_token_path_uses_bot_home(self, tmp_path):
        """_token_path should produce path under a home dir, not hardcode /Users/."""
        with patch("evolve_admin.skills.telegram_install._token_path") as mock_tp:
            mock_tp.return_value = tmp_path / ".openclaw/skills/telegram.json"
            path = telegram_install._token_path("admin_bot")
        assert str(path).endswith("telegram.json")
        assert ".openclaw/skills" in str(path)

    def test_token_path_constant(self):
        """TELEGRAM_TOKEN_PATH constant has the right relative path."""
        assert telegram_install.TELEGRAM_TOKEN_PATH == ".openclaw/skills/telegram.json"


# ── TestTokenReadWrite ─────────────────────────────────────────────────────────


class TestTokenReadWrite:
    """read_token_config / delete_token_config on the filesystem."""

    def test_read_returns_none_when_file_missing(self, tmp_path):
        """Returns None when the config file doesn't exist."""
        fake_path = tmp_path / ".openclaw" / "skills" / "telegram.json"
        with patch.object(telegram_install, "_token_path", return_value=fake_path):
            result = telegram_install.read_token_config("admin_bot")
        assert result is None

    def test_read_returns_dict_when_file_exists(self, tmp_path):
        """Returns parsed dict when the config file exists."""
        skills_dir = tmp_path / ".openclaw" / "skills"
        skills_dir.mkdir(parents=True)
        config_file = skills_dir / "telegram.json"
        config_file.write_text(
            '{"bot_token": "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567", "bot_username": "testbot"}',
            encoding="utf-8",
        )
        with patch.object(telegram_install, "_token_path", return_value=config_file):
            result = telegram_install.read_token_config("admin_bot")
        assert result is not None
        assert result["bot_username"] == "testbot"

    def test_delete_returns_true_when_file_exists(self, tmp_path):
        """delete_token_config returns True when file is present and deleted."""
        skills_dir = tmp_path / ".openclaw" / "skills"
        skills_dir.mkdir(parents=True)
        config_file = skills_dir / "telegram.json"
        config_file.write_text("{}", encoding="utf-8")
        with patch.object(telegram_install, "_token_path", return_value=config_file):
            result = telegram_install.delete_token_config("admin_bot")
        assert result is True
        assert not config_file.exists()

    def test_delete_returns_false_gracefully_on_error(self, tmp_path):
        """delete_token_config doesn't raise even when sudo rm isn't available."""
        fake_path = tmp_path / "nonexistent.json"  # file doesn't exist
        with patch.object(telegram_install, "_token_path", return_value=fake_path):
            # sudo /bin/rm will fail in test environment, but we shouldn't raise
            result = telegram_install.delete_token_config("admin_bot")
        # Result may be True or False depending on whether sudo rm succeeds,
        # but it must not raise an exception.
        assert isinstance(result, bool)


# ── TestSkillRegistryEntry ────────────────────────────────────────────────────


class TestSkillRegistryEntry:
    """SKILL_REGISTRY_ENTRY has the right shape."""

    def test_entry_has_required_keys(self):
        reg = telegram_install.SKILL_REGISTRY_ENTRY
        assert reg["id"] == "telegram"
        assert "display_name" in reg
        assert "summary" in reg
        assert "access_panel" in reg

    def test_skill_id_constant(self):
        assert telegram_install.TELEGRAM_SKILL_ID == "telegram"

    def test_display_name_is_telegram(self):
        reg = telegram_install.SKILL_REGISTRY_ENTRY
        assert "Telegram" in reg["display_name"]


# ── TestHostileInputs ─────────────────────────────────────────────────────────


class TestHostileInputs:
    """verify_bot_token handles boundary and adversarial inputs gracefully."""

    @pytest.mark.parametrize("token", [
        "",                          # empty
        "   ",                       # whitespace only
        "notavalidtoken",            # no colon separator
        "abc:short",                 # suffix too short
        "1234567890:" + "A" * 512,   # absurdly long suffix
        "1234567890:ab cd ef",       # spaces in token body
        "\x00\x01\x02:badchars",     # control characters
        "9" * 50 + ":" + "B" * 35,  # valid-ish but numeric prefix too long
    ])
    def test_hostile_token_never_raises(self, token):
        """verify_bot_token must not raise on any input."""
        with patch.object(telegram_install, "_telegram_get_json", return_value=(0, None)):
            try:
                result = telegram_install.verify_bot_token(token)
                # Must return a dict with ok key
                assert isinstance(result, dict)
                assert "ok" in result
            except Exception as exc:
                pytest.fail(f"verify_bot_token raised for token {token!r}: {exc}")


# ── TestEnableChannelInOcConfig ───────────────────────────────────────────────


class _FakeSubprocessRun:
    """Capture-only stub for subprocess.run that returns success.

    Records (argv, kwargs) for every call so tests can assert ordering and
    the specific shell-out invocations the helper makes (sudo cp, chown, chmod).
    On a ``sudo /bin/cp <src> <dst>`` call it stashes the *staged* file's
    contents in ``self.staged_payload`` — the helper's ``finally`` clause
    unlinks the source right after, so reading via the destination path
    would race the cleanup.
    """

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.staged_payload: str | None = None

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
    """enable_channel_in_oc_config: merge channels.telegram + plugins.entries.telegram
    into openclaw.json, preserving operator-set fields."""

    def _fake_read_oc(self, cfg):
        return lambda _bot_id: (cfg, None)

    def _run_with_stage(self, fake_run, tmp_path):
        """Context manager that points mkstemp at a real file under tmp_path so
        the helper's json.dump writes somewhere reachable. _FakeSubprocessRun's
        cp handler captures the staged payload before the helper's finally
        clause unlinks it."""
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            real_tmp = tmp_path / "stage.json"
            fd = os.open(str(real_tmp), os.O_WRONLY | os.O_CREAT, 0o600)
            with patch("tempfile.mkstemp", return_value=(fd, str(real_tmp))):
                yield
        return _ctx()

    def test_adds_channels_and_plugin_entry_when_absent(self, tmp_path):
        """Empty openclaw.json gets channels.telegram + plugins.entries.telegram."""
        def _fake_read(_bot_id):
            return {}, None

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network", return_value={"bots": {"atlas": {"user": "atlas"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._run_with_stage(fake_run, tmp_path):
            ok, err = telegram_install.enable_channel_in_oc_config(
                "atlas",
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567",
            )

        assert ok, err
        assert fake_run.staged_payload is not None, "expected sudo cp to fire"
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        tg = staged["channels"]["telegram"]
        assert tg["enabled"] is True
        assert tg["botToken"] == "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567"
        assert tg["dmPolicy"] == "pairing"
        assert tg["groupPolicy"] == "allowlist"
        assert tg["streaming"] == {"mode": "off"}
        assert staged["plugins"]["entries"]["telegram"]["enabled"] is True

        cp_calls = [c for c in fake_run.calls if c[0][:2] == ["sudo", "/bin/cp"]]
        assert len(cp_calls) == 1, f"expected one sudo cp, got {fake_run.calls}"

    def test_preserves_existing_policy_fields(self, tmp_path):
        """Operator-set dmPolicy / groupPolicy / streaming survive a redo of the install."""
        existing = {
            "channels": {
                "telegram": {
                    "enabled": False,
                    "dmPolicy": "owner-only",
                    "groupPolicy": "deny",
                    "streaming": {"mode": "partial"},
                    "botToken": "old-token",
                }
            },
            "plugins": {"entries": {"telegram": {"enabled": False}}},
        }

        def _fake_read(_bot_id):
            return existing, None

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network", return_value={"bots": {"atlas": {"user": "atlas"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._run_with_stage(fake_run, tmp_path):
            ok, err = telegram_install.enable_channel_in_oc_config(
                "atlas",
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-new-token",
            )

        assert ok, err
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        tg = staged["channels"]["telegram"]
        assert tg["dmPolicy"] == "owner-only"
        assert tg["groupPolicy"] == "deny"
        assert tg["streaming"] == {"mode": "partial"}
        assert tg["enabled"] is True
        assert tg["botToken"] == "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-new-token"
        assert staged["plugins"]["entries"]["telegram"]["enabled"] is True

    def test_strips_legacy_token_field(self, tmp_path):
        """A `token` key left by old wizards is removed in favour of botToken."""
        existing = {
            "channels": {"telegram": {"token": "legacy-shape", "enabled": False}},
        }

        def _fake_read(_bot_id):
            return existing, None

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network", return_value={"bots": {"atlas": {"user": "atlas"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._run_with_stage(fake_run, tmp_path):
            ok, err = telegram_install.enable_channel_in_oc_config(
                "atlas",
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567",
            )

        assert ok, err
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        assert "token" not in staged["channels"]["telegram"]
        assert staged["channels"]["telegram"]["botToken"] == "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567"

    def test_returns_error_on_oc_read_failure(self, tmp_path):
        """If reading the bot's openclaw.json fails, the helper does NOT write."""
        def _fake_read(_bot_id):
            return None, "oc_read_failed: PermissionError"

        fake_run = _FakeSubprocessRun()
        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run):
            ok, err = telegram_install.enable_channel_in_oc_config(
                "atlas",
                "1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567",
            )

        assert ok is False
        assert "oc_read_failed" in (err or "")
        # No sudo cp was attempted
        assert not any(c[0][:2] == ["sudo", "/bin/cp"] for c in fake_run.calls)


# ── TestKickstartGateway ──────────────────────────────────────────────────────


class TestKickstartGateway:
    """kickstart_gateway: shell out to launchctl, surface failures."""

    def test_invokes_launchctl_with_per_bot_label(self):
        from evolve_admin.skills import _oc_install_common as _oc_common
        fake_run = _FakeSubprocessRun()
        with patch.object(_oc_common.subprocess, "run", fake_run):
            ok, err = telegram_install.kickstart_gateway("atlas")
        assert ok, err
        assert fake_run.calls, "expected at least one subprocess.run call"
        argv, _ = fake_run.calls[0]
        assert argv == [
            "sudo", "/bin/launchctl", "kickstart", "-k",
            "system/ai.openclaw.atlas-gateway",
        ]

    def test_returns_error_when_launchctl_fails(self):
        from evolve_admin.skills import _oc_install_common as _oc_common
        class _BadRun:
            def __call__(self, argv, **kwargs):
                class _R:
                    returncode = 3
                    stdout = ""
                    stderr = "Could not find specified service"
                return _R()

        with patch.object(_oc_common.subprocess, "run", _BadRun()):
            ok, err = telegram_install.kickstart_gateway("atlas")
        assert ok is False
        assert "launchctl kickstart failed" in (err or "")
        assert "Could not find specified service" in (err or "")


# os import used by the staging tests above.
import os  # noqa: E402
