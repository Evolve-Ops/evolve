"""Shared pytest fixtures for evolve-zoom-mcp tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_zoom_mcp.credentials import Credentials, save_credentials
from evolve_zoom_mcp.zoom_oauth import OAuthConfig
from evolve_zoom_mcp.zoom_s2s import S2sConfig


@pytest.fixture
def credentials_dir(tmp_path: Path) -> Path:
    return tmp_path / "zoom"


@pytest.fixture
def oauth_config(credentials_dir: Path) -> OAuthConfig:
    return OAuthConfig(
        client_id="test_client_id",
        client_secret="test_client_secret",
        redirect_url="https://example.test/cb",
        credentials_dir=str(credentials_dir),
    )


@pytest.fixture
def s2s_config() -> S2sConfig:
    return S2sConfig(
        client_id="s2s_client_id",
        client_secret="s2s_client_secret",
        account_id="s2s_account_id",
    )


@pytest.fixture
def saved_creds(credentials_dir: Path) -> Credentials:
    creds = Credentials(
        refresh_token="rt_initial",
        access_token="at_initial",
        scopes=["meeting:read:meeting"],
        user_email="atlas-zoom@example.test",
    ).with_fresh_access_token(access_token="at_initial", expires_in_seconds=3600)
    save_credentials(credentials_dir, creds)
    return creds


def write_creds_file(credentials_dir: Path, payload: dict) -> Path:
    credentials_dir.mkdir(parents=True, exist_ok=True)
    p = credentials_dir / "credentials.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p
