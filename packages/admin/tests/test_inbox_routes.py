"""Tests for the Inbox API surface (Phase 1).

Endpoints under test:
  GET  /api/inbox                          — list filed intakes with
                                              unread_activity_count
  POST /api/evo/intake/<id>/seen           — mark activity seen
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
def inbox_app(tmp_path):
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
    app.config["_SHARED_DIR"] = tmp_path
    return app


def _seed_filed_intake(
    shared_dir: Path,
    *,
    id_: str,
    repo: str = "openclaw/openclaw",
    number: int = 84820,
    activity: list[dict] | None = None,
    last_seen: str = "",
):
    """Helper: write a filed intake with optional activity log."""
    from evolve_admin.intake import store
    from evolve_admin.intake.envelope import ActivityEvent, Intake

    ix = Intake(id=id_, kind="bug", body=f"body for {id_}")
    ix.state = "filed"
    ix.promotion.github_issue_url = (
        f"https://github.com/{repo}/issues/{number}"
    )
    ix.promotion.github_issue_number = number
    if activity:
        ix.activity_log = [
            ActivityEvent(
                kind=a.get("kind", "new_comment"),
                actor=a.get("actor", ""),
                observed_at=a.get("observed_at", ""),
                snippet=a.get("snippet", ""),
                ref=a.get("ref", ""),
            )
            for a in activity
        ]
    ix.last_seen_activity_at = last_seen
    store.write_intake(ix, shared_dir)
    return ix


# ─── GET /api/inbox ────────────────────────────────────────────────────────


def test_inbox_list_empty_pod(inbox_app):
    """No filed intakes → empty items list, 200 OK."""
    client = inbox_app.test_client()
    r = client.get("/api/inbox")
    assert r.status_code == 200
    body = r.get_json()
    assert body["items"] == []
    assert body["count"] == 0


def test_inbox_list_returns_filed_intakes_with_unread_count(inbox_app):
    """Each row should carry the intake plus computed
    unread_activity_count + last_activity_at."""
    shared = inbox_app.config["_SHARED_DIR"]
    _seed_filed_intake(
        shared, id_="intake-1",
        activity=[
            {"kind": "new_comment", "observed_at": "2026-05-22T10:00:00Z",
             "actor": "x", "snippet": "first"},
            {"kind": "new_comment", "observed_at": "2026-05-22T11:00:00Z",
             "actor": "y", "snippet": "second"},
        ],
        last_seen="2026-05-22T10:30:00Z",
    )

    client = inbox_app.test_client()
    r = client.get("/api/inbox")
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1
    row = body["items"][0]
    assert row["intake"]["id"] == "intake-1"
    assert row["unread_activity_count"] == 1  # only the 11:00 event
    assert row["last_activity_at"] == "2026-05-22T11:00:00Z"


def test_inbox_list_excludes_unfiled_intakes(inbox_app):
    """Open / triaged / closed intakes don't belong in the Inbox view."""
    from evolve_admin.intake.envelope import Intake
    from evolve_admin.intake import store

    shared = inbox_app.config["_SHARED_DIR"]
    # One open intake (should NOT appear)
    store.write_intake(
        Intake(id="open-1", kind="bug", body="open intake"),
        shared,
    )
    # One filed intake (should appear)
    _seed_filed_intake(shared, id_="filed-1")

    client = inbox_app.test_client()
    body = client.get("/api/inbox").get_json()
    ids = [r["intake"]["id"] for r in body["items"]]
    assert ids == ["filed-1"]


def test_inbox_list_sorts_most_recently_active_first(inbox_app):
    """Items needing the operator's attention should rank to the top."""
    shared = inbox_app.config["_SHARED_DIR"]
    _seed_filed_intake(
        shared, id_="oldish", number=1,
        activity=[{"kind": "new_comment", "observed_at": "2026-05-20T00:00:00Z"}],
    )
    _seed_filed_intake(
        shared, id_="newest", number=2,
        activity=[{"kind": "new_comment", "observed_at": "2026-05-22T12:00:00Z"}],
    )
    _seed_filed_intake(
        shared, id_="middle", number=3,
        activity=[{"kind": "new_comment", "observed_at": "2026-05-21T00:00:00Z"}],
    )

    client = inbox_app.test_client()
    body = client.get("/api/inbox").get_json()
    ids = [r["intake"]["id"] for r in body["items"]]
    assert ids == ["newest", "middle", "oldish"]


