"""Zoom Server-to-Server OAuth — account-level token minting for write tools.

S2S OAuth tokens have ~1h validity and are minted on demand using the
``account_credentials`` grant. There's no refresh token — re-mint each
time. We cache the access token in-process to avoid one network round-trip
per tool call.

See:
    https://developers.zoom.us/docs/internal-apps/

A ``ZoomS2sSession`` is intentionally small and stateful — instantiate once
per shim run and call ``get_access_token()`` on every write tool invocation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


ZOOM_S2S_TOKEN_URL = "https://zoom.us/oauth/token"


@dataclass
class S2sConfig:
    """Static configuration for the S2S flow."""

    client_id: str
    client_secret: str
    account_id: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Optional["S2sConfig"]:
        """Build from env. Returns None if S2S isn't configured.

        Unlike user-OAuth, S2S is optional — operators who only want read
        tools don't have to set it up. Missing env → write tools simply
        don't register on the MCP server.
        """
        cid = env.get("ZOOM_S2S_CLIENT_ID")
        sec = env.get("ZOOM_S2S_CLIENT_SECRET")
        acc = env.get("ZOOM_S2S_ACCOUNT_ID")
        if not (cid and sec and acc):
            return None
        return cls(client_id=cid, client_secret=sec, account_id=acc)


class S2sError(Exception):
    """Raised when Zoom returns an S2S OAuth error response."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class ZoomS2sSession:
    """Cached S2S access-token holder, thread-safe via a single lock.

    Tokens are minted on demand and reused until they're within
    ``refresh_slack_seconds`` of expiry. The cache lives in-process; restart
    forces a fresh mint, no on-disk persistence (matches the S2S model).
    """

    def __init__(
        self,
        config: S2sConfig,
        *,
        client: Optional[httpx.Client] = None,
        refresh_slack_seconds: int = 60,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=15.0)
        self._owned_client = client is None
        self._refresh_slack_seconds = refresh_slack_seconds
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a fresh S2S access token, minting if needed.

        Thread-safe: concurrent callers serialize on the internal lock; only
        one mint happens even under contention.
        """
        with self._lock:
            if not force_refresh and self._is_fresh():
                assert self._access_token is not None
                return self._access_token
            self._mint_locked()
            assert self._access_token is not None
            return self._access_token

    def invalidate(self) -> None:
        """Drop the cached token. Next ``get_access_token()`` will mint fresh.

        Call this after a 401 from Zoom so we don't keep retrying with a
        token Zoom already rejected.
        """
        with self._lock:
            self._access_token = None
            self._expires_at = None

    def _is_fresh(self) -> bool:
        if self._access_token is None or self._expires_at is None:
            return False
        slack = timedelta(seconds=self._refresh_slack_seconds)
        return datetime.now(timezone.utc) + slack < self._expires_at

    def _mint_locked(self) -> None:
        resp = self._client.post(
            ZOOM_S2S_TOKEN_URL,
            auth=(self._config.client_id, self._config.client_secret),
            data={
                "grant_type": "account_credentials",
                "account_id": self._config.account_id,
            },
        )
        try:
            body = resp.json()
        except ValueError:
            raise S2sError(
                f"non-JSON response from Zoom S2S token endpoint (HTTP {resp.status_code})",
                code="invalid_response",
            )
        if resp.status_code >= 400 or "error" in body:
            raise S2sError(
                body.get("reason") or body.get("error") or f"HTTP {resp.status_code}",
                code=body.get("error") or f"http_{resp.status_code}",
            )
        if "access_token" not in body:
            raise S2sError(
                "Zoom S2S token response missing access_token",
                code="malformed_response",
            )
        self._access_token = body["access_token"]
        expires_in = int(body.get("expires_in") or 3600)
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
