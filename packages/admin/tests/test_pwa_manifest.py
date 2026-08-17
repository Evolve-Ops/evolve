"""Tests for the /manifest.json endpoint and its pod-name handling.

Covers the polish fix that drops the per-pod suffix when ``networkId``
is the wizard's default placeholder ("my-pod") or empty — without that
fix Chrome's standalone-window title-bar concatenates manifest name +
document.title and shows "Evolve · my-pod Evolve" on a fresh install.

Spec: docs/spec-pwa-2026-05-18.md §3.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


# ── _is_default_or_empty: unit tests ─────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", True),
        ("   ", True),
        (None, True),
        ("my-pod", True),
        ("MY-POD", True),
        ("My-Pod", True),
        ("  my-pod  ", True),
        ("evolve", True),
        ("Evolve", True),
        ("  EVOLVE ", True),
        ("team_bot_a-mini", False),
        ("Pod_admin's pod", False),
        ("home", False),
        ("my-pod-2", False),  # only the literal default, not a prefix match
        ("evolve-prod", False),
    ],
)
def test_is_default_or_empty_classifies(value, expected):
    from evolve_admin.web.server import _is_default_or_empty

    assert _is_default_or_empty(value) is expected


# ── /manifest.json: integration tests ────────────────────────────────────────


def _make_app(tmp_path: Path, network_id: str | None):
    """Build a Flask test client with ``networkId`` set on disk.

    Passing ``None`` writes a network.json without the key at all (older
    installs); the server should still drop the suffix because the value
    resolves to the empty string.
    """
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network: dict = {"members": ["bot"], "sharedDir": str(shared_dir)}
    if network_id is not None:
        network["networkId"] = network_id
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client()


def _fetch_manifest(client) -> dict:
    resp = client.get("/manifest.json")
    assert resp.status_code == 200
    assert resp.mimetype == "application/manifest+json"
    return json.loads(resp.get_data(as_text=True))


@pytest.mark.parametrize(
    "network_id",
    ["my-pod", "MY-POD", "  my-pod  ", "", "   ", None, "evolve"],
)
def test_manifest_drops_pod_suffix_for_default_or_empty(tmp_path, network_id):
    client = _make_app(tmp_path, network_id)
    body = _fetch_manifest(client)
    assert body["name"] == "Evolve", body
    assert body["short_name"] == "Evolve", body


@pytest.mark.parametrize(
    "network_id,expected_name,expected_short",
    [
        ("team_bot_a-mini", "Evolve · team_bot_a-mini", "team_bot_a-mini"),
        ("Pod_admin's pod", "Evolve · Pod_admin's pod", "Pod_admin's pod"),
        ("my-pod-2", "Evolve · my-pod-2", "my-pod-2"),
    ],
)
def test_manifest_keeps_pod_suffix_when_customized(
    tmp_path, network_id, expected_name, expected_short
):
    client = _make_app(tmp_path, network_id)
    body = _fetch_manifest(client)
    assert body["name"] == expected_name
    assert body["short_name"] == expected_short


def test_manifest_shape_contract_intact(tmp_path):
    """Regression guard: the PWA install-criteria fields must stay present."""
    client = _make_app(tmp_path, "team_bot_a-mini")
    body = _fetch_manifest(client)
    assert body["description"] == "Your Evolve pod"
    assert body["start_url"] == "/"
    assert body["scope"] == "/"
    assert body["display"] == "standalone"
    assert body["background_color"] == "#0d1117"
    assert body["theme_color"] == "#0d1117"

    icons = body["icons"]
    srcs = {icon["src"] for icon in icons}
    assert srcs == {
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-512-maskable.png",
    }
    maskable = [icon for icon in icons if icon.get("purpose") == "maskable"]
    assert len(maskable) == 1
    assert maskable[0]["sizes"] == "512x512"


def test_site_webmanifest_alias_returns_same_body(tmp_path):
    """Backwards-compat shim should hand back the same per-pod payload."""
    client = _make_app(tmp_path, "team_bot_a-mini")
    canonical = _fetch_manifest(client)
    alias_resp = client.get("/site.webmanifest")
    assert alias_resp.status_code == 200
    assert json.loads(alias_resp.get_data(as_text=True)) == canonical