def test_inbox_list_falls_back_to_updated_at_when_no_activity(inbox_app):
    """Filed intakes with no activity yet still appear in the list,
    sorted by their updated_at."""
    shared = inbox_app.config["_SHARED_DIR"]
    _seed_filed_intake(shared, id_="no-activity", number=42)
    client = inbox_app.test_client()
    body = client.get("/api/inbox").get_json()
    assert body["count"] == 1
    assert body["items"][0]["unread_activity_count"] == 0
    assert body["items"][0]["last_activity_at"] == ""


def test_inbox_list_unread_only_filter(inbox_app):
    """?unread_only=1 should drop rows with no unread activity."""
    shared = inbox_app.config["_SHARED_DIR"]
    _seed_filed_intake(shared, id_="has-unread", number=1,
                       activity=[{"kind": "new_comment",
                                  "observed_at": "2026-05-22T10:00:00Z"}])
    _seed_filed_intake(shared, id_="no-unread", number=2)

    client = inbox_app.test_client()
    body = client.get("/api/inbox?unread_only=1").get_json()
    ids = [r["intake"]["id"] for r in body["items"]]
    assert ids == ["has-unread"]


def test_inbox_list_unread_only_accepts_truthy_values(inbox_app):
    """Truthy variants for the query param: 1, true, yes."""
    shared = inbox_app.config["_SHARED_DIR"]
    _seed_filed_intake(shared, id_="no-unread", number=1)

    client = inbox_app.test_client()
    for truthy in ("1", "true", "yes", "TRUE", "Yes"):
        body = client.get(f"/api/inbox?unread_only={truthy}").get_json()
        assert body["count"] == 0, f"unread_only={truthy!r} should filter out"
    # No filter → row appears.
    assert client.get("/api/inbox").get_json()["count"] == 1


def test_inbox_list_respects_limit_param(inbox_app):
    """?limit=N caps the response. Invalid/excessive limits are clamped."""
    shared = inbox_app.config["_SHARED_DIR"]
    for i in range(5):
        _seed_filed_intake(shared, id_=f"intake-{i}", number=i + 1)

    client = inbox_app.test_client()
    body = client.get("/api/inbox?limit=2").get_json()
    assert body["count"] == 2

    # Invalid limit falls back to default; large limit caps at 500.
    body_bad = client.get("/api/inbox?limit=not-a-number").get_json()
    assert body_bad["count"] == 5  # all of them


# ─── POST /api/evo/intake/<id>/seen ───────────────────────────────────────


def test_seen_marks_activity_as_read(inbox_app):
    """After POSTing /seen, the unread count drops to zero."""
    shared = inbox_app.config["_SHARED_DIR"]
    _seed_filed_intake(
        shared, id_="seen-test",
        activity=[
            {"kind": "new_comment",
             "observed_at": "2026-05-22T10:00:00Z", "actor": "x"},
        ],
    )
    client = inbox_app.test_client()
    pre = client.get("/api/inbox").get_json()
    assert pre["items"][0]["unread_activity_count"] == 1

    r = client.post("/api/evo/intake/seen-test/seen")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    post = client.get("/api/inbox").get_json()
    assert post["items"][0]["unread_activity_count"] == 0


def test_seen_404_when_intake_missing(inbox_app):
    client = inbox_app.test_client()
    r = client.post("/api/evo/intake/never-existed/seen")
    assert r.status_code == 404


def test_seen_idempotent(inbox_app):
    """Posting /seen twice should be a noop on the second call."""
    shared = inbox_app.config["_SHARED_DIR"]
    _seed_filed_intake(
        shared, id_="idempotent",
        activity=[{"kind": "new_comment",
                   "observed_at": "2026-05-22T10:00:00Z"}],
    )
    client = inbox_app.test_client()
    r1 = client.post("/api/evo/intake/idempotent/seen")
    r2 = client.post("/api/evo/intake/idempotent/seen")
    assert r1.status_code == 200
    assert r2.status_code == 200
    body = client.get("/api/inbox").get_json()
    assert body["items"][0]["unread_activity_count"] == 0
