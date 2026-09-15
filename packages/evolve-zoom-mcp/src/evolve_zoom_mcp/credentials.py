"""Persistent credential storage for the shim.

The shim writes its OAuth state to ``$ZOOM_CREDENTIALS_DIR/credentials.json``
with mode 600. The file holds:

    {
      "refresh_token": "<long-lived>",
      "access_token": "<short-lived, optional>",
      "access_token_expires_at": "<ISO 8601 UTC, optional>",
      "user_email": "<authorized Zoom user, optional, for display>",
      "scopes": ["meeting:read:meeting", ...]
    }

The access_token + expires_at are caches — if absent or stale, the shim
mints a fresh access token from the refresh_token. Only the refresh token
is load-bearing for resuming work after restart.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CREDENTIALS_FILENAME = "credentials.json"


@dataclass
class Credentials:
    """OAuth credential set persisted between shim invocations."""

    refresh_token: str
    access_token: Optional[str] = None
    access_token_expires_at: Optional[str] = None  # ISO 8601 UTC string
    user_email: Optional[str] = None
    scopes: list[str] = field(default_factory=list)

    def is_access_token_fresh(self, slack_seconds: int = 60) -> bool:
        """Return True if a cached access token is present and not near expiry.

        ``slack_seconds`` defines how much margin to require — we refresh
        early rather than risk a 401 mid-call.
        """
        if not self.access_token or not self.access_token_expires_at:
            return False
        try:
            expires = datetime.fromisoformat(
                self.access_token_expires_at.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        now = datetime.now(timezone.utc)
        return (expires - now).total_seconds() > slack_seconds

    def with_fresh_access_token(
        self, access_token: str, expires_in_seconds: int
    ) -> "Credentials":
        """Return a new Credentials with the access-token cache updated."""
        from datetime import timedelta

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
        ).isoformat()
        return Credentials(
            refresh_token=self.refresh_token,
            access_token=access_token,
            access_token_expires_at=expires_at,
            user_email=self.user_email,
            scopes=list(self.scopes),
        )


def credentials_path(credentials_dir: str | Path) -> Path:
    """Return the absolute path to credentials.json under ``credentials_dir``."""
    return Path(credentials_dir) / CREDENTIALS_FILENAME


def load_credentials(credentials_dir: str | Path) -> Optional[Credentials]:
    """Read credentials.json, returning None if it doesn't exist.

    Raises if the file exists but is unreadable or malformed — the operator
    should know rather than silently re-prompt for OAuth.
    """
    path = credentials_path(credentials_dir)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Credentials(
        refresh_token=raw["refresh_token"],
        access_token=raw.get("access_token"),
        access_token_expires_at=raw.get("access_token_expires_at"),
        user_email=raw.get("user_email"),
        scopes=list(raw.get("scopes") or []),
    )


def save_credentials(credentials_dir: str | Path, creds: Credentials) -> Path:
    """Write credentials.json atomically with mode 600.

    Uses temp-file + rename so a crash mid-write doesn't corrupt the file.
    Creates ``credentials_dir`` if it doesn't exist.
    """
    dir_path = Path(credentials_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    target = credentials_path(dir_path)
    tmp = target.with_suffix(".json.tmp")
    payload = json.dumps(asdict(creds), indent=2, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return target


def delete_credentials(credentials_dir: str | Path) -> bool:
    """Delete credentials.json. Returns True if it existed and was removed."""
    path = credentials_path(credentials_dir)
    if not path.exists():
        return False
    path.unlink()
    return True
