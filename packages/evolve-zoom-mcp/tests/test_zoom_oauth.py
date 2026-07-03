"""Tests for zoom_oauth.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from evolve_zoom_mcp.credentials import Credentials, load_credentials
from evolve_zoom_mcp.zoom_oauth import (
    OAuthConfig,
    OAuthError,
    ZOOM_AUTHORIZE_URL,
    ZOOM_TOKEN_URL,
    ZOOM_USER_ME_URL,
    build_authorize_url,
    exchange_code,
    get_access_token,
    refresh_access_token,
)


# ---------- build_authorize_url ----------


class TestBuildAuthorizeUrl:
    def test_url_anchored_at_zoom(self, oauth_config: OAuthConfig) -> None:
        url = build_authorize_url(oauth_config)
        assert url.startswith(ZOOM_AUTHORIZE_URL + "?")

    def test_url_contains_required_params(self, oauth_config: OAuthConfig) -> None:
        url = build_authorize_url(oauth_config, scopes=("meeting:read:meeting",))
        qs = parse_qs(urlparse(url).query)
        assert qs["response_type"] == ["code"]
        assert qs["client_id"] == [oauth_config.client_id]
        assert qs["redirect_uri"] == [oauth_config.redirect_url]
        assert qs["scope"] == ["meeting:read:meeting"]

    def test_state_threaded_when_supplied(self, oauth_config: OAuthConfig) -> None:
        url = build_authorize_url(oauth_config, state="abc123")
        qs = parse_qs(urlparse(url).query)
        assert qs["state"] == ["abc123"]


# ---------- exchange_code ----------


def _mock_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


class TestExchangeCode:
    def test_persists_credentials_and_returns_them(
        self, oauth_config: OAuthConfig, credentials_dir: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == ZOOM_TOKEN_URL:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at_new",
                        "refresh_token": "rt_new",
                        "expires_in": 3600,
                        "scope": "meeting:read:meeting user:read:user",
                    },
                )
            if str(request.url) == ZOOM_USER_ME_URL:
                return httpx.Response(200, json={"email": "atlas-zoom@example.test"})
            return httpx.Response(404)

        client = httpx.Client(transport=_mock_transport(handler))
        creds = exchange_code(oauth_config, "code_abc", client=client)
        assert creds.refresh_token == "rt_new"
        assert creds.access_token == "at_new"
        assert creds.user_email == "atlas-zoom@example.test"
        assert "meeting:read:meeting" in creds.scopes
        # Persisted to disk too.
        loaded = load_credentials(credentials_dir)
        assert loaded is not None
        assert loaded.refresh_token == "rt_new"

    def test_zoom_error_raises_oauth_error(self, oauth_config: OAuthConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"reason": "Invalid client_id or client_secret", "error": "invalid_client"},
            )

        client = httpx.Client(transport=_mock_transport(handler))
        with pytest.raises(OAuthError) as exc_info:
            exchange_code(oauth_config, "code_abc", client=client)
        assert "Invalid client_id" in str(exc_info.value)
        assert exc_info.value.code == "invalid_client"

    def test_missing_access_token_raises(self, oauth_config: OAuthConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"refresh_token": "rt", "expires_in": 3600})

        client = httpx.Client(transport=_mock_transport(handler))
        with pytest.raises(OAuthError):
            exchange_code(oauth_config, "code_abc", client=client)

    def test_users_me_failure_doesnt_block_success(
        self, oauth_config: OAuthConfig, credentials_dir: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == ZOOM_TOKEN_URL:
                return httpx.Response(
                    200,
                    json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
                )
            return httpx.Response(500)  # /users/me fails — should be best-effort

        client = httpx.Client(transport=_mock_transport(handler))
        creds = exchange_code(oauth_config, "code_abc", client=client)
        assert creds.refresh_token == "rt"
        assert creds.user_email is None  # not set; flow still succeeded


# ---------- refresh_access_token ----------


class TestRefreshAccessToken:
    def test_rotates_refresh_token_when_zoom_returns_new_one(
        self, oauth_config: OAuthConfig
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "at_v2",
                    "refresh_token": "rt_v2",
                    "expires_in": 3600,
                },
            )

        client = httpx.Client(transport=_mock_transport(handler))
        old = Credentials(refresh_token="rt_v1", user_email="x@example.test")
        new = refresh_access_token(oauth_config, old, client=client)
        assert new.refresh_token == "rt_v2"
        assert new.access_token == "at_v2"
        assert new.user_email == "x@example.test"  # carried over

    def test_keeps_old_refresh_token_when_zoom_omits_one(
        self, oauth_config: OAuthConfig
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": "at_v2", "expires_in": 3600}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        old = Credentials(refresh_token="rt_v1")
        new = refresh_access_token(oauth_config, old, client=client)
        assert new.refresh_token == "rt_v1"

    def test_persists_to_disk(
        self, oauth_config: OAuthConfig, credentials_dir: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"access_token": "at", "refresh_token": "rt_new", "expires_in": 3600},
            )

        client = httpx.Client(transport=_mock_transport(handler))
        refresh_access_token(oauth_config, Credentials(refresh_token="rt_old"), client=client)
        loaded = load_credentials(credentials_dir)
        assert loaded is not None
        assert loaded.refresh_token == "rt_new"

    def test_revoked_refresh_token_raises(self, oauth_config: OAuthConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": "invalid_grant", "reason": "Invalid Token!"}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        with pytest.raises(OAuthError) as exc_info:
            refresh_access_token(
                oauth_config, Credentials(refresh_token="rt"), client=client
            )
        assert exc_info.value.code == "invalid_grant"


# ---------- get_access_token ----------


class TestGetAccessToken:
    def test_raises_when_credentials_absent(self, oauth_config: OAuthConfig) -> None:
        with pytest.raises(OAuthError) as exc_info:
            get_access_token(oauth_config)
        assert exc_info.value.code == "not_configured"

    def test_returns_cached_when_fresh(
        self, oauth_config: OAuthConfig, saved_creds: Credentials
    ) -> None:
        # No HTTP calls should happen — pass a transport that fails any request.
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected HTTP call: {request.url}")

        client = httpx.Client(transport=_mock_transport(handler))
        token = get_access_token(oauth_config, client=client)
        assert token == saved_creds.access_token

    def test_refreshes_when_stale(
        self, oauth_config: OAuthConfig, credentials_dir: Path
    ) -> None:
        # Pre-seed with an expired token.
        from evolve_zoom_mcp.credentials import save_credentials

        save_credentials(
            credentials_dir,
            Credentials(
                refresh_token="rt_stale",
                access_token="at_stale",
                access_token_expires_at="2020-01-01T00:00:00+00:00",
            ),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"access_token": "at_fresh", "refresh_token": "rt_fresh", "expires_in": 3600},
            )

        client = httpx.Client(transport=_mock_transport(handler))
        token = get_access_token(oauth_config, client=client)
        assert token == "at_fresh"
