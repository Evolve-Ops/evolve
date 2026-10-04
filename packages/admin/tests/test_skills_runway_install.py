"""Tests for evolve_admin.skills.runway.

Mirrors the Notion / Linear test layout: token format validation, the
verify_token state mapping (HTTP stubbed), the resolve_status state
machine, install plan shape, and Flask route behaviour. The Runway API
is never contacted — _runway_get_json is stubbed in every test.

The strict-128-hex format check is the most important pin: Runway's own
error message tells the user keys must be ``key_`` + 128 hex chars, so
client-side rejection of obviously-wrong pastes saves a round-trip and
gives an actionable error instantly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.skills import runway_install as runway


# Helper: a token that passes _token_looks_valid (key_ + 128 hex chars).
GOOD_TOKEN = "key_" + ("a" * 128)


# ── Token format validation ───────────────────────────────────────────────────

class TestTokenFormat:
    def test_accepts_exact_key_plus_128_hex(self):
        assert runway._token_looks_valid(GOOD_TOKEN) is True

    def test_accepts_mixed_case_hex(self):
        """Hex characters are case-insensitive — both letterforms must pass."""
        assert runway._token_looks_valid("key_" + ("aBcDeF" * 21) + "ab") is True

    def test_rejects_missing_key_prefix(self):
        """Runway's own error message says keys must begin with key_;
        bouncing client-side avoids a wasteful round-trip."""
        assert runway._token_looks_valid("a" * 132) is False

    def test_rejects_wrong_length(self):
        assert runway._token_looks_valid("key_" + ("a" * 64)) is False
        assert runway._token_looks_valid("key_" + ("a" * 130)) is False

    def test_rejects_non_hex_chars(self):
        # Underscore inside the hex body is invalid.
        assert runway._token_looks_valid("key_" + ("a" * 64) + "_" + ("a" * 63)) is False

    def test_rejects_empty(self):
        assert runway._token_looks_valid("") is False
        assert runway._token_looks_valid(None) is False


# ── verify_token state mapping ────────────────────────────────────────────────

class TestVerifyToken:
    def test_valid_on_200_with_org_data(self):
        body = {"id": "org-1", "name": "Acme Studio", "tier": "pro"}
        with patch.object(runway, "_runway_get_json", return_value=(200, body, None)):
            r = runway.verify_token(GOOD_TOKEN)
        assert r["ok"] is True
        assert r["status"] == "valid"
        assert r["organization_name"] == "Acme Studio"
        assert r["organization_tier"] == "pro"

    def test_revoked_on_401(self):
        with patch.object(runway, "_runway_get_json", return_value=(401, None, None)):
            r = runway.verify_token(GOOD_TOKEN)
        assert r["status"] == "revoked"
        assert r["error"] == "unauthorized"

    def test_invalid_format_short_circuits_http(self):
        """Format check must run before _runway_get_json so we don't
        waste a network round-trip on obviously bad input."""
        with patch.object(runway, "_runway_get_json") as m:
            r = runway.verify_token("not-a-token")
            assert r["status"] == "invalid"
            m.assert_not_called()

    def test_unknown_on_connection_failed(self):
        with patch.object(runway, "_runway_get_json",
                          return_value=(0, None, "connection_failed")):
            r = runway.verify_token(GOOD_TOKEN)
        assert r["status"] == "unknown"
        assert r["error"] == "connection_failed"

    def test_unknown_on_unexpected_http_status(self):
        """Anything other than 200/401 falls into unknown — could be a Runway
        outage or our pinned X-Runway-Version going stale; either way it's
        not the user's token at fault."""
        with patch.object(runway, "_runway_get_json", return_value=(503, None, None)):
            r = runway.verify_token(GOOD_TOKEN)
        assert r["status"] == "unknown"


# ── HTTP layer: header contract ───────────────────────────────────────────────

