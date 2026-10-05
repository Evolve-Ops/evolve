"""tests/test_skills_discord_set_token_route.py — Flask route tests.

Covers the ``POST /api/skills/install/discord/set-token`` route added so
the add-bot wizard's Screen 4 messaging-channel step can install Discord
via a directly-pasted bot token (parallel to Telegram's set-token flow,
rather than the Skills page's pod-level keystore + OAuth invite flow).

All filesystem and network effects are stubbed — no real sudo, no real
Discord API calls. The Skills page's ``/confirm`` route already covers
the keystore path; these tests pin only the wizard contract:

  * ``{bot_id, bot_token}`` accepted (the field name regression the
    wizard kept hitting before).
  * Discord rejects bad token → 422, not silent 200.
  * Token write failure short-circuits openclaw.json + kickstart.
  * openclaw.json write failure short-circuits kickstart.
  * Happy path verifies → writes → wires openclaw.json → kickstarts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.skills import discord_install  # noqa: E402


_BOT = "team_bot_b"
# Obviously-synthetic low-entropy placeholder so gitleaks does not flag
# the literal as a real credential. verify_token is mocked in every test,
# so the actual string is never sent to Discord.
_TOKEN = "Bot.fake.test.token"


@pytest.fixture
def discord_route_app(tmp_path):
    """Flask app with the Discord install routes registered.

    Mirrors fixture shape from test_skills_runway_install.py — a real
    ``create_app`` so all blueprint registration runs, with a minimal
    network.json so bot lookups succeed.
    """
    from evolve_admin.web import server as srv

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {_BOT: {"user": "personal_bot_user"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = srv.create_app(network_path=network_path)
    app.config["TESTING"] = True
    return app


def _ok_verify(_token):
    return True, None, {"id": "111222333444555666", "username": "EvolveBot#0001"}


def _revoked_verify(_token):
    return False, "invalid_token", None


def _valid_status(_bot_id, **_kw):
    return discord_install.InstallStatus(
        bot_id=_BOT,
        token_state="valid",
        bot_user_id="111222333444555666",
        bot_username="EvolveBot#0001",
    )


class TestInputValidation:
    def test_missing_bot_id_rejected(self, discord_route_app):
        r = discord_route_app.test_client().post(
            "/api/skills/install/discord/set-token",
            json={"bot_token": _TOKEN},
        )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_missing_bot_token_rejected(self, discord_route_app):
        """The wizard regression — accepting ``token`` instead of
        ``bot_token`` is exactly the bug this endpoint was created to
        avoid silently 404-ing on. The contract is bot_token."""
        r = discord_route_app.test_client().post(
            "/api/skills/install/discord/set-token",
            json={"bot_id": _BOT, "token": _TOKEN},  # wrong field
        )
        assert r.status_code == 400
        assert r.get_json()["error"] == "bot_token required"

    def test_token_too_long_rejected_before_network(self, discord_route_app):
        """Belt-and-suspenders against a paste of pages of garbage."""
        long_token = "A" * 600
        with patch.object(discord_install, "verify_token") as mock_verify:
            r = discord_route_app.test_client().post(
                "/api/skills/install/discord/set-token",
                json={"bot_id": _BOT, "bot_token": long_token},
            )
        assert r.status_code == 400
        assert r.get_json()["error"] == "token_too_long"
        # Critical: we MUST NOT have hit Discord with the bogus token.
        mock_verify.assert_not_called()


class TestTokenVerification:
    def test_revoked_token_returns_422_with_detail(self, discord_route_app):
        with patch.object(discord_install, "verify_token", _revoked_verify):
            r = discord_route_app.test_client().post(
                "/api/skills/install/discord/set-token",
                json={"bot_id": _BOT, "bot_token": _TOKEN},
            )
        assert r.status_code == 422
        body = r.get_json()
        assert body["error"] == "token_invalid"
        # Detail must be plain-language so the wizard's error chip is
        # actionable, not "unknown".
        assert "invalid_token" in body.get("detail", "")
        assert "Developer Portal" in body.get("detail", "")


class TestEffectOrdering:
    def test_token_write_failure_short_circuits_oc_and_kickstart(
        self, discord_route_app,
    ):
        oc_calls: list = []
        kick_calls: list = []

        def _fail_write(_bot_id, _cfg):
            return False, "fake disk error"

        def _spy_oc(bot_id, _tok):
            oc_calls.append(bot_id)
            return True, None

        def _spy_kick(bot_id):
            kick_calls.append(bot_id)
            return True, None

        with patch.object(discord_install, "verify_token", _ok_verify), \
             patch.object(discord_install, "write_token_config", _fail_write), \
             patch.object(discord_install, "enable_channel_in_oc_config", _spy_oc), \
             patch.object(discord_install, "kickstart_gateway", _spy_kick):
            r = discord_route_app.test_client().post(
                "/api/skills/install/discord/set-token",
                json={"bot_id": _BOT, "bot_token": _TOKEN},
            )

        assert r.status_code == 500
        assert r.get_json()["error"] == "config_write_failed"
        # openclaw.json and kickstart must NOT have fired.
        assert oc_calls == []
        assert kick_calls == []

    def test_oc_write_failure_short_circuits_kickstart(self, discord_route_app):
        kick_calls: list = []

        def _ok_write(_bot_id, _cfg):
            return True, None

        def _fail_oc(_bot_id, _tok):
            return False, "fake oc write error"

        def _spy_kick(bot_id):
            kick_calls.append(bot_id)
            return True, None

        with patch.object(discord_install, "verify_token", _ok_verify), \
             patch.object(discord_install, "write_token_config", _ok_write), \
             patch.object(discord_install, "enable_channel_in_oc_config", _fail_oc), \
             patch.object(discord_install, "kickstart_gateway", _spy_kick):
            r = discord_route_app.test_client().post(
                "/api/skills/install/discord/set-token",
                json={"bot_id": _BOT, "bot_token": _TOKEN},
            )

        assert r.status_code == 500
        assert r.get_json()["error"] == "oc_config_write_failed"
        # Critical: kickstart must NOT have fired, otherwise we'd be
        # restarting a gateway on a half-wired config.
        assert kick_calls == []


class TestHappyPath:
    def test_writes_config_wires_openclaw_and_kickstarts(self, discord_route_app):
        """End-to-end ordering: verify → token write → oc wire → kickstart.

        The wizard's promise is that picking Discord on Screen 4 lands a
        fully-wired Discord channel on the bot. This test pins all four
        effects firing for the right bot in the right order, and the
        response surfacing the resolved status for the gauntlet.
        """
        calls: list = []

        def _spy_write(bot_id, cfg):
            calls.append(("write", bot_id, cfg.get("bot_username")))
            return True, None

        def _spy_oc(bot_id, tok):
            calls.append(("oc", bot_id, tok))
            return True, None

        def _spy_kick(bot_id):
            calls.append(("kick", bot_id))
            return True, None

        with patch.object(discord_install, "verify_token", _ok_verify), \
             patch.object(discord_install, "write_token_config", _spy_write), \
             patch.object(discord_install, "enable_channel_in_oc_config", _spy_oc), \
             patch.object(discord_install, "kickstart_gateway", _spy_kick), \
             patch.object(discord_install, "resolve_status", _valid_status):
            r = discord_route_app.test_client().post(
                "/api/skills/install/discord/set-token",
                json={"bot_id": _BOT, "bot_token": _TOKEN},
            )

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body.get("token_state") == "valid"
        assert body.get("gateway_kickstarted") is True

        # All three effects fired for the right bot, in the right order.
        assert [c[0] for c in calls] == ["write", "oc", "kick"]
        assert calls[0][1] == _BOT
        assert calls[0][2] == "EvolveBot#0001"
        assert calls[1] == ("oc", _BOT, _TOKEN)
        assert calls[2] == ("kick", _BOT)

    def test_kickstart_failure_still_returns_ok_with_diagnostic(
        self, discord_route_app,
    ):
        """Best-effort kickstart — the per-bot config + openclaw.json are
        already on disk, so a kickstart hiccup should not roll the
        install back. Surface the failure so the operator can restart
        manually, but keep the install otherwise complete (same shape
        as Telegram's set-token endpoint)."""

        def _kick_fail(_bot_id):
            return False, "launchctl returned 3"

        with patch.object(discord_install, "verify_token", _ok_verify), \
             patch.object(discord_install, "write_token_config",
                          lambda _b, _c: (True, None)), \
             patch.object(discord_install, "enable_channel_in_oc_config",
                          lambda _b, _t: (True, None)), \
             patch.object(discord_install, "kickstart_gateway", _kick_fail), \
             patch.object(discord_install, "resolve_status", _valid_status):
            r = discord_route_app.test_client().post(
                "/api/skills/install/discord/set-token",
                json={"bot_id": _BOT, "bot_token": _TOKEN},
            )

        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["gateway_kickstarted"] is False
        assert body["gateway_kickstart_error"] == "launchctl returned 3"
