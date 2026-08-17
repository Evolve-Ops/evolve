"""End-to-end tests for the Slack-policy admin-UI routes.

Wires the routes onto a tiny Flask app, points network at a tmp dir,
and exercises the GET / init / save / apply happy + refuse paths.
The slack_client is dependency-injected via the doctor's
``slack_client_factory`` hook so no test touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from flask import Flask

from evolve_admin.web.slack_routes import register_slack_policy_routes


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers: network.json + a bot home that holds an openclaw.json
# ─────────────────────────────────────────────────────────────────────────────


def _write_network(tmp_path: Path, bot_id: str, bot_home: Path) -> Path:
    """Build the minimal network.json the routes need to resolve a bot."""
    import pwd
    # We need pwd.getpwnam(user) to resolve to bot_home — but the route
    # ultimately calls config.bot_home() which goes through pwd. To avoid
    # touching the OS user db, point the bot's user to the current user
    # (whose home is real) and override sharedDir to tmp_path.
    try:
        current_user = pwd.getpwuid(__import__("os").geteuid()).pw_name
    except Exception:
        current_user = "evolve"
    network = {
        "primary": bot_id,
        "members": [bot_id],
        "bots": {bot_id: {"user": current_user, "port": 19000, "role": "primary"}},
        "sharedDir": str(tmp_path / "shared"),
    }
    path = tmp_path / "network.json"
    path.write_text(json.dumps(network))
    return path


def _build_app(network_path: Path) -> Flask:
    app = Flask(__name__)
    register_slack_policy_routes(app, network_path)
    app.config["TESTING"] = True
    return app


def _stage_bot_openclaw(bot_home: Path, payload: dict) -> None:
    """Write an openclaw.json into the bot home for the route to read."""
    (bot_home / ".openclaw").mkdir(parents=True, exist_ok=True)
    (bot_home / ".openclaw" / "openclaw.json").write_text(json.dumps(payload))


def _stub_slack_client(monkeypatch, *, auth_ok: bool = True,
                       member_channels: "list[dict] | None" = None,
                       scopes: "list[str] | None" = None) -> None:
    """Replace SlackClient at every import site the doctor uses.

    The doctor does ``from .slack_client import SlackClient`` — once at
    module load — so monkeypatching ``slack_client.SlackClient`` after
    that doesn't affect the doctor's already-bound reference. Patch
    the bound name on the doctor module directly.
    """
    from evolve_admin.integrations.slack import doctor as doctor_mod
    from evolve_admin.integrations.slack.slack_client import SlackError

    class StubClient:
        def __init__(self, token: str) -> None:
            pass
        def auth_test(self):
            if not auth_ok:
                return None, SlackError(
                    code="api_error", detail="", slack_error="invalid_auth",
                )
            return {
                "team": "Acme", "team_id": "T1", "user": "team_bot_a",
                "user_id": "U0BOT", "bot_id": "B1",
                "_scopes": list(scopes or ["app_mentions:read", "chat:write"]),
            }, None
        def users_conversations(self, **_):
            return list(member_channels or []), None
        def users_info(self, uid):
            return {"id": uid, "name": "x"}, None

    monkeypatch.setattr(doctor_mod, "SlackClient", StubClient)


def _slack_payload(*, channels: dict | None = None, allow_from: list[str] | None = None) -> dict:
    slack = {
        "botToken": "xoxb-test",
        "groupPolicy": "allowlist",
        "dmPolicy": "pairing",
        "enabled": True,
        "mode": "socket",
        "appToken": "xapp-test",
    }
    if channels is not None:
        slack["channels"] = channels
    if allow_from is not None:
        slack["allowFrom"] = allow_from
    return {
        "channels": {"slack": slack},
        "messages": {"groupChat": {"visibleReplies": "automatic"}},
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/bot/slack-policy
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSlackPolicy:
    def test_unknown_bot_returns_404(self, tmp_path: Path) -> None:
        network = _write_network(tmp_path, "team_bot_a", tmp_path / "team_bot_a_home")
        app = _build_app(network)
        with app.test_client() as c:
            r = c.get("/api/bot/slack-policy?bot=nonexistent")
            assert r.status_code == 404

    def test_missing_bot_arg_returns_404(self, tmp_path: Path) -> None:
        network = _write_network(tmp_path, "team_bot_a", tmp_path / "team_bot_a_home")
        app = _build_app(network)
        with app.test_client() as c:
            r = c.get("/api/bot/slack-policy")
            assert r.status_code == 404

    def test_returns_doctor_state_for_known_bot(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A configured bot returns doctor state. We patch the slack client
        the doctor would construct so the route doesn't try to call Slack."""
        import pwd
        current_user = pwd.getpwuid(__import__("os").geteuid()).pw_name
        bot_home = Path(pwd.getpwnam(current_user).pw_dir) / "_test_team_bot_a_home_evolve"
        try:
            bot_home.mkdir(parents=True, exist_ok=True)
            _stage_bot_openclaw(bot_home, _slack_payload(
                channels={"G0T79FGSE": {"requireMention": False}},
                allow_from=["U0AAA", "U0BBB"],
            ))
            # Point `bot_home(bot_id)` at our test directory by monkeypatching
            from evolve_admin import config as admin_config
            monkeypatch.setattr(admin_config, "bot_home", lambda bid, net=None: bot_home)
            # Stub SlackClient at the doctor's import site so no network call
            _stub_slack_client(
                monkeypatch,
                member_channels=[{"id": "G0T79FGSE", "name": "ops"}],
                scopes=["app_mentions:read", "chat:write"],
            )

            network = _write_network(tmp_path, "team_bot_a", bot_home)
            app = _build_app(network)
            with app.test_client() as c:
                r = c.get("/api/bot/slack-policy?bot=team_bot_a")
                assert r.status_code == 200
                data = r.get_json()
                assert data["bot_id"] == "team_bot_a"
                assert data["policy_exists"] is False
                assert data["doctor"]["slack_enabled"] is True
                assert data["doctor"]["transport_mode"] == "socket"
                assert data["doctor"]["dm_policy"] == "pairing"
                # listening channels exposed
                listening = data["doctor"]["listening_channels"]
                assert len(listening) == 1
                assert listening[0]["channel_id"] == "G0T79FGSE"
                assert listening[0]["display_name"] == "ops"
                assert listening[0]["is_joined"] is True
                # feature bundles + scopes flow through
                assert data["doctor"]["oauth_scopes_count"] == 2
                assert any(b["name"] == "Respond to @-mentions" and b["enabled"]
                           for b in data["doctor"]["feature_bundles"])
        finally:
            import shutil
            shutil.rmtree(bot_home, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/bot/slack-policy/init
# ─────────────────────────────────────────────────────────────────────────────


class TestInitSlackPolicy:
    def test_unknown_bot_returns_404(self, tmp_path: Path) -> None:
        network = _write_network(tmp_path, "team_bot_a", tmp_path / "team_bot_a_home")
        app = _build_app(network)
        with app.test_client() as c:
            r = c.post("/api/bot/slack-policy/init?bot=ghost", json={})
            assert r.status_code == 404

    def test_refuses_when_openclaw_has_FAIL(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Encoding bug 1 (name-keyed channel) into the policy would
        re-render the bug — route must refuse without --force."""
        import pwd
        current_user = pwd.getpwuid(__import__("os").geteuid()).pw_name
        bot_home = Path(pwd.getpwnam(current_user).pw_dir) / "_test_team_bot_a_init_fail"
        try:
            bot_home.mkdir(parents=True, exist_ok=True)
            _stage_bot_openclaw(bot_home, _slack_payload(
                channels={"project-x": {"requireMention": False}},  # bug 1
            ))
            from evolve_admin import config as admin_config
            monkeypatch.setattr(admin_config, "bot_home", lambda bid, net=None: bot_home)
            network = _write_network(tmp_path, "team_bot_a", bot_home)
            app = _build_app(network)
            with app.test_client() as c:
                r = c.post("/api/bot/slack-policy/init?bot=team_bot_a", json={"force": False})
                assert r.status_code == 422
                body = r.get_json()
                assert body["error"] == "refused_by_doctor"
                assert any(f["code"] == "SLK001" for f in body.get("fails", []))
        finally:
            import shutil
            shutil.rmtree(bot_home, ignore_errors=True)

    def test_happy_path_writes_policy(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import pwd
        current_user = pwd.getpwuid(__import__("os").geteuid()).pw_name
        bot_home = Path(pwd.getpwnam(current_user).pw_dir) / "_test_team_bot_a_init_ok"
        try:
            bot_home.mkdir(parents=True, exist_ok=True)
            _stage_bot_openclaw(bot_home, _slack_payload(
                channels={"G0T79FGSE": {"requireMention": False}},
            ))
            from evolve_admin import config as admin_config
            monkeypatch.setattr(admin_config, "bot_home", lambda bid, net=None: bot_home)
            _stub_slack_client(monkeypatch,
                               member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
            network = _write_network(tmp_path, "team_bot_a", bot_home)
            app = _build_app(network)
            with app.test_client() as c:
                r = c.post("/api/bot/slack-policy/init?bot=team_bot_a", json={})
                assert r.status_code == 200, r.get_json()
                body = r.get_json()
                assert body["ok"] is True
                # The policy file should now exist
                policy_file = tmp_path / "shared" / "bots" / "team_bot_a" / "slack-policy.json"
                assert policy_file.exists()
                stored = json.loads(policy_file.read_text())
                assert stored["bot_id"] == "team_bot_a"
                assert any(
                    e["channel_id"] == "G0T79FGSE" for e in stored["channels"]["entries"]
                )
                # Second init refuses (policy already exists)
                r2 = c.post("/api/bot/slack-policy/init?bot=team_bot_a", json={"force": False})
                assert r2.status_code == 409
        finally:
            import shutil
            shutil.rmtree(bot_home, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/bot/slack-policy/save  (the UI's edit path)
# ─────────────────────────────────────────────────────────────────────────────


class TestSaveSlackPolicy:
    def test_invalid_body_returns_400(self, tmp_path: Path) -> None:
        network = _write_network(tmp_path, "team_bot_a", tmp_path / "team_bot_a_home")
        app = _build_app(network)
        with app.test_client() as c:
            r = c.post("/api/bot/slack-policy/save?bot=team_bot_a",
                       data="not json", content_type="application/json")
            assert r.status_code == 400

    def test_save_overrides_bot_id_in_body(self, tmp_path: Path) -> None:
        """The route should never trust the client to set bot_id —
        a typo in the body must NOT cross-write another bot's policy.
        """
        network = _write_network(tmp_path, "team_bot_a", tmp_path / "team_bot_a_home")
        app = _build_app(network)
        body = {
            "bot_id": "other_bot",  # ignored — URL-bot wins
            "schema_version": 1,
            "channels": {"entries": [{"channel_id": "G0T79FGSE"}]},
            "messaging": {"visible_replies_default": "automatic"},
        }
        with app.test_client() as c:
            r = c.post("/api/bot/slack-policy/save?bot=team_bot_a", json=body)
            assert r.status_code == 200, r.get_json()
            # File lands under team_bot_a's dir, not other_bot's
            assert (tmp_path / "shared" / "bots" / "team_bot_a" / "slack-policy.json").exists()
            assert not (tmp_path / "shared" / "bots" / "other_bot").exists()

    def test_invalid_policy_rejected(self, tmp_path: Path) -> None:
        network = _write_network(tmp_path, "team_bot_a", tmp_path / "team_bot_a_home")
        app = _build_app(network)
        body = {
            "schema_version": 1,
            "channels": {"entries": [{"channel_id": "project-x"}]},  # name-keyed
            "messaging": {"visible_replies_default": "automatic"},
        }
        with app.test_client() as c:
            r = c.post("/api/bot/slack-policy/save?bot=team_bot_a", json=body)
            assert r.status_code == 422
            assert "not a Slack ID" in r.get_json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/bot/slack-policy/apply
# ─────────────────────────────────────────────────────────────────────────────


class TestApplySlackPolicy:
    def test_no_policy_returns_404(self, tmp_path: Path) -> None:
        network = _write_network(tmp_path, "team_bot_a", tmp_path / "team_bot_a_home")
        app = _build_app(network)
        with app.test_client() as c:
            r = c.post("/api/bot/slack-policy/apply?bot=team_bot_a", json={})
            assert r.status_code == 404
            assert r.get_json()["error"] == "no_policy"