class TestHttpHeaderShape:
    """Runway requires X-Runway-Version on every call. A missing header
    produces a 400 with an actionable error; we pin RUNWAY_API_VERSION so
    behaviour doesn't drift. This test catches a refactor that forgets the
    header (which would silently break verification for every user)."""

    def test_request_includes_bearer_and_version_headers(self):
        captured = {}

        class FakeResp:
            status = 200
            def read(self): return b'{"name":"X","tier":"pro"}'
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            runway._runway_get_json(
                f"{runway.RUNWAY_API_BASE}/organization", "key_abc",
            )

        # urllib normalises headers via .capitalize() (whole-string), so
        # "X-Runway-Version" reads back as "X-runway-version". Look up
        # case-insensitively so this test pins intent rather than urllib
        # quirks.
        lower_headers = {k.lower(): v for k, v in captured["headers"].items()}
        assert lower_headers.get("authorization") == "Bearer key_abc"
        # The pin: any refactor that drops X-Runway-Version trips this.
        assert lower_headers.get("x-runway-version") == runway.RUNWAY_API_VERSION


# ── resolve_status ────────────────────────────────────────────────────────────

class TestResolveStatus:
    def test_missing_when_no_config(self):
        st = runway.resolve_status("admin_bot",
                                    read_cfg=lambda b: None,
                                    check_token=lambda t: {"ok": True})
        assert st.status == "missing"

    def test_valid_propagates_org_from_check(self):
        st = runway.resolve_status("admin_bot",
                                    read_cfg=lambda b: {"access_token": GOOD_TOKEN},
                                    check_token=lambda t: {
                                        "ok": True, "status": "valid",
                                        "organization_name": "Acme",
                                        "organization_tier": "pro",
                                    })
        assert st.status == "valid"
        assert st.organization_name == "Acme"
        assert st.organization_tier == "pro"

    def test_revoked_preserves_stored_org_for_ui(self):
        """Show 'Acme Studio rejected the key' rather than just 'rejected'
        on revoke — which side of the chain to fix is the load-bearing info."""
        st = runway.resolve_status("admin_bot",
                                    read_cfg=lambda b: {
                                        "access_token": GOOD_TOKEN,
                                        "organization_name": "Acme Studio",
                                    },
                                    check_token=lambda t: {
                                        "ok": False, "status": "revoked",
                                        "error": "unauthorized",
                                    })
        assert st.status == "revoked"
        assert st.organization_name == "Acme Studio"

    def test_invalid_when_stored_token_garbage(self):
        st = runway.resolve_status("admin_bot",
                                    read_cfg=lambda b: {"access_token": "junk"},
                                    check_token=lambda t: {"ok": True})
        assert st.status == "invalid"


# ── Install plan ──────────────────────────────────────────────────────────────

class TestInstallPlan:
    def test_valid_yields_no_steps(self):
        st = runway.InstallStatus(bot_id="admin_bot", status="valid")
        assert runway.build_install_plan(st) == []

    def test_missing_yields_set_config_then_confirm(self):
        st = runway.InstallStatus(bot_id="admin_bot", status="missing")
        steps = runway.build_install_plan(st)
        assert [s.id for s in steps] == ["set_config", "confirm"]
        names = [f["name"] for f in steps[0].fields]
        assert names == ["access_token"]
        assert steps[0].fields[0]["type"] == "password"
        # The help text must tell the user where to generate the key —
        # without that, the modal is mysterious for first-timers.
        assert "API Keys" in steps[0].fields[0]["help"]

    def test_set_config_endpoint_matches_route(self):
        st = runway.InstallStatus(bot_id="admin_bot", status="missing")
        steps = runway.build_install_plan(st)
        assert steps[0].endpoint.endswith("/runway/set-token")


# ── Flask route smoke tests ───────────────────────────────────────────────────


# Unit tests above (token format, verify_token, resolve_status,
# build_install_plan) stay because verify_token is still the gate
# before the bundled-plugin install writes anything. New tests for
# the bundled-plugin path (auth-profiles.json writer + openclaw.json
# wiring + status resolver + wrapper route) follow.


import json as _json
from unittest.mock import patch


# ── Auth-profile writer (file IO is mocked — no real sudo) ────────────────────


