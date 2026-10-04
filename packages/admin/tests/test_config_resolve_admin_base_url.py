"""Tests for evolve_admin.config.resolve_admin_base_url."""

from __future__ import annotations

import os
import socket
from unittest.mock import patch

import pytest

from evolve_admin.config import resolve_admin_base_url


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Strip EVOLVE_ADMIN_BASE_URL so each test runs in a known state."""
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)


def test_env_var_wins_over_network_config(monkeypatch):
    monkeypatch.setenv("EVOLVE_ADMIN_BASE_URL", "http://override.example.com:5050")
    network = {"adminBaseUrl": "http://network-config.example.com:5050"}
    assert resolve_admin_base_url(network) == "http://override.example.com:5050"


def test_env_var_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("EVOLVE_ADMIN_BASE_URL", "http://x.example.com:5050/")
    assert resolve_admin_base_url(None) == "http://x.example.com:5050"


def test_env_var_blank_falls_through(monkeypatch):
    monkeypatch.setenv("EVOLVE_ADMIN_BASE_URL", "   ")
    network = {"adminBaseUrl": "http://from-network:5050"}
    assert resolve_admin_base_url(network) == "http://from-network:5050"


def test_network_config_used_when_no_env():
    network = {"adminBaseUrl": "https://pod.tail-scale.ts.net"}
    assert resolve_admin_base_url(network) == "https://pod.tail-scale.ts.net"


def test_network_config_strips_trailing_slash():
    network = {"adminBaseUrl": "http://pod.local:5050/"}
    assert resolve_admin_base_url(network) == "http://pod.local:5050"


def test_network_config_preserves_https_scheme():
    """Once §4.1 (HTTPS on LAN) lands, operators may configure https://.
    The helper must NOT downgrade to http://."""
    network = {"adminBaseUrl": "https://pod.local:5050"}
    assert resolve_admin_base_url(network) == "https://pod.local:5050"


def test_network_config_blank_falls_through_to_derived():
    network = {"adminBaseUrl": "   "}
    with patch("socket.gethostname", return_value="mini.local"):
        assert resolve_admin_base_url(network) == "http://mini:5050"


def test_network_config_missing_falls_through_to_derived():
    network = {}
    with patch("socket.gethostname", return_value="mini.local"):
        assert resolve_admin_base_url(network) == "http://mini:5050"


def test_network_config_wrong_type_falls_through():
    network = {"adminBaseUrl": 12345}
    with patch("socket.gethostname", return_value="mini.local"):
        assert resolve_admin_base_url(network) == "http://mini:5050"


def test_none_network_falls_through_to_derived():
    with patch("socket.gethostname", return_value="mini.local"):
        assert resolve_admin_base_url(None) == "http://mini:5050"


def test_derived_default_uses_short_hostname():
    """Match the wizard's `hostname.split('.')[0]` convention so the
    laptop-side SSH tunnel one-liner and the admin URL agree."""
    with patch("socket.gethostname", return_value="team_bot_a-mini.tail-scale.ts.net"):
        assert resolve_admin_base_url({}) == "http://team_bot_a-mini:5050"


def test_derived_default_falls_back_to_localhost_when_hostname_empty():
    with patch("socket.gethostname", return_value=""):
        assert resolve_admin_base_url({}) == "http://localhost:5050"


def test_derived_default_falls_back_to_localhost_on_oserror():
    with patch("socket.gethostname", side_effect=OSError("nope")):
        assert resolve_admin_base_url({}) == "http://localhost:5050"


def test_no_trailing_slash_in_any_path():
    cases = [
        ("http://x:5050", "http://x:5050"),
        ("http://x:5050/", "http://x:5050"),
        ("http://x:5050//", "http://x:5050"),
        ("https://pod.ts.net/", "https://pod.ts.net"),
    ]
    for input_url, expected in cases:
        assert resolve_admin_base_url({"adminBaseUrl": input_url}) == expected
