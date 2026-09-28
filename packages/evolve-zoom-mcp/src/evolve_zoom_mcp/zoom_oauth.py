"""Zoom user-OAuth (Marketplace General App) — code grant + refresh.

Zoom's user-OAuth flow is standard OAuth 2.0 with HTTP Basic auth on the
token endpoint (`client_secret_basic`). See:

    https://developers.zoom.us/docs/integrations/oauth/

This module wraps the three endpoints we need:

- ``build_authorize_url``   — construct the URL the operator opens in browser
- ``exchange_code``         — first-time code→{access,refresh}_token swap
- ``refresh_access_token``  — rotate access tokens using the refresh_token

Plus a small in-process helper that combines the cached-or-refresh path,
``get_access_token``, which the proxy client calls before every request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

from .credentials import Credentials, load_credentials, save_credentials


# Read scopes the shim requests when constructing an authorize URL. The
# operator confirms these on Zoom's consent screen. Pinned 2026-06-06 against
# Zoom's Marketplace scope picker; update via the drift CI script (§11.c).
ZOOM_USER_OAUTH_SCOPES = (
    "meeting:read:meeting",
    "meeting:read:list_meetings",
    "cloud_recording:read:list_user_recordings",
    "cloud_recording:read:list_recording_files",
    "cloud_recording:read:recording",
    "cloud_recording:read:meeting_transcript",
    "team_chat:read:list_user_messages",
    "user:read:user",
    "user:read:email",
)


ZOOM_AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_USER_ME_URL = "https://api.zoom.us/v2/users/me"


@dataclass
class OAuthConfig:
    """Static configuration for the user-OAuth flow."""

    client_id: str
    client_secret: str
    redirect_url: str
    credentials_dir: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "OAuthConfig":
        """Build from a dict-like env. Raises KeyError if any are missing."""
        return cls(
            client_id=env["ZOOM_OAUTH_CLIENT_ID"],
            client_secret=env["ZOOM_OAUTH_CLIENT_SECRET"],
            redirect_url=env["ZOOM_OAUTH_REDIRECT_URL"],
            credentials_dir=env["ZOOM_CREDENTIALS_DIR"],
        )


class OAuthError(Exception):
    """Raised when Zoom returns an OAuth error response."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


def build_authorize_url(
    config: OAuthConfig,
    scopes: tuple[str, ...] = ZOOM_USER_OAUTH_SCOPES,
    state: Optional[str] = None,
) -> str:
    """Construct the authorize URL the operator opens in their browser."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_url,
        "scope": " ".join(scopes),
    }
    if state:
        params["state"] = state
    return f"{ZOOM_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    config: OAuthConfig,
    code: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Credentials:
    """Swap an authorization code for {access,refresh}_token.

    Persists the result to ``credentials_dir/credentials.json`` and returns
    the populated Credentials. Also fetches the authorizing user's email
    to display in the access panel.
    """
    owned = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.post(
            ZOOM_TOKEN_URL,
            auth=(config.client_id, config.client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_url,
            },
        )
        body = _parse_oauth_response(resp)
        creds = Credentials(
            refresh_token=body["refresh_token"],
            access_token=body["access_token"],
            scopes=str(body.get("scope") or "").split(),
        )
        creds = creds.with_fresh_access_token(
            access_token=body["access_token"],
            expires_in_seconds=int(body.get("expires_in") or 3600),
        )
        # Best-effort: enrich with user email for display.
        try:
            me = client.get(
                ZOOM_USER_ME_URL,
                headers={"Authorization": f"Bearer {creds.access_token}"},
            )
            if me.status_code == 200:
                creds.user_email = me.json().get("email")
        except httpx.HTTPError:
            pass
        save_credentials(config.credentials_dir, creds)
        return creds
    finally:
        if owned:
            client.close()


def refresh_access_token(
    config: OAuthConfig,
    creds: Credentials,
    *,
    client: Optional[httpx.Client] = None,
) -> Credentials:
    """Mint a fresh access token using the stored refresh token.

    Zoom rotates refresh tokens on every refresh — the response includes a
    new refresh_token that supersedes the old one. We persist the new pair
    immediately so a crash mid-flight doesn't strand us.
    """
    owned = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.post(
            ZOOM_TOKEN_URL,
            auth=(config.client_id, config.client_secret),
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
            },
        )
        body = _parse_oauth_response(resp)
        updated = Credentials(
            refresh_token=body.get("refresh_token") or creds.refresh_token,
            user_email=creds.user_email,
            scopes=str(body.get("scope") or "").split() or list(creds.scopes),
        ).with_fresh_access_token(
            access_token=body["access_token"],
            expires_in_seconds=int(body.get("expires_in") or 3600),
        )
        save_credentials(config.credentials_dir, updated)
        return updated
    finally:
        if owned:
            client.close()


def get_access_token(
    config: OAuthConfig,
    *,
    client: Optional[httpx.Client] = None,
) -> str:
    """Return a fresh access token, refreshing if needed.

    Reads ``credentials.json`` from disk, returns the cached access token
    if it's still fresh, otherwise refreshes and persists the result.
    Raises ``OAuthError`` if there's no credentials.json (first-time login
    hasn't run) or if the refresh fails (refresh token revoked).
    """
    creds = load_credentials(config.credentials_dir)
    if creds is None:
        raise OAuthError(
            "no credentials — run `evolve-zoom-mcp login` first",
            code="not_configured",
        )
    if creds.is_access_token_fresh():
        assert creds.access_token is not None  # implied by is_access_token_fresh
        return creds.access_token
    refreshed = refresh_access_token(config, creds, client=client)
    assert refreshed.access_token is not None
    return refreshed.access_token


def _parse_oauth_response(resp: httpx.Response) -> dict:
    """Validate a Zoom OAuth response, raising OAuthError on non-2xx."""
    try:
        body = resp.json()
    except ValueError:
        raise OAuthError(
            f"non-JSON response from Zoom token endpoint (HTTP {resp.status_code})",
            code="invalid_response",
        )
    if resp.status_code >= 400 or "error" in body:
        raise OAuthError(
            body.get("reason") or body.get("error") or f"HTTP {resp.status_code}",
            code=body.get("error") or f"http_{resp.status_code}",
        )
    if "access_token" not in body:
        raise OAuthError(
            "Zoom token response missing access_token", code="malformed_response"
        )
    return body
