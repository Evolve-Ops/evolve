"""tests/test_skills_slack_install.py — Slack skill install flow.

Covers the contract for the V2.1-2 Slack skill:

  - resolve_status routes by (credentials present, token present, auth.test
    result) into the five-state machine.
  - build_install_plan returns the right ordered steps for each state.
  - The plain-language access panel ships with the OAuth step.
  - validate_callback accepts/rejects state tokens correctly.
  - exchange_code_for_token shapes the Slack oauth.v2.access call correctly.
  - Hostile inputs: missing credentials, network errors, malformed callbacks.
  - Token write/read helpers call the right paths (no hardcoded /Users/{bot_id}).

OAuth is mocked at the reader boundary — no real Slack API traffic.
No filesystem writes in tests — write_token_config is stubbed via injectable
read_token / check_workspace callables.

Trust-chain notes:
  - Bot tokens are never logged or returned verbatim in test assertions.
  - resolve_status delegates to injectable check_workspace so token validation
    is exercised without real network traffic.
  - The state-token store is an in-process singleton; tests that create states
    must use different bot_id/state values to avoid cross-test interference.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import slack_install  # noqa: E402


# ── Reader/checker helpers ────────────────────────────────────────────────────


def _no_token(_bot_id):
    """No Slack token stored yet."""
    return None


def _valid_token_config(_bot_id):
    """Valid token config stored on disk."""
    return {
        "bot_token": "xoxb-111-222-abc",
        "workspace_id": "T12345",
        "workspace_name": "Test Workspace",
        "bot_user_id": "U99999",
        "scopes": ["chat:write", "channels:read", "im:read", "im:write"],
        "issued_at": 1_700_000_000,
    }


def _revoked_token_config(_bot_id):
    """Token config stored but token is revoked."""
    return {
        "bot_token": "xoxb-old-revoked",
        "workspace_id": "T12345",
        "workspace_name": "Test Workspace",
        "bot_user_id": "U99999",
        "scopes": ["chat:write"],
        "issued_at": 1_600_000_000,
    }


def _workspace_ok(_token):
    """auth.test says token is valid."""
    return True, None


def _workspace_revoked(_token):
    """auth.test says token is revoked."""
    return False, "token_revoked"


def _workspace_network_error(_token):
    """auth.test raises a network error."""
    raise ConnectionError("simulated network failure")


# ── TestResolveStatus ─────────────────────────────────────────────────────────


class TestResolveStatus:
    """State machine: credentials_missing → missing → valid / revoked / unknown."""

    def test_credentials_missing_when_no_shared_dir_creds(self, tmp_path):
        """credentials_missing when keystore files are absent."""
        # shared_dir with no keystore files
        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,  # empty tmp dir — no keystore subdir
            read_token=_no_token,
            check_workspace=_workspace_ok,
        )
        assert status.token_state == "credentials_missing"
        assert status.error is not None
        assert "slack-setup.md" in status.error

    def test_credentials_missing_is_pod_level_preemption(self, tmp_path):
        """credentials_missing pre-empts even when token exists."""
        # Even if a token is stored, we check credentials first
        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,  # no creds
            read_token=_valid_token_config,
            check_workspace=_workspace_ok,
        )
        assert status.token_state == "credentials_missing"

    def test_missing_when_no_token_stored(self, tmp_path):
        """missing when credentials are present but no token stored."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "slack-client-id").write_text("C123")
        (ks / "slack-client-secret").write_text("S456")

        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_no_token,
            check_workspace=_workspace_ok,
        )
        assert status.token_state == "missing"
        assert status.workspace_id is None

    def test_valid_when_token_passes_auth_test(self, tmp_path):
        """valid when token stored and auth.test passes."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "slack-client-id").write_text("C123")
        (ks / "slack-client-secret").write_text("S456")

        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_valid_token_config,
            check_workspace=_workspace_ok,
        )
        assert status.token_state == "valid"
        assert status.workspace_id == "T12345"
        assert status.workspace_name == "Test Workspace"
        assert status.bot_user_id == "U99999"
        assert "chat:write" in status.scopes_granted

    def test_revoked_when_auth_test_fails(self, tmp_path):
        """revoked when token stored but auth.test rejects it."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "slack-client-id").write_text("C123")
        (ks / "slack-client-secret").write_text("S456")

        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_revoked_token_config,
            check_workspace=_workspace_revoked,
        )
        assert status.token_state == "revoked"
        assert status.error == "token_revoked"
        assert status.workspace_id == "T12345"

    def test_unknown_when_auth_test_raises(self, tmp_path):
        """unknown when auth.test raises (network error)."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "slack-client-id").write_text("C123")
        (ks / "slack-client-secret").write_text("S456")

        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_valid_token_config,
            check_workspace=_workspace_network_error,
        )
        assert status.token_state == "unknown"
        assert "auth_test_failed" in (status.error or "")

    def test_resolve_without_shared_dir_uses_injected_check(self):
        """When shared_dir is None, credential check is skipped; caller owns it."""
        # This path is used by unit tests that don't need the keystore check.
        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=None,  # skip credential check
            read_token=_no_token,
            check_workspace=_workspace_ok,
        )
        assert status.token_state == "missing"

    def test_resolve_valid_without_shared_dir(self):
        """Valid token, no shared_dir — credential check is caller's responsibility."""
        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=None,
            read_token=_valid_token_config,
            check_workspace=_workspace_ok,
        )
        assert status.token_state == "valid"

    def test_to_dict_shape(self, tmp_path):
        """to_dict() includes all required keys."""
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "slack-client-id").write_text("C123")
        (ks / "slack-client-secret").write_text("S456")

        status = slack_install.resolve_status(
            "admin_bot",
            shared_dir=tmp_path,
            read_token=_valid_token_config,
            check_workspace=_workspace_ok,
        )
        d = status.to_dict()
        assert d["skill_id"] == "slack"
        assert d["token_state"] == "valid"
        assert d["status"] == "valid"  # alias field
        assert "workspace_id" in d
        assert "workspace_name" in d
        assert "bot_user_id" in d
        assert "scopes_granted" in d


