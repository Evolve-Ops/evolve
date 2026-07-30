"""tests/test_peers_routes.py — Multi-pod hub switcher (M2).

Covers the two pieces that back the sidebar pod switcher:

  * ``evolve_admin.config.validate_peers`` / ``resolve_peers`` — the light
    ``network.json::peers`` validator (each entry is {name, adminBaseUrl}
    only, adminBaseUrl an http(s) URL; the hard v1 invariant is **no
    tokens / no extra keys**).
  * ``GET /api/peers`` (``peers_routes.register_peers_routes``) — the read
    route returning ``{current: {name, version}, peers: [...]}``.

Spec: docs/design-multi-pod-2026-06-11.md §3, §3.1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin.config import (  # noqa: E402
    _default_network,
    resolve_peers,
    validate_peers,
)
from evolve_admin.web.peers_routes import register_peers_routes  # noqa: E402


# ── validate_peers / resolve_peers unit tests ───────────────────────────────


def test_default_network_has_empty_peers():
    """A fresh pod is single-pod: peers defaults to []."""
    assert _default_network()["peers"] == []


def test_validate_peers_none_and_absent_are_single_pod():
    """None / absent ⇒ valid empty (single-pod), not an error."""
    assert validate_peers(None) == ([], [])
    assert resolve_peers({}) == []
    assert resolve_peers({"peers": []}) == []


def test_validate_peers_non_list_is_error():
    errors, cleaned = validate_peers({"name": "x"})  # dict, not a list
    assert cleaned == []
    assert any("must be a list" in e for e in errors)


def test_validate_peers_happy_path_strips_trailing_slash():
    errors, cleaned = validate_peers([
        {"name": "vps-hetzner", "adminBaseUrl": "https://vps.example.ts.net:5050/"},
    ])
    assert errors == []
    assert cleaned == [
        {"name": "vps-hetzner", "adminBaseUrl": "https://vps.example.ts.net:5050"},
    ]


def test_validate_peers_rejects_non_http_url():
    errors, cleaned = validate_peers([{"name": "x", "adminBaseUrl": "ftp://nope"}])
    assert cleaned == []
    assert any("http(s) URL" in e for e in errors)


def test_validate_peers_requires_name_and_url():
    errors, cleaned = validate_peers([
        {"adminBaseUrl": "http://h:5050"},          # missing name
        {"name": "y"},                               # missing adminBaseUrl
        {"name": "", "adminBaseUrl": "http://h:5050"},  # empty name
    ])
    assert cleaned == []
    assert len(errors) == 3


def test_validate_peers_rejects_token_smuggling():
    """The hard v1 invariant: entries are {name, adminBaseUrl} ONLY — any
    extra key (e.g. a smuggled token) is rejected, not silently carried."""
    errors, cleaned = validate_peers([{
        "name": "x", "adminBaseUrl": "http://h:5050", "token": "sekret",
    }])
    assert cleaned == []
    assert any("token" in e and "no tokens" in e for e in errors)


def test_resolve_peers_drops_malformed_keeps_valid():
    """resolve_peers degrades gracefully — bad rows dropped, good ones kept,
    so the switcher never blocks on one malformed entry."""
    cleaned = resolve_peers({"peers": [
        {"name": "ok", "adminBaseUrl": "http://h:5050"},
        {"name": "bad"},                              # dropped
        {"name": "ok2", "adminBaseUrl": "https://h2:5050"},
    ]})
    assert cleaned == [
        {"name": "ok", "adminBaseUrl": "http://h:5050"},
        {"name": "ok2", "adminBaseUrl": "https://h2:5050"},
    ]


# ── GET /api/peers route tests ──────────────────────────────────────────────


def _make_client(tmp_path: Path, network: dict) -> "Flask":
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    app = Flask(__name__)
    register_peers_routes(app, p)
    return app.test_client()


def test_api_peers_single_pod_shape(tmp_path: Path):
    """Single-pod: current.name = networkId, current.version present, peers []."""
    client = _make_client(tmp_path, {"networkId": "home-mini", "peers": []})
    r = client.get("/api/peers")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"current", "peers"}
    assert body["current"]["name"] == "home-mini"
    assert isinstance(body["current"]["version"], str) and body["current"]["version"]
    assert body["peers"] == []


def test_api_peers_multi_pod_returns_clean_links_only(tmp_path: Path):
    """Multi-pod: peers come back as {name, adminBaseUrl} only — never the
    extra keys an operator might paste in, and never a token."""
    client = _make_client(tmp_path, {
        "networkId": "home-mini",
        "peers": [
            {"name": "vps-hetzner", "adminBaseUrl": "https://vps.example.ts.net:5050/"},
            {"name": "bad-row"},  # malformed — must be dropped, not 500
        ],
    })
    r = client.get("/api/peers")
    assert r.status_code == 200
    body = r.get_json()
    assert body["current"]["name"] == "home-mini"
    assert body["peers"] == [
        {"name": "vps-hetzner", "adminBaseUrl": "https://vps.example.ts.net:5050"},
    ]
    # Belt-and-suspenders: nothing token-shaped ever leaves the route.
    for entry in body["peers"]:
        assert set(entry) == {"name", "adminBaseUrl"}


def test_api_peers_missing_networkid_degrades(tmp_path: Path):
    """A network.json with no networkId still yields a usable identity label."""
    client = _make_client(tmp_path, {"peers": []})
    body = client.get("/api/peers").get_json()
    assert body["current"]["name"]  # non-empty fallback, not a crash


def test_api_peers_missing_network_file_is_single_pod(tmp_path: Path):
    """An absent network.json degrades to single-pod, not a 500."""
    app = Flask(__name__)
    register_peers_routes(app, tmp_path / "does-not-exist.json")
    r = app.test_client().get("/api/peers")
    assert r.status_code == 200
    assert r.get_json()["peers"] == []