class TestRunwayAuthProfileShape:
    """write_runway_auth_profile builds the right JSON shape for OC's
    bundled @openclaw/runway-provider. Tests the in-memory mutation
    pattern; the sudo/chown choreography is exercised in route tests
    via the real Flask wiring."""

    def test_writes_runway_default_profile_with_correct_fields(self, tmp_path):
        # Stub read_auth_profiles to return an empty dict; capture what
        # write_auth_profiles is called with.
        captured = {}

        def _fake_read(_bot_id):
            return {}

        def _fake_write(_bot_id, data):
            captured["data"] = data
            return True, None

        with patch.object(runway, "read_auth_profiles", _fake_read), \
             patch.object(runway, "write_auth_profiles", _fake_write):
            ok, err = runway.write_runway_auth_profile(
                "team_bot_b", "key_test_secret_xyz",
            )

        assert ok, err
        profile = captured["data"]["profiles"]["runway:default"]
        # Exact shape TeamBotA's working setup uses
        assert profile["type"] == "api_key"
        assert profile["provider"] == "runway"
        assert profile["key"] == "key_test_secret_xyz"

    def test_preserves_other_providers_in_auth_profiles(self):
        """Other providers (google, xai, anthropic) must stay intact when
        we add or update runway:default. The auth-profiles.json file is
        shared across providers — clobbering would break LLM access."""
        existing = {
            "profiles": {
                "google:default": {"type": "oauth", "...": "..."},
                "anthropic:default": {"type": "api_key", "provider": "anthropic",
                                       "key": "sk-ant-..."},
            },
        }
        captured = {}

        def _fake_read(_bot_id):
            return existing

        def _fake_write(_bot_id, data):
            captured["data"] = data
            return True, None

        with patch.object(runway, "read_auth_profiles", _fake_read), \
             patch.object(runway, "write_auth_profiles", _fake_write):
            ok, err = runway.write_runway_auth_profile(
                "team_bot_b", "key_new",
            )

        assert ok, err
        profiles = captured["data"]["profiles"]
        assert "runway:default" in profiles
        # Other providers preserved verbatim
        assert profiles["google:default"] == {"type": "oauth", "...": "..."}
        assert profiles["anthropic:default"]["key"] == "sk-ant-..."

    def test_strips_whitespace_from_key(self):
        captured = {}

        def _fake_read(_bot_id):
            return {}

        def _fake_write(_bot_id, data):
            captured["data"] = data
            return True, None

        with patch.object(runway, "read_auth_profiles", _fake_read), \
             patch.object(runway, "write_auth_profiles", _fake_write):
            runway.write_runway_auth_profile(
                "team_bot_b", "   key_padded   ",
            )

        assert captured["data"]["profiles"]["runway:default"]["key"] == "key_padded"


class TestDeleteRunwayAuthProfile:
    def test_removes_only_runway_default_profile(self):
        existing = {
            "profiles": {
                "runway:default": {"type": "api_key", "provider": "runway", "key": "x"},
                "google:default": {"type": "oauth"},
            },
        }
        captured = {}

        def _fake_read(_bot_id):
            return existing

        def _fake_write(_bot_id, data):
            captured["data"] = data
            return True, None

        with patch.object(runway, "read_auth_profiles", _fake_read), \
             patch.object(runway, "write_auth_profiles", _fake_write):
            ok, err = runway.delete_runway_auth_profile("team_bot_b")

        assert ok, err
        profiles = captured["data"]["profiles"]
        assert "runway:default" not in profiles
        assert "google:default" in profiles  # untouched

    def test_idempotent_when_profile_absent(self):
        """No write triggered when there's nothing to remove."""
        captured = {"written": False}

        def _fake_read(_bot_id):
            return {"profiles": {}}

        def _fake_write(_bot_id, data):
            captured["written"] = True
            return True, None

        with patch.object(runway, "read_auth_profiles", _fake_read), \
             patch.object(runway, "write_auth_profiles", _fake_write):
            ok, err = runway.delete_runway_auth_profile("team_bot_b")

        assert ok
        assert captured["written"] is False  # no-op


# ── openclaw.json model-default wiring ────────────────────────────────────────