# ── TestBuildInstallPlan ──────────────────────────────────────────────────────


class TestBuildInstallPlan:
    """build_install_plan returns the right steps for each token_state."""

    def _make_status(self, token_state, *, bot_id="admin_bot", workspace_name=None, error=None):
        return slack_install.InstallStatus(
            bot_id=bot_id,
            token_state=token_state,
            workspace_name=workspace_name,
            error=error,
        )

    def test_credentials_missing_has_one_configure_step(self):
        st = self._make_status("credentials_missing")
        plan = slack_install.build_install_plan(st)
        assert len(plan) == 1
        assert plan[0].id == "configure_credentials"
        assert "slack-setup.md" in (plan[0].payload.get("hint") or "")

    def test_missing_has_oauth_and_confirm_steps(self):
        st = self._make_status("missing")
        plan = slack_install.build_install_plan(st)
        assert len(plan) == 2
        ids = [s.id for s in plan]
        assert "oauth" in ids
        assert "confirm" in ids

    def test_missing_oauth_step_has_access_panel(self):
        st = self._make_status("missing")
        plan = slack_install.build_install_plan(st)
        oauth_step = next(s for s in plan if s.id == "oauth")
        assert oauth_step.access_panel is not None
        # Access panel must have will/wont lists
        assert "will" in oauth_step.access_panel
        assert "wont" in oauth_step.access_panel

    def test_revoked_has_same_steps_as_missing(self):
        st_missing = self._make_status("missing")
        st_revoked = self._make_status("revoked")
        plan_missing = slack_install.build_install_plan(st_missing)
        plan_revoked = slack_install.build_install_plan(st_revoked)
        assert [s.id for s in plan_missing] == [s.id for s in plan_revoked]

    def test_valid_returns_empty_plan(self):
        st = self._make_status("valid")
        plan = slack_install.build_install_plan(st)
        assert plan == []

    def test_unknown_returns_empty_plan(self):
        st = self._make_status("unknown", error="network_error")
        plan = slack_install.build_install_plan(st)
        assert plan == []

    def test_step_to_dict_shape(self):
        st = self._make_status("missing")
        plan = slack_install.build_install_plan(st)
        for step in plan:
            d = step.to_dict()
            assert "id" in d
            assert "label" in d
            assert "endpoint" in d
            assert "payload" in d
            assert "access_panel" in d

    def test_oauth_endpoint_points_to_slack_route(self):
        st = self._make_status("missing")
        plan = slack_install.build_install_plan(st)
        oauth_step = next(s for s in plan if s.id == "oauth")
        assert "/api/skills/install/slack" in (oauth_step.endpoint or "")

    def test_confirm_endpoint_points_to_slack_status(self):
        st = self._make_status("missing")
        plan = slack_install.build_install_plan(st)
        confirm_step = next(s for s in plan if s.id == "confirm")
        assert "/api/skills/install/slack" in (confirm_step.endpoint or "")
        assert confirm_step.payload.get("bot_id") == "admin_bot"


# ── TestValidateCallback ──────────────────────────────────────────────────────


