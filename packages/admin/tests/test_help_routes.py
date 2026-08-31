"""tests/test_help_routes.py — Flask routes for /api/evo/help/*."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def help_app(tmp_path):
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes

    network = {"sharedDir": str(tmp_path)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    app.config["_SHARED_DIR"] = tmp_path
    return app


def _seed_and_build(shared_dir: Path) -> None:
    """Create a couple of docs and write the index file."""
    from evolve_admin.help_index import build

    docs_root = shared_dir / "src" / "docs"
    (docs_root / "help").mkdir(parents=True)
    (docs_root / "help" / "cost.md").write_text(
        "# Help: Cost Page\n\nSpend by model and channel.\n"
    )
    (docs_root / "help" / "security.md").write_text(
        "# Help: Security Page\n\nAudit findings and posture views.\n"
    )
    idx = build.build_index(docs_root)
    build.write_index(idx, shared_dir)


def test_search_503_when_no_index(help_app):
    client = help_app.test_client()
    r = client.post("/api/evo/help/search", json={"query": "cost"})
    assert r.status_code == 503
    assert "help-index build" in r.get_json()["error"]


def test_search_requires_query(help_app):
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/search", json={})
    assert r.status_code == 400
    r = client.post("/api/evo/help/search", json={"query": "   "})
    assert r.status_code == 400


def test_search_returns_hits(help_app):
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/search", json={"query": "cost"})
    assert r.status_code == 200
    hits = r.get_json()["hits"]
    assert any(h["doc_id"] == "help/cost" for h in hits)
    # Hit shape
    hit = hits[0]
    assert {"doc_id", "title", "snippet", "score", "path"} <= set(hit.keys())


def test_search_empty_when_no_match(help_app):
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/search", json={"query": "kubernetes"})
    assert r.status_code == 200
    assert r.get_json()["hits"] == []


def test_search_k_capped(help_app):
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/search", json={"query": "page", "k": 999})
    assert r.status_code == 200
    # k clamped to 10; with only 2 docs we get ≤2
    hits = r.get_json()["hits"]
    assert len(hits) <= 10


def test_read_503_when_no_index(help_app):
    client = help_app.test_client()
    r = client.post("/api/evo/help/read", json={"doc_id": "cost"})
    assert r.status_code == 503


def test_read_requires_doc_id(help_app):
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/read", json={})
    assert r.status_code == 400


def test_read_404_when_missing(help_app):
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/read", json={"doc_id": "nope"})
    assert r.status_code == 404


def test_read_404_when_id_unnamespaced(help_app):
    """`cost` (no category prefix) should 404 — bots should always
    pass the prefixed id returned by search."""
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/read", json={"doc_id": "cost"})
    assert r.status_code == 404


def test_read_returns_full_body(help_app):
    _seed_and_build(help_app.config["_SHARED_DIR"])
    client = help_app.test_client()
    r = client.post("/api/evo/help/read", json={"doc_id": "help/cost"})
    assert r.status_code == 200
    doc = r.get_json()["doc"]
    assert doc["doc_id"] == "help/cost"
    assert "Help: Cost Page" in doc["body"]
    assert "Spend by model" in doc["body"]
