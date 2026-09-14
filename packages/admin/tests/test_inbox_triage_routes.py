"""Tests for the triage queue API surface (Phase 4b of Issue Inbox).

Endpoints under test:
  GET /api/inbox          (extended) — must EXCLUDE inbound intakes
  GET /api/inbox/triage   (new)      — returns inbound intakes only
"""

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
def app(tmp_path):
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes
    network = {"sharedDir": str(tmp_path), "primary": "evo"}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    app.config["_SHARED_DIR"] = tmp_path
    return app


def _seed_outbound(shared_dir, *, id_):
    """Operator-filed intake — should appear on /api/inbox, NOT on /api/inbox/triage."""
    from evolve_admin.intake.envelope import Intake
    from evolve_admin.intake import store
    ix = Intake(id=id_, kind="bug", body="outbound body")
    ix.state = "filed"
    ix.promotion.github_issue_url = "https://github.com/x/y/issues/1"
    ix.promotion.github_issue_number = 1
    store.write_intake(ix, shared_dir)


def _seed_inbound(shared_dir, *, id_, urgency="p2", category="bug",
                  number=42, author="user1", title="X broken"):
    """Inbound intake captured by the watcher — should appear on
    /api/inbox/triage, NOT on /api/inbox."""
    from evolve_admin.intake.envelope import Intake, TriageRecord
    from evolve_admin.intake import store
    ix = Intake(id=id_, kind="bug", body=f"{title}\n\nIssue body")
    ix.state = "filed"
    ix.inbound = True
    ix.promotion.github_issue_url = f"https://github.com/evolve-ops/evolve/issues/{number}"
    ix.promotion.github_issue_number = number
    ix.triage = TriageRecord(
        category=category, urgency=urgency,
        recommendation="route_to_admin",
        confidence=0.85,
        inbound_author=author, inbound_title=title,
        inbound_body_short="Issue body",
    )
    store.write_intake(ix, shared_dir)


# ─── /api/inbox now excludes inbound ──────────────────────────────────────


def test_outbound_inbox_excludes_inbound_items(app):
    shared = app.config["_SHARED_DIR"]
    _seed_outbound(shared, id_="ob-1")
    _seed_inbound(shared, id_="in-1")
    client = app.test_client()
    body = client.get("/api/inbox").get_json()
    ids = [r["intake"]["id"] for r in body["items"]]
    # Outbound shows up; inbound does not.
    assert "ob-1" in ids
    assert "in-1" not in ids


# ─── /api/inbox/triage happy path ─────────────────────────────────────────


def test_triage_list_returns_inbound_with_verdict(app):
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1", urgency="p1",
                  category="bug", author="alice", title="X is broken")
    client = app.test_client()
    r = client.get("/api/inbox/triage")
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1
    row = body["items"][0]
    assert row["intake"]["id"] == "in-1"
    assert row["intake"]["inbound"] is True
    assert row["intake"]["triage"]["urgency"] == "p1"
    assert row["intake"]["triage"]["category"] == "bug"
    assert row["intake"]["triage"]["inbound_author"] == "alice"


def test_triage_list_empty_when_no_inbound(app):
    shared = app.config["_SHARED_DIR"]
    _seed_outbound(shared, id_="ob-1")  # outbound only
    body = app.test_client().get("/api/inbox/triage").get_json()
    assert body["count"] == 0
    assert body["items"] == []


def test_triage_list_sorts_by_triaged_at_desc(app):
    """Most-recently-triaged items rank to the top so the operator's
    most-pressing fresh work is visible first."""
    from evolve_admin.intake.envelope import Intake, TriageRecord
    from evolve_admin.intake import store
    shared = app.config["_SHARED_DIR"]
    for i, (sort_ts, id_) in enumerate([
        ("2026-05-22T10:00:00Z", "older"),
        ("2026-05-22T12:00:00Z", "newest"),
        ("2026-05-22T11:00:00Z", "middle"),
    ]):
        ix = Intake(id=id_, kind="bug", body=f"body for {id_}")
        ix.state = "filed"
        ix.inbound = True
        ix.promotion.github_issue_url = f"https://github.com/x/y/issues/{i + 1}"
        ix.promotion.github_issue_number = i + 1
        ix.triage = TriageRecord(
            category="bug", urgency="p2",
            confidence=0.8, triaged_at=sort_ts,
        )
        store.write_intake(ix, shared)
    body = app.test_client().get("/api/inbox/triage").get_json()
    ids = [r["intake"]["id"] for r in body["items"]]
    assert ids == ["newest", "middle", "older"]


# ─── Filters ──────────────────────────────────────────────────────────────


def test_triage_filter_by_urgency_single(app):
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="p0-1", urgency="p0", number=1)
    _seed_inbound(shared, id_="p2-1", urgency="p2", number=2)
    body = app.test_client().get("/api/inbox/triage?urgency=p0").get_json()
    ids = [r["intake"]["id"] for r in body["items"]]
    assert ids == ["p0-1"]


def test_triage_filter_by_urgency_multiple(app):
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="p0-1", urgency="p0", number=1)
    _seed_inbound(shared, id_="p1-1", urgency="p1", number=2)
    _seed_inbound(shared, id_="p2-1", urgency="p2", number=3)
    body = app.test_client().get("/api/inbox/triage?urgency=p0,p1").get_json()
    ids = sorted([r["intake"]["id"] for r in body["items"]])
    assert ids == ["p0-1", "p1-1"]


def test_triage_filter_by_category(app):
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="bug-1", category="bug", number=1)
    _seed_inbound(shared, id_="dup-1", category="duplicate", number=2)
    body = app.test_client().get("/api/inbox/triage?category=duplicate").get_json()
    ids = [r["intake"]["id"] for r in body["items"]]
    assert ids == ["dup-1"]


def test_triage_limit_clamps(app):
    shared = app.config["_SHARED_DIR"]
    for i in range(5):
        _seed_inbound(shared, id_=f"in-{i}", number=i + 1)
    body = app.test_client().get("/api/inbox/triage?limit=2").get_json()
    assert body["count"] == 2


def test_triage_invalid_limit_falls_back_to_default(app):
    shared = app.config["_SHARED_DIR"]
    for i in range(3):
        _seed_inbound(shared, id_=f"in-{i}", number=i + 1)
    body = app.test_client().get("/api/inbox/triage?limit=garbage").get_json()
    assert body["count"] == 3
