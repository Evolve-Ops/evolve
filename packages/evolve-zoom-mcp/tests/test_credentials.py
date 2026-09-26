"""Tests for credentials.py."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evolve_zoom_mcp.credentials import (
    Credentials,
    credentials_path,
    delete_credentials,
    load_credentials,
    save_credentials,
)


class TestCredentialsRoundtrip:
    def test_save_then_load_preserves_fields(self, tmp_path: Path) -> None:
        creds = Credentials(
            refresh_token="rt_x",
            access_token="at_x",
            access_token_expires_at="2026-06-06T20:00:00+00:00",
            user_email="x@example.test",
            scopes=["meeting:read:meeting", "user:read:user"],
        )
        save_credentials(tmp_path, creds)
        loaded = load_credentials(tmp_path)
        assert loaded is not None
        assert loaded == creds

    def test_load_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert load_credentials(tmp_path) is None

    def test_save_writes_file_with_mode_600(self, tmp_path: Path) -> None:
        creds = Credentials(refresh_token="rt_y")
        path = save_credentials(tmp_path, creds)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_save_is_atomic_via_temp_rename(self, tmp_path: Path) -> None:
        # Pre-existing file should not be partially overwritten on failure;
        # we just confirm the .tmp file doesn't linger after success.
        save_credentials(tmp_path, Credentials(refresh_token="rt_z"))
        tmp = credentials_path(tmp_path).with_suffix(".json.tmp")
        assert not tmp.exists()

    def test_load_raises_on_malformed_json(self, tmp_path: Path) -> None:
        (tmp_path / "credentials.json").write_text("not json")
        try:
            load_credentials(tmp_path)
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError("expected JSONDecodeError")


class TestFreshness:
    def test_no_access_token_means_not_fresh(self) -> None:
        creds = Credentials(refresh_token="rt")
        assert creds.is_access_token_fresh() is False

    def test_expired_means_not_fresh(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        creds = Credentials(
            refresh_token="rt", access_token="at", access_token_expires_at=past
        )
        assert creds.is_access_token_fresh() is False

    def test_within_slack_means_not_fresh(self) -> None:
        # 30s into the future, default slack is 60s → still considered stale.
        near = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        creds = Credentials(
            refresh_token="rt", access_token="at", access_token_expires_at=near
        )
        assert creds.is_access_token_fresh() is False

    def test_well_in_future_means_fresh(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        creds = Credentials(
            refresh_token="rt", access_token="at", access_token_expires_at=future
        )
        assert creds.is_access_token_fresh() is True

    def test_malformed_expiry_means_not_fresh(self) -> None:
        creds = Credentials(
            refresh_token="rt", access_token="at", access_token_expires_at="not a date"
        )
        assert creds.is_access_token_fresh() is False


class TestWithFreshAccessToken:
    def test_returns_new_object_with_updated_fields(self) -> None:
        original = Credentials(refresh_token="rt", user_email="x@example.test")
        updated = original.with_fresh_access_token("at_new", 3600)
        # Original unchanged
        assert original.access_token is None
        # New has access token + expires_at
        assert updated.access_token == "at_new"
        assert updated.access_token_expires_at is not None
        # Refresh token + user email carry over
        assert updated.refresh_token == "rt"
        assert updated.user_email == "x@example.test"


class TestDelete:
    def test_delete_returns_true_when_file_existed(self, tmp_path: Path) -> None:
        save_credentials(tmp_path, Credentials(refresh_token="rt"))
        assert delete_credentials(tmp_path) is True
        assert not credentials_path(tmp_path).exists()

    def test_delete_returns_false_when_file_absent(self, tmp_path: Path) -> None:
        assert delete_credentials(tmp_path) is False
