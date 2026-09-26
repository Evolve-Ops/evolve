"""tests for POST /api/evo/intake/<id>/triage.

The route runs the triage classifier on demand against any intake
regardless of state or origin. Used by the Issues page "Triage now"
button so operators can get a verdict on their own filed issues
(which the inbound watcher skips because it filters to non-operator
authored issues only).
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
def intake_app(tmp_path):
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes

    network = {
        "members": ["team_bot_a", "evo"],
        "sharedDir": str(tmp_path),
        "primary": "evo",
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    app.config["_NETWORK_PATH"] = network_path
    app.config["_SHARED_DIR"] = tmp_path
    return app


@pytest.fixture
def stub_triager():
    """Swap the triager for a fake that records calls and returns a
    canned verdict. Restores the original at teardown."""
    from evolve_admin.intake import classifier as _c

    calls: list[dict] = []

    def fake(title, body, repo, author, ctx):
        calls.append({
            "title": title, "body": body, "repo": repo, "author": author,
        })
        return _c.TriageVerdict(
            category="bug",
            merit="real",
            urgency="p2",
            duplicate_of=["evolve-ops/evolve#42"],
            recommendation="route_to_admin",
            draft_reply="Thanks for the report — looks like a real bug.",
            draft_labels=["bug", "needs-triage"],
            estimated_effort="small",
            confidence=0.78,
            reasoning="Concrete repro steps; matches the cost-breaker symptom class.",
        )

    original = _c.get_triager()
    _c.set_triager(fake)
    try:
        yield calls
    finally:
        _c.set_triager(original)


def test_triage_missing_intake_returns_404(intake_app):
    client = intake_app.test_client()
    r = client.post("/api/evo/intake/intake-does-not-exist/triage")
    assert r.status_code == 404
    assert r.get_json()["error"] == "not found"


def test_triage_writes_verdict_onto_intake(intake_app, stub_triager):
    client = intake_app.test_client()
    # Create an intake first.
    created = client.post(
        "/api/evo/intake",
        json={
            "kind": "bug",
            "body": "Home banner shows wrong relative time\n\n"
                    "The narrative said \"about an hour ago\" when the breaker "
                    "actually tripped 22 hours ago.",
        },
    ).get_json()
    intake_id = created["intake"]["id"]
    assert created["intake"]["triage"] is None

    # Triage it.
    r = client.post(f"/api/evo/intake/{intake_id}/triage")
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["ok"] is True
    triage = data["intake"]["triage"]
    assert triage is not None
    assert triage["category"] == "bug"
    assert triage["merit"] == "real"
    assert triage["urgency"] == "p2"
    assert triage["recommendation"] == "route_to_admin"
    assert triage["confidence"] == 0.78
    assert triage["duplicate_of"] == ["evolve-ops/evolve#42"]
    assert "real bug" in triage["draft_reply"]
    assert triage["draft_labels"] == ["bug", "needs-triage"]
    assert triage["estimated_effort"] == "small"
    assert "Concrete repro" in triage["reasoning"]
    assert triage["inbound_title"] == "Home banner shows wrong relative time"
    assert triage["inbound_body_short"].startswith("The narrative said")

    # Classifier was called with the right title / body split.
    assert len(stub_triager) == 1
    call = stub_triager[0]
    assert call["title"] == "Home banner shows wrong relative time"
    assert call["body"].startswith("The narrative said")


def test_triage_persists_to_disk(intake_app, stub_triager, tmp_path):
    """A subsequent GET reads the same triage back from disk."""
    client = intake_app.test_client()
    created = client.post(
        "/api/evo/intake", json={"kind": "feature", "body": "Add dark mode"},
    ).get_json()
    intake_id = created["intake"]["id"]
    client.post(f"/api/evo/intake/{intake_id}/triage")

    r = client.get(f"/api/evo/intake/{intake_id}")
    assert r.status_code == 200
    triage = r.get_json()["intake"]["triage"]
    assert triage is not None
    assert triage["category"] == "bug"  # stub returns "bug" regardless


def test_re_triage_overwrites_existing_verdict(intake_app):
    """Operator clicks Triage again — new verdict replaces the old one."""
    from evolve_admin.intake import classifier as _c

    client = intake_app.test_client()
    created = client.post(
        "/api/evo/intake", json={"kind": "bug", "body": "Something is broken"},
    ).get_json()
    intake_id = created["intake"]["id"]

    def first_pass(t, b, repo, author, ctx):
        return _c.TriageVerdict(category="bug", confidence=0.3,
                                reasoning="first")

    def second_pass(t, b, repo, author, ctx):
        return _c.TriageVerdict(category="question", confidence=0.9,
                                reasoning="second")

    original = _c.get_triager()
    try:
        _c.set_triager(first_pass)
        r1 = client.post(f"/api/evo/intake/{intake_id}/triage").get_json()
        assert r1["intake"]["triage"]["confidence"] == 0.3
        assert r1["intake"]["triage"]["reasoning"] == "first"

        _c.set_triager(second_pass)
        r2 = client.post(f"/api/evo/intake/{intake_id}/triage").get_json()
        assert r2["intake"]["triage"]["confidence"] == 0.9
        assert r2["intake"]["triage"]["reasoning"] == "second"
        assert r2["intake"]["triage"]["category"] == "question"
    finally:
        _c.set_triager(original)


def test_triage_derives_repo_from_promotion_url(intake_app, stub_triager):
    """When the intake has been promoted, the route extracts owner/repo
    from the promotion URL so the classifier gets the right context."""
    from evolve_admin.intake import store as _store
    from evolve_admin.intake import envelope as _env

    client = intake_app.test_client()
    created = client.post(
        "/api/evo/intake", json={"kind": "bug", "body": "Title\n\nBody"},
    ).get_json()
    intake_id = created["intake"]["id"]

    # Simulate a prior promotion — synthesize the on-disk state. We stay
    # in the "open" subdir to keep the test simple; the route doesn't
    # care about state, only that the intake exists and has promotion
    # fields populated.
    shared_dir = intake_app.config["_SHARED_DIR"]
    located = _store.find_intake(shared_dir, intake_id)
    assert located is not None
    intake, _, _ = located
    intake.promotion.github_issue_url = (
        "https://github.com/evolve-ops/evolve/issues/2165"
    )
    intake.promotion.promoted_by = "evolve-ops-bot"
    _store.write_intake(intake, shared_dir)

    r = client.post(f"/api/evo/intake/{intake_id}/triage")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert len(stub_triager) == 1
    assert stub_triager[0]["repo"] == "evolve-ops/evolve"
    assert stub_triager[0]["author"] == "evolve-ops-bot"