class TestEnableRunwayInOcConfig:
    """enable_runway_in_oc_config sets agents.defaults.videoGenerationModel.primary
    — mirrors telegram_install.enable_channel_in_oc_config's read-merge-
    write shape via _oc_install_common."""

    def test_sets_model_primary_in_empty_oc_config(self):
        captured = {}

        def _fake_read(_bot_id):
            return {}, None

        def _fake_write(_bot_id, cfg):
            captured["cfg"] = cfg
            return True, None

        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common, "write_oc_config", _fake_write):
            ok, err = runway.enable_runway_in_oc_config("team_bot_b")

        assert ok, err
        primary = captured["cfg"]["agents"]["defaults"]["videoGenerationModel"]["primary"]
        assert primary == "runway/gen4.5"

    def test_preserves_other_agents_defaults(self):
        """Other fields under agents.defaults (e.g. messaging-related
        defaults) must not be clobbered."""
        existing = {
            "agents": {
                "defaults": {
                    "model": {"primary": "claude-4.5-sonnet"},
                    "memoryPolicy": {"mode": "rolling"},
                },
            },
        }
        captured = {}

        def _fake_read(_bot_id):
            return existing, None

        def _fake_write(_bot_id, cfg):
            captured["cfg"] = cfg
            return True, None

        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common, "write_oc_config", _fake_write):
            ok, err = runway.enable_runway_in_oc_config("team_bot_b")

        assert ok, err
        defaults = captured["cfg"]["agents"]["defaults"]
        # New videoGenerationModel added
        assert defaults["videoGenerationModel"]["primary"] == "runway/gen4.5"
        # Existing defaults untouched
        assert defaults["model"]["primary"] == "claude-4.5-sonnet"
        assert defaults["memoryPolicy"]["mode"] == "rolling"

    def test_custom_model_arg_overrides_default(self):
        """A future caller could pick gen4_turbo etc. as the default."""
        captured = {}

        def _fake_read(_bot_id):
            return {}, None

        def _fake_write(_bot_id, cfg):
            captured["cfg"] = cfg
            return True, None

        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common, "write_oc_config", _fake_write):
            runway.enable_runway_in_oc_config(
                "team_bot_b", model="runway/gen4_aleph",
            )

        assert captured["cfg"]["agents"]["defaults"]["videoGenerationModel"]["primary"] \
            == "runway/gen4_aleph"


class TestDisableRunwayInOcConfig:
    def test_removes_only_videoGenerationModel_primary(self):
        existing = {
            "agents": {
                "defaults": {
                    "model": {"primary": "claude-4.5-sonnet"},
                    "videoGenerationModel": {"primary": "runway/gen4.5"},
                    "memoryPolicy": {"mode": "rolling"},
                },
            },
        }
        captured = {}

        def _fake_read(_bot_id):
            return existing, None

        def _fake_write(_bot_id, cfg):
            captured["cfg"] = cfg
            return True, None

        from evolve_admin.skills import _oc_install_common as _oc_common
        with patch.object(_oc_common, "read_oc_config", _fake_read), \
             patch.object(_oc_common, "write_oc_config", _fake_write):
            ok, err = runway.disable_runway_in_oc_config("team_bot_b")

        assert ok, err
        defaults = captured["cfg"]["agents"]["defaults"]
        # videoGenerationModel removed entirely (was just our one key)
        assert "videoGenerationModel" not in defaults
        # Other defaults preserved
        assert defaults["model"]["primary"] == "claude-4.5-sonnet"
        assert defaults["memoryPolicy"]["mode"] == "rolling"


# ── Status resolver (bundled-plugin pattern) ──────────────────────────────────