class TestValidateCallback:
    """validate_callback accepts / rejects state tokens correctly."""

    def test_valid_state_and_code_passes(self):
        # Create a live state entry first
        state = slack_install.slack_state_create("test-bot-validate", "http://localhost/cb")
        ok, err = slack_install.validate_callback(state, "some-code-xyz")
        assert ok is True
        assert err is None

    def test_empty_state_fails(self):
        ok, err = slack_install.validate_callback("", "some-code")
        assert ok is False
        assert err == "missing_state"

    def test_empty_code_fails(self):
        state = slack_install.slack_state_create("test-bot-empty-code", "http://localhost/cb")
        ok, err = slack_install.validate_callback(state, "")
        assert ok is False
        assert err == "missing_code"

    def test_unknown_state_fails(self):
        ok, err = slack_install.validate_callback("not-a-real-state-token", "some-code")
        assert ok is False
        assert err == "unknown_or_expired_state"

    def test_expired_state_fails(self):
        """An entry that expires in the past is rejected."""
        import secrets
        state = secrets.token_urlsafe(24)
        with slack_install._SLACK_OAUTH_STATE_LOCK:
            slack_install._SLACK_OAUTH_STATE[state] = {
                "bot_id": "test-bot-expired",
                "redirect_uri": "http://localhost/cb",
                "expires_at": time.time() - 1,  # already expired
                "result": {"status": "pending"},
            }
        ok, err = slack_install.validate_callback(state, "some-code")
        assert ok is False
        assert err == "unknown_or_expired_state"


# ── TestStateStore ────────────────────────────────────────────────────────────


class TestStateStore:
    """In-memory state store: create, get, set_result, consume."""

    def test_create_and_get(self):
        state = slack_install.slack_state_create("bot-1", "http://localhost/cb")
        entry = slack_install.slack_state_get(state)
        assert entry is not None
        assert entry["bot_id"] == "bot-1"
        assert entry["result"]["status"] == "pending"

    def test_unknown_state_returns_none(self):
        assert slack_install.slack_state_get("not-a-state") is None

    def test_set_result_updates_entry(self):
        state = slack_install.slack_state_create("bot-2", "http://localhost/cb")
        ok = slack_install.slack_state_set_result(state, {"status": "success", "workspace_name": "Test"})
        assert ok is True
        entry = slack_install.slack_state_get(state)
        assert entry["result"]["status"] == "success"

    def test_consume_pops_state(self):
        state = slack_install.slack_state_create("bot-3", "http://localhost/cb")
        slack_install.slack_state_set_result(state, {"status": "success"})
        popped = slack_install.slack_state_consume(state)
        assert popped is not None
        # After consume, it's gone
        assert slack_install.slack_state_get(state) is None


# ── TestExchangeCodeForToken ──────────────────────────────────────────────────


class TestExchangeCodeForToken:
    """exchange_code_for_token calls Slack's oauth.v2.access correctly."""

    def test_successful_exchange_returns_ok(self):
        """Mock a successful Slack token exchange."""
        mock_body = {
            "ok": True,
            "access_token": "xoxb-111-222-abc",
            "token_type": "bot",
            "scope": "chat:write,channels:read",
            "bot_user_id": "U99999",
            "team": {"id": "T12345", "name": "Test Workspace"},
        }
        with patch.object(slack_install, "_slack_http_post_json", return_value=(200, mock_body)):
            ok, body, err = slack_install.exchange_code_for_token(
                "auth-code-123", "client-id", "client-secret", "http://localhost/cb"
            )
        assert ok is True
        assert body["access_token"] == "xoxb-111-222-abc"
        assert err is None

    def test_slack_error_returns_false(self):
        """Slack returns ok=false (e.g., invalid_code)."""
        mock_body = {"ok": False, "error": "invalid_code"}
        with patch.object(slack_install, "_slack_http_post_json", return_value=(200, mock_body)):
            ok, body, err = slack_install.exchange_code_for_token(
                "bad-code", "client-id", "client-secret", "http://localhost/cb"
            )
        assert ok is False
        assert err == "invalid_code"

    def test_http_error_returns_false(self):
        """HTTP 500 from Slack returns false."""
        with patch.object(slack_install, "_slack_http_post_json", return_value=(500, None)):
            ok, body, err = slack_install.exchange_code_for_token(
                "code", "client-id", "client-secret", "http://localhost/cb"
            )
        assert ok is False
        assert "500" in (err or "")

    def test_network_error_returns_false(self):
        """Network failure (status 0) returns false."""
        with patch.object(slack_install, "_slack_http_post_json", return_value=(0, None)):
            ok, body, err = slack_install.exchange_code_for_token(
                "code", "client-id", "client-secret", "http://localhost/cb"
            )
        assert ok is False


