"""tests/test_skills_google_workspace_token_shim.py — pure-function coverage
for the Google Workspace token-shape translator.

The shim's job is to translate Evolve's ``auth-profiles.json`` profile shape
into the ``google.oauth2.credentials.Credentials.to_json()`` shape that
``taylorwilsdon/google_workspace_mcp`` reads.

These tests cover the pure functions (no disk, no subprocess). The IO wrapper
``write_credentials_for_bot`` is covered by the install-module integration
test elsewhere.

References:
  * Module: ``evolve_admin.skills.google_workspace_token_shim``
  * Spec: ``docs/spec-google-workspace-suite-2026-06-04.md`` §8
  * Vetting: ``docs/vetting-workspace-mcp-2026-06-04.md`` §3
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


from evolve_admin.skills import google_workspace_token_shim as shim  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_profile():
    """A typical OAuth profile written by /api/admin/onboard/google/callback."""
    return {
        "provider": "google_workspace",
        "type": "oauth",
        "google_account": "sam@example.com",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar",
        ],
        "services": ["gmail", "calendar"],
        "access_token": "ya29.a0fakeAccessToken",
        "access_token_expires_at": 1717000000.0,
        "refresh_token": "1//0gfakeRefreshToken",
        "issued_at": 1716996400.0,
        "status": "active",
    }


@pytest.fixture
def oauth_client():
    return {
        "mode": "self_hosted",
        "client_id": "123456789-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-fakeSecret",
    }


# ── build_credentials_json — happy path + edge cases ─────────────────────────


class TestBuildCredentialsJson:
    """Pure-function contract for the format translator."""

    def test_happy_path_shape(self, fresh_profile, oauth_client):
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        # Required keys for the MCP server (vetting doc §3).
        for key in (
            "token",
            "refresh_token",
            "client_id",
            "client_secret",
            "token_uri",
            "scopes",
            "expiry",
            "id_token",
            "quota_project_id",
        ):
            assert key in out, f"missing required key {key!r}"

    def test_field_mapping(self, fresh_profile, oauth_client):
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        assert out["token"] == "ya29.a0fakeAccessToken"
        assert out["refresh_token"] == "1//0gfakeRefreshToken"
        assert out["client_id"] == "123456789-abc.apps.googleusercontent.com"
        assert out["client_secret"] == "GOCSPX-fakeSecret"
        assert out["token_uri"] == shim.GOOGLE_TOKEN_URI
        assert out["scopes"] == fresh_profile["scopes"]
        assert out["id_token"] is None
        assert out["quota_project_id"] is None

    def test_expiry_is_iso_utc(self, fresh_profile, oauth_client):
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        # 1717000000 → 2024-05-29T16:26:40Z (the value doesn't matter, the
        # shape does). Must end in Z and parse round-trip.
        assert out["expiry"] is not None
        assert out["expiry"].endswith("Z")
        from datetime import datetime
        # Should be parseable via fromisoformat (post-trim of Z).
        parsed = datetime.fromisoformat(out["expiry"].rstrip("Z").rstrip("0").rstrip("."))
        assert parsed.year >= 2024

    def test_quota_project_id_passthrough(self, fresh_profile, oauth_client):
        out = shim.build_credentials_json(
            fresh_profile, oauth_client, quota_project_id="my-gcp-project",
        )
        assert out["quota_project_id"] == "my-gcp-project"

    # Edge: missing refresh token (rare; happens on Testing-mode re-consent).
    def test_missing_refresh_token_writes_empty(self, fresh_profile, oauth_client):
        fresh_profile.pop("refresh_token")
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        assert out["refresh_token"] == ""
        # Other fields still populated — we want the file to exist so the
        # MCP server fails loudly on first refresh instead of silently.
        assert out["token"]
        assert out["client_id"]

    # Edge: missing access_token_expires_at.
    def test_missing_expiry_yields_null(self, fresh_profile, oauth_client):
        fresh_profile.pop("access_token_expires_at")
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        assert out["expiry"] is None

    # Edge: garbage expiry value.
    @pytest.mark.parametrize("bad", ["not-a-number", float("nan"), object()])
    def test_garbage_expiry_yields_null(self, fresh_profile, oauth_client, bad):
        fresh_profile["access_token_expires_at"] = bad
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        assert out["expiry"] is None

    # Edge: empty scopes.
    def test_empty_scopes_passes_through(self, fresh_profile, oauth_client):
        fresh_profile["scopes"] = []
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        assert out["scopes"] == []

    # Edge: None scopes (defensive — shouldn't happen but the writer once
    # got fed a None somewhere).
    def test_none_scopes_becomes_empty_list(self, fresh_profile, oauth_client):
        fresh_profile["scopes"] = None
        out = shim.build_credentials_json(fresh_profile, oauth_client)
        assert out["scopes"] == []

    # Edge: oauth_client with missing fields.
    def test_missing_client_fields_yield_empty_strings(self, fresh_profile):
        out = shim.build_credentials_json(fresh_profile, {})
        assert out["client_id"] == ""
        assert out["client_secret"] == ""


# ── credentials_filename_for ─────────────────────────────────────────────────


class TestCredentialsFilenameFor:
    def test_standard_email(self, fresh_profile):
        assert shim.credentials_filename_for(fresh_profile) == "sam@example.com.json"

    def test_workspace_email(self):
        assert shim.credentials_filename_for(
            {"google_account": "lex@example-corp.com"}
        ) == "lex@example-corp.com.json"

    def test_dotted_local_part(self):
        assert shim.credentials_filename_for(
            {"google_account": "first.last@example.com"}
        ) == "first.last@example.com.json"

    def test_plus_alias(self):
        assert shim.credentials_filename_for(
            {"google_account": "sam+bot@example.com"}
        ) == "sam+bot@example.com.json"

    def test_missing_account_falls_back(self):
        assert shim.credentials_filename_for({}) == shim.FALLBACK_CREDENTIALS_FILENAME

    def test_empty_account_falls_back(self):
        assert shim.credentials_filename_for({"google_account": ""}) == shim.FALLBACK_CREDENTIALS_FILENAME

    def test_whitespace_account_falls_back(self):
        assert shim.credentials_filename_for(
            {"google_account": "   "}
        ) == shim.FALLBACK_CREDENTIALS_FILENAME

    # Defensive: a malicious profile shouldn't path-escape.
    def test_path_traversal_is_scrubbed(self):
        out = shim.credentials_filename_for({"google_account": "../../etc/passwd"})
        assert "/" not in out
        assert ".." in out  # the literal characters survive; just no path sep
        assert out.endswith(".json")

    def test_shell_metacharacters_scrubbed(self):
        out = shim.credentials_filename_for(
            {"google_account": "a;rm -rf /;b@example.com"}
        )
        assert ";" not in out
        assert " " not in out
        assert out.endswith(".json")


# ── credentials_dir_for_bot / credentials_path_for_bot ───────────────────────


class TestCredentialsPaths:
    """Path resolution against bot_home(). Uses monkey-patched bot_home so we
    don't depend on pwd or a real macOS user being present in test env."""

    def test_dir_under_dot_openclaw(self, monkeypatch, tmp_path, fresh_profile):
        from evolve_admin import config as cfg
        # Pretend /tmp/fakebot is the bot's home.
        monkeypatch.setattr(cfg, "bot_home", lambda bot_id, network=None: tmp_path)
        out = shim.credentials_dir_for_bot("lex")
        assert out == tmp_path / shim.MCP_CREDENTIALS_SUBDIR
        # MCP_CREDENTIALS_SUBDIR is the env-var value we hand to the MCP
        # server; it lives under .openclaw/ (vetting doc decision §4).
        assert ".openclaw" in str(out)

    def test_path_combines_dir_and_filename(self, monkeypatch, tmp_path, fresh_profile):
        from evolve_admin import config as cfg
        monkeypatch.setattr(cfg, "bot_home", lambda bot_id, network=None: tmp_path)
        out = shim.credentials_path_for_bot("lex", fresh_profile)
        assert out.parent == tmp_path / shim.MCP_CREDENTIALS_SUBDIR
        assert out.name == "sam@example.com.json"


# ── Sanity: vetting doc contract still holds ─────────────────────────────────


class TestVettingContract:
    """Lock-in tests for the vetting doc decisions. If these fail, the vetting
    decision was changed — the spec doc + vetting doc must be updated in lockstep."""

    def test_token_uri_is_oauth2_googleapis(self):
        assert shim.GOOGLE_TOKEN_URI == "https://oauth2.googleapis.com/token"

    def test_credentials_subdir_is_under_openclaw(self):
        # Vetting doc §4: credentials live under .openclaw/ so the sudoers
        # grant pattern matches the other per-bot skill credentials.
        assert shim.MCP_CREDENTIALS_SUBDIR.startswith(".openclaw")

    def test_credentials_subdir_matches_install_module_expectation(self):
        # The install module passes this as WORKSPACE_MCP_CREDENTIALS_DIR;
        # the value must be a relative subdir under bot_home.
        assert not shim.MCP_CREDENTIALS_SUBDIR.startswith("/")