class TestResolveStatusBundled:
    """Bundled-plugin status checks BOTH signals: openclaw.json
    videoGenerationModel.primary AND auth-profiles.json runway:default.
    Distinct status codes for partial states so the UI can prompt
    the right next step."""

    def test_valid_when_both_signals_present(self):
        def _read_oc(_bot_id):
            return {
                "agents": {"defaults": {"videoGenerationModel": {"primary": "runway/gen4.5"}}},
            }, None

        def _read_auth(_bot_id):
            return {"profiles": {"runway:default": {"key": "key_xyz"}}}

        status = runway.resolve_status_bundled(
            "team_bot_b", read_oc_config=_read_oc, read_auth_profiles_fn=_read_auth,
        )
        assert status.status == "valid"

    def test_missing_when_neither_signal_present(self):
        status = runway.resolve_status_bundled(
            "team_bot_b",
            read_oc_config=lambda _b: ({}, None),
            read_auth_profiles_fn=lambda _b: {},
        )
        assert status.status == "missing"

    def test_revoked_when_model_set_but_auth_profile_missing(self):
        """Operator deleted the API key from auth-profiles.json but the
        openclaw.json model-default is still set. Bot's gateway would
        attempt to use Runway and fail. UI should prompt for re-paste."""
        def _read_oc(_bot_id):
            return {
                "agents": {"defaults": {"videoGenerationModel": {"primary": "runway/gen4.5"}}},
            }, None

        status = runway.resolve_status_bundled(
            "team_bot_b", read_oc_config=_read_oc,
            read_auth_profiles_fn=lambda _b: {},
        )
        assert status.status == "revoked"
        assert "auth_profile_missing" in (status.error or "")

    def test_invalid_when_auth_present_but_model_not_runway(self):
        """Operator pasted the API key but openclaw.json's model default
        is not runway/* — partial install. The bot would still default
        to whatever other provider is set and never use Runway."""
        def _read_oc(_bot_id):
            return {
                "agents": {"defaults": {"videoGenerationModel": {"primary": "google/veo3"}}},
            }, None

        status = runway.resolve_status_bundled(
            "team_bot_b", read_oc_config=_read_oc,
            read_auth_profiles_fn=lambda _b: {
                "profiles": {"runway:default": {"key": "x"}},
            },
        )
        assert status.status == "invalid"
        assert "model_default_not_runway" in (status.error or "")

    def test_unknown_when_oc_unreadable(self):
        status = runway.resolve_status_bundled(
            "team_bot_b",
            read_oc_config=lambda _b: (None, "permission_denied"),
            read_auth_profiles_fn=lambda _b: {},
        )
        assert status.status == "unknown"
        assert "permission_denied" in (status.error or "")

    def test_empty_key_is_treated_as_missing_auth(self):
        """An auth profile present but with an empty 'key' field should
        not count as a valid signal — same effect as the profile being
        absent."""
        def _read_oc(_bot_id):
            return {
                "agents": {"defaults": {"videoGenerationModel": {"primary": "runway/gen4.5"}}},
            }, None

        status = runway.resolve_status_bundled(
            "team_bot_b", read_oc_config=_read_oc,
            read_auth_profiles_fn=lambda _b: {
                "profiles": {"runway:default": {"key": ""}},
            },
        )
        assert status.status == "revoked"


# ── Access panel: cost callout ────────────────────────────────────────────────


class TestAccessPanel:
    def test_post_install_callout_mentions_cost(self):
        """Runway charges per-second of generated video — operators
        should see this upfront, not after their first surprise bill."""
        callout = runway.RUNWAY_ACCESS_PANEL.get("post_install_callout") or ""
        assert "cost" in callout.lower() or "charge" in callout.lower() \
            or "$" in callout


# ── Route integration: /api/skills/install/runway/set-token ───────────────────


import pytest as _pytest