# ── TestAccessPanel ───────────────────────────────────────────────────────────


class TestAccessPanel:
    """The plain-language access panel must be Plex-test friendly."""

    def test_access_panel_has_will_and_wont_lists(self):
        panel = slack_install.SLACK_ACCESS_PANEL
        assert "will" in panel
        assert "wont" in panel
        assert len(panel["will"]) > 0
        assert len(panel["wont"]) > 0

    def test_access_panel_no_oauth_jargon(self):
        panel = slack_install.SLACK_ACCESS_PANEL
        full_text = " ".join([
            panel.get("summary", ""),
            *panel.get("will", []),
            *panel.get("wont", []),
        ])
        for jargon in ("scope", "token", "oauth", "grant"):
            assert jargon.lower() not in full_text.lower(), (
                f"Access panel contains jargon word '{jargon}'"
            )

    def test_access_panel_mentions_slack(self):
        panel = slack_install.SLACK_ACCESS_PANEL
        assert "Slack" in (panel.get("summary") or "") or "Slack" in panel.get("skill_display_name", "")

    def test_wont_list_mentions_no_read_of_messages(self):
        """Users should see that the bot won't read message history."""
        wont = " ".join(slack_install.SLACK_ACCESS_PANEL.get("wont", []))
        # Should mention reading history as something NOT done
        assert any(
            word in wont.lower()
            for word in ("history", "read", "content")
        )


# ── TestCredentialHelpers ─────────────────────────────────────────────────────


class TestCredentialHelpers:
    """read_slack_credentials reads from the keystore correctly."""

    def test_reads_present_credentials(self, tmp_path):
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "slack-client-id").write_text("MY_CLIENT_ID\n")
        (ks / "slack-client-secret").write_text("MY_CLIENT_SECRET\n")

        client_id, client_secret = slack_install.read_slack_credentials(tmp_path)
        assert client_id == "MY_CLIENT_ID"
        assert client_secret == "MY_CLIENT_SECRET"

    def test_returns_none_when_files_missing(self, tmp_path):
        client_id, client_secret = slack_install.read_slack_credentials(tmp_path)
        assert client_id is None
        assert client_secret is None

    def test_returns_none_when_files_empty(self, tmp_path):
        ks = tmp_path / "keystore"
        ks.mkdir()
        (ks / "slack-client-id").write_text("")
        (ks / "slack-client-secret").write_text("")

        client_id, client_secret = slack_install.read_slack_credentials(tmp_path)
        assert client_id is None
        assert client_secret is None


# ── TestSkillRegistryEntry ────────────────────────────────────────────────────


class TestSkillRegistryEntry:
    """SKILL_REGISTRY_ENTRY has the right shape for inventory.py."""

    def test_entry_has_required_keys(self):
        reg = slack_install.SKILL_REGISTRY_ENTRY
        assert reg["id"] == "slack"
        assert "display_name" in reg
        assert "summary" in reg
        assert "access_panel" in reg
        assert "default_scopes" in reg

    def test_default_scopes_are_conservative(self):
        scopes = slack_install.SLACK_DEFAULT_SCOPES
        # Must include chat:write to be useful
        assert "chat:write" in scopes
        # Must NOT include admin-level scopes
        for scope in scopes:
            assert not scope.startswith("admin"), (
                f"Scope {scope!r} is admin-level — should not be in defaults"
            )

    def test_skill_id_constant(self):
        assert slack_install.SLACK_SKILL_ID == "slack"


# ── TestEnableChannelInOcConfig ───────────────────────────────────────────────


import os  # noqa: E402