@_pytest.fixture
def runway_route_app(tmp_path):
    """Flask app + stubs for the runway install route. Mirrors fixture
    shape from test_skills_notion_install.py."""
    from evolve_admin.web import server as srv

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {"team_bot_b": {"user": "personal_bot_user"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(_json.dumps(network))
    app = srv.create_app(network_path=network_path)
    app.config["TESTING"] = True

    return app


def _ok_verify(_token):
    return {
        "ok": True, "status": "valid",
        "organization_name": "Palace Games",
        "organization_tier": "pro",
        "error": None, "http_status": 200,
    }


class TestSetTokenRoute:
    def test_missing_bot_id_rejected(self, runway_route_app):
        r = runway_route_app.test_client().post(
            "/api/skills/install/runway/set-token",
            json={"access_token": "key_x"},
        )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_missing_token_rejected(self, runway_route_app):
        r = runway_route_app.test_client().post(
            "/api/skills/install/runway/set-token",
            json={"bot_id": "team_bot_b"},
        )
        assert r.status_code == 400
        assert "access_token" in r.get_json()["error"]

    def test_revoked_token_returns_401(self, runway_route_app):
        def _revoked(_token):
            return {
                "ok": False, "status": "revoked",
                "organization_name": None,
                "error": "unauthorized", "http_status": 401,
            }

        with patch.object(runway, "verify_token", _revoked):
            r = runway_route_app.test_client().post(
                "/api/skills/install/runway/set-token",
                json={"bot_id": "team_bot_b", "access_token": "key_revoked"},
            )
        assert r.status_code == 401

    def test_happy_path_writes_auth_profile_and_oc_config_and_kickstarts(
        self, runway_route_app,
    ):
        """Critical end-to-end: verify_token → auth-profiles.json write
        → openclaw.json model-default → gateway kickstart. Mocks the
        filesystem helpers so no real sudo runs."""
        auth_writes: list[tuple[str, str]] = []
        oc_writes: list[str] = []
        kickstart_calls: list[str] = []

        def _fake_write_auth(bot_id, api_key):
            auth_writes.append((bot_id, api_key))
            return True, None

        def _fake_enable_oc(bot_id, model="runway/gen4.5"):
            oc_writes.append(bot_id)
            return True, None

        def _fake_kickstart(bot_id):
            kickstart_calls.append(bot_id)
            return True, None

        with patch.object(runway, "verify_token", _ok_verify), \
             patch.object(runway, "write_runway_auth_profile", _fake_write_auth), \
             patch.object(runway, "enable_runway_in_oc_config", _fake_enable_oc), \
             patch("evolve_admin.skills._oc_install_common.kickstart_gateway", _fake_kickstart), \
             patch.object(
                 runway, "resolve_status_bundled",
                 return_value=runway.InstallStatus(
                     bot_id="team_bot_b", status="valid",
                 ),
             ):
            r = runway_route_app.test_client().post(
                "/api/skills/install/runway/set-token",
                json={"bot_id": "team_bot_b", "access_token": "key_real_secret"},
            )

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "valid"
        assert body.get("organization_name") == "Palace Games"
        assert body.get("gateway_kickstarted") is True

        # All three filesystem effects fired exactly once for the right bot
        assert auth_writes == [("team_bot_b", "key_real_secret")]
        assert oc_writes == ["team_bot_b"]
        assert kickstart_calls == ["team_bot_b"]

    def test_auth_profile_failure_does_not_touch_oc_config(self, runway_route_app):
        """If auth-profiles.json write fails, openclaw.json must NOT be
        modified — otherwise we'd ship a half-install (model set but no
        credential)."""
        oc_writes: list[str] = []

        def _fake_auth_fail(_bot_id, _key):
            return False, "fake write error"

        def _spy_oc(bot_id, **kw):
            oc_writes.append(bot_id)
            return True, None

        with patch.object(runway, "verify_token", _ok_verify), \
             patch.object(runway, "write_runway_auth_profile", _fake_auth_fail), \
             patch.object(runway, "enable_runway_in_oc_config", _spy_oc):
            r = runway_route_app.test_client().post(
                "/api/skills/install/runway/set-token",
                json={"bot_id": "team_bot_b", "access_token": "key_x"},
            )

        assert r.status_code == 500
        assert "auth_profile_write_failed" in r.get_json()["error"]
        # Critical: openclaw.json was NOT modified
        assert oc_writes == []

    def test_oc_config_failure_rolls_back_auth_profile(self, runway_route_app):
        """If auth-profiles.json wrote successfully but openclaw.json
        write fails, the auth profile must be deleted so we don't leave
        the credential dangling without the model-default that activates it."""
        auth_writes: list = []
        deletes: list = []

        def _spy_write(bot_id, key):
            auth_writes.append((bot_id, key))
            return True, None

        def _fake_oc_fail(_bot_id):
            return False, "fake oc write error"

        def _spy_delete(bot_id):
            deletes.append(bot_id)
            return True, None

        with patch.object(runway, "verify_token", _ok_verify), \
             patch.object(runway, "write_runway_auth_profile", _spy_write), \
             patch.object(runway, "enable_runway_in_oc_config", _fake_oc_fail), \
             patch.object(runway, "delete_runway_auth_profile", _spy_delete):
            r = runway_route_app.test_client().post(
                "/api/skills/install/runway/set-token",
                json={"bot_id": "team_bot_b", "access_token": "key_x"},
            )

        assert r.status_code == 500
        assert "oc_config_write_failed" in r.get_json()["error"]
        # Rollback happened
        assert deletes == ["team_bot_b"]