class _FakeSubprocessRun:
    """Capture-only stub for subprocess.run. On a sudo /bin/cp call it stashes
    the staged file's contents in ``self.staged_payload`` so tests can assert
    what the helper would have shipped to /tmp before the finally clause
    unlinks it."""

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
    """enable_channel_in_oc_config: merge channels.slack + plugins.entries.slack
    into openclaw.json, preserving operator-set fields like appToken / mode."""

    def _stage(self, tmp_path):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            real_tmp = tmp_path / "stage.json"
            fd = os.open(str(real_tmp), os.O_WRONLY | os.O_CREAT, 0o600)
            with patch("tempfile.mkstemp", return_value=(fd, str(real_tmp))):
                yield
        return _ctx()

    def test_adds_channels_and_plugin_entry_when_absent(self, tmp_path):
        """Empty openclaw.json gets channels.slack + plugins.entries.slack."""
        from evolve_admin.skills import _oc_install_common as _oc_common

        def _fake_read(_bot_id):
            return {}, None

        fake_run = _FakeSubprocessRun()
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network", return_value={"bots": {"team_bot_a": {"user": "team_bot_a"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._stage(tmp_path):
            ok, err = slack_install.enable_channel_in_oc_config("team_bot_a", "xoxb-test-token")

        assert ok, err
        assert fake_run.staged_payload is not None
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        sl = staged["channels"]["slack"]
        assert sl["enabled"] is True
        assert sl["botToken"] == "xoxb-test-token"
        assert sl["dmPolicy"] == "pairing"
        assert sl["groupPolicy"] == "allowlist"
        assert sl["streaming"] == {"mode": "off"}
        assert sl["userTokenReadOnly"] is True
        # Mode is NOT defaulted — operator picks socket vs webhook.
        assert "mode" not in sl
        assert staged["plugins"]["entries"]["slack"]["enabled"] is True

    def test_preserves_existing_app_token_and_mode(self, tmp_path):
        """Operator-set appToken / mode / channel allowlist survive a redo."""
        from evolve_admin.skills import _oc_install_common as _oc_common

        existing = {
            "channels": {
                "slack": {
                    "enabled": False,
                    "mode": "socket",
                    "botToken": "xoxb-old",
                    "appToken": "xapp-operator-set",
                    "allowFrom": ["U123", "U456"],
                    "channels": {"C0AN8B04U4U": {"requireMention": True}},
                    "dmPolicy": "owner-only",
                }
            },
            "plugins": {"entries": {"slack": {"enabled": False}}},
        }

        def _fake_read(_bot_id):
            return existing, None

        fake_run = _FakeSubprocessRun()
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network", return_value={"bots": {"team_bot_a": {"user": "team_bot_a"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._stage(tmp_path):
            ok, err = slack_install.enable_channel_in_oc_config("team_bot_a", "xoxb-new")

        assert ok, err
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        sl = staged["channels"]["slack"]
        # Operator-set keys preserved
        assert sl["mode"] == "socket"
        assert sl["appToken"] == "xapp-operator-set"
        assert sl["allowFrom"] == ["U123", "U456"]
        assert sl["channels"] == {"C0AN8B04U4U": {"requireMention": True}}
        assert sl["dmPolicy"] == "owner-only"
        # Install flow overrides
        assert sl["enabled"] is True
        assert sl["botToken"] == "xoxb-new"
        assert staged["plugins"]["entries"]["slack"]["enabled"] is True

    def test_strips_legacy_token_field(self, tmp_path):
        """A legacy `token` key is removed in favour of botToken."""
        from evolve_admin.skills import _oc_install_common as _oc_common

        existing = {"channels": {"slack": {"token": "legacy-shape"}}}

        def _fake_read(_bot_id):
            return existing, None

        fake_run = _FakeSubprocessRun()
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run), \
             patch("evolve_admin.config.load_network", return_value={"bots": {"team_bot_a": {"user": "team_bot_a"}}}), \
             patch("evolve_admin.config.bot_home", return_value=tmp_path), \
             self._stage(tmp_path):
            ok, err = slack_install.enable_channel_in_oc_config("team_bot_a", "xoxb-new")

        assert ok, err
        import json as _json
        staged = _json.loads(fake_run.staged_payload)
        assert "token" not in staged["channels"]["slack"]
        assert staged["channels"]["slack"]["botToken"] == "xoxb-new"

    def test_returns_error_on_oc_read_failure(self):
        """If reading the bot's openclaw.json fails, the helper does NOT write."""
        from evolve_admin.skills import _oc_install_common as _oc_common

        def _fake_read(_bot_id):
            return None, "oc_read_failed: PermissionError"

        fake_run = _FakeSubprocessRun()
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common.subprocess, "run", fake_run):
            ok, err = slack_install.enable_channel_in_oc_config("team_bot_a", "xoxb-x")

        assert ok is False
        assert "oc_read_failed" in (err or "")
        assert not any(c[0][:2] == ["sudo", "/bin/cp"] for c in fake_run.calls)


class TestKickstartGateway:
    """kickstart_gateway re-export hits the shared launchctl helper."""

    def test_invokes_launchctl_with_per_bot_label(self):
        from evolve_admin.skills import _oc_install_common as _oc_common
        fake_run = _FakeSubprocessRun()
        with patch.object(_oc_common.subprocess, "run", fake_run):
            ok, err = slack_install.kickstart_gateway("team_bot_a")
        assert ok, err
        argv, _ = fake_run.calls[0]
        assert argv == [
            "sudo", "/bin/launchctl", "kickstart", "-k",
            "system/ai.openclaw.team_bot_a-gateway",
        ]
