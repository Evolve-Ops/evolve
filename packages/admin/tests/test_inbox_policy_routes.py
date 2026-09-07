"""Tests for the Phase 5 policy + apply + undo HTTP endpoints.

Endpoints under test:
  GET  /api/inbox/triage/policy
  POST /api/inbox/triage/policy
  POST /api/inbox/triage/<id>/apply
  POST /api/inbox/triage/<id>/undo
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
    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        # v1 (legacy single-target) intake.github shape: a flat
        # owner/repo at the top of the block. promote.from_network
        # builds an implicit "default" target with token_slot=github_intake.
        "intake": {
            "github": {
                "owner": "evolve-ops",
                "repo": "evolve",
                "token_slot": "github_intake",
            },
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    app.config["_SHARED_DIR"] = tmp_path
    return app


def _seed_inbound(
    shared_dir, *, id_,
    confidence=0.95,
    recommendation="auto_close_duplicate",
    duplicate_of=("evolve-ops/evolve#42",),
    draft_reply="Could you share the OC version?",
    auto_action=None,
):
    from evolve_admin.intake.envelope import Intake, TriageRecord, AutoActionRecord
    from evolve_admin.intake import store
    ix = Intake(id=id_, kind="bug", body="inbound body")
    ix.state = "filed"
    ix.inbound = True
    ix.promotion.github_issue_url = (
        "https://github.com/evolve-ops/evolve/issues/7"
    )
    ix.promotion.github_issue_number = 7
    ix.triage = TriageRecord(
        category="bug", urgency="p1",
        recommendation=recommendation,
        confidence=confidence,
        duplicate_of=list(duplicate_of),
        draft_reply=draft_reply,
        inbound_author="alice", inbound_title="X is broken",
        inbound_body_short="…",
    )
    if auto_action is not None:
        ix.auto_action = auto_action
    store.write_intake(ix, shared_dir)
    return ix


def _seed_keystore_token(shared_dir, *, slot="github_intake", token="ghp_test"):
    """The /apply and /undo endpoints look up a GitHub token from the
    keystore. KeystoreManager needs the slot registered before
    ``set_value`` will store the value (matching production setup)."""
    from evolve_admin.keystore import KeystoreManager
    km = KeystoreManager(shared_dir)
    km.register(
        name=slot, provider="github", scope="shared",
        description="test", value=token,
    )


# ─── GET /api/inbox/triage/policy ────────────────────────────────────────────


def test_get_policy_returns_defaults_when_missing(app):
    """No policy file = everything off, returns 200 not 404."""
    r = app.test_client().get("/api/inbox/triage/policy")
    assert r.status_code == 200
    p = r.get_json()["policy"]
    assert p["enabled"] is False
    assert p["close_duplicate_enabled"] is False


def test_get_policy_reads_persisted_state(app):
    from evolve_admin.intake.policy import AutoResponsePolicy, save_policy
    save_policy(
        app.config["_SHARED_DIR"],
        AutoResponsePolicy(enabled=True, close_duplicate_enabled=True),
    )
    p = app.test_client().get("/api/inbox/triage/policy").get_json()["policy"]
    assert p["enabled"] is True
    assert p["close_duplicate_enabled"] is True


# ─── POST /api/inbox/triage/policy ───────────────────────────────────────────


def test_post_policy_merges_with_current(app):
    """A PATCH-style update — send one field, others stay as they were."""
    client = app.test_client()
    # First write: enable globally with close_duplicate on.
    client.post("/api/inbox/triage/policy", json={
        "enabled": True, "close_duplicate_enabled": True,
    })
    # Now flip just the global enabled flag back to False.
    body = client.post(
        "/api/inbox/triage/policy", json={"enabled": False},
    ).get_json()
    # close_duplicate_enabled should still be True (merge semantics).
    assert body["policy"]["enabled"] is False
    assert body["policy"]["close_duplicate_enabled"] is True


def test_post_policy_persists_across_requests(app):
    client = app.test_client()
    client.post("/api/inbox/triage/policy", json={
        "enabled": True, "note": "two-week observation done",
    })
    fetched = client.get("/api/inbox/triage/policy").get_json()
    assert fetched["policy"]["enabled"] is True
    assert fetched["policy"]["note"] == "two-week observation done"


def test_post_policy_rejects_non_object(app):
    r = app.test_client().post(
        "/api/inbox/triage/policy", json=["not", "an", "object"],
    )
    assert r.status_code == 400


# ─── POST /api/inbox/triage/<id>/apply ───────────────────────────────────────


class _FakeTransport:
    def __init__(self):
        self.requests = []
        self._comment_id = 8000

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url))
        if method == "POST" and "/comments" in url:
            self._comment_id += 1
            return 201, {"id": self._comment_id}
        if method == "PATCH":
            return 200, {"state": "closed"}
        if method == "DELETE":
            return 204, {}
        return 200, {}


def test_apply_404_when_intake_missing(app):
    _seed_keystore_token(app.config["_SHARED_DIR"])
    r = app.test_client().post(
        "/api/inbox/triage/nope/apply", json={"actor": "evo-bot"},
    )
    assert r.status_code == 404


def test_apply_409_when_intake_not_inbound(app):
    """The /apply endpoint is for inbound only — operator-filed
    intakes route through /api/evo/intake/<id>/promote instead."""
    from evolve_admin.intake.envelope import Intake
    from evolve_admin.intake import store
    shared = app.config["_SHARED_DIR"]
    ix = Intake(id="ob-1", kind="bug", body="outbound body")
    ix.state = "filed"
    store.write_intake(ix, shared)
    _seed_keystore_token(shared)
    r = app.test_client().post("/api/inbox/triage/ob-1/apply")
    assert r.status_code == 409


def test_apply_409_when_already_actioned(app, monkeypatch):
    from evolve_admin.intake.envelope import AutoActionRecord
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1", auto_action=AutoActionRecord(
        kind="close_duplicate", actor="evo-bot",
    ))
    _seed_keystore_token(shared)
    r = app.test_client().post("/api/inbox/triage/in-1/apply")
    assert r.status_code == 409


def test_apply_calls_auto_responder_with_kind_from_recommendation(app, monkeypatch):
    """The default kind derives from triage.recommendation. Verify the
    POST flows into apply() with the right kind."""
    from evolve_admin.intake import auto_responder
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1", recommendation="auto_close_duplicate")
    _seed_keystore_token(shared)
    fake = _FakeTransport()
    monkeypatch.setattr(auto_responder, "default_transport", fake)
    r = app.test_client().post(
        "/api/inbox/triage/in-1/apply", json={"actor": "evo-bot"},
    )
    assert r.status_code == 200
    methods = [m for (m, _u) in fake.requests]
    assert "POST" in methods  # comment
    assert "PATCH" in methods  # close


def test_apply_persists_auto_action(app, monkeypatch):
    from evolve_admin.intake import auto_responder, store
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1")
    _seed_keystore_token(shared)
    monkeypatch.setattr(auto_responder, "default_transport", _FakeTransport())
    app.test_client().post(
        "/api/inbox/triage/in-1/apply", json={"actor": "evo-bot"},
    )
    found = store.find_intake(shared, "in-1")
    assert found is not None
    refreshed, _path, _sd = found
    assert refreshed.auto_action is not None
    assert refreshed.auto_action.kind == "close_duplicate"
    assert refreshed.auto_action.reason == "manual"


def test_apply_409_when_recommendation_has_no_action(app, monkeypatch):
    """The route_to_admin verdict has no auto-action handler."""
    from evolve_admin.intake import auto_responder
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1", recommendation="route_to_admin")
    _seed_keystore_token(shared)
    monkeypatch.setattr(auto_responder, "default_transport", _FakeTransport())
    r = app.test_client().post("/api/inbox/triage/in-1/apply")
    assert r.status_code == 409


def test_apply_explicit_kind_overrides_recommendation(app, monkeypatch):
    """Operator can force a different action — useful for verdicts whose
    recommendation is 'route_to_admin' but the operator decides to
    label-only it."""
    from evolve_admin.intake import auto_responder
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1", recommendation="route_to_admin")
    # The seed doesn't set draft_labels; add one so label_only can fire.
    from evolve_admin.intake import store
    found = store.find_intake(shared, "in-1")
    assert found is not None
    ix, _p, _sd = found
    ix.triage.draft_labels = ["needs-triage"]
    store.write_intake(ix, shared)
    _seed_keystore_token(shared)
    monkeypatch.setattr(auto_responder, "default_transport", _FakeTransport())
    r = app.test_client().post(
        "/api/inbox/triage/in-1/apply", json={"kind": "label_only"},
    )
    assert r.status_code == 200


# ─── POST /api/inbox/triage/<id>/undo ────────────────────────────────────────


def test_undo_404_when_intake_missing(app):
    _seed_keystore_token(app.config["_SHARED_DIR"])
    r = app.test_client().post("/api/inbox/triage/nope/undo")
    assert r.status_code == 404


def test_undo_409_when_no_auto_action(app):
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1")
    _seed_keystore_token(shared)
    r = app.test_client().post("/api/inbox/triage/in-1/undo")
    assert r.status_code == 409


def test_undo_round_trip_marks_undone(app, monkeypatch):
    """apply then undo back-to-back — auto_action.undone should be True
    on re-read, and the GH side should see PATCH+DELETE on undo."""
    from evolve_admin.intake import auto_responder, store
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1")
    _seed_keystore_token(shared)
    fake = _FakeTransport()
    monkeypatch.setattr(auto_responder, "default_transport", fake)
    client = app.test_client()
    client.post("/api/inbox/triage/in-1/apply", json={"actor": "evo-bot"})
    fake.requests.clear()
    r = client.post("/api/inbox/triage/in-1/undo")
    assert r.status_code == 200
    # First undo request should be a PATCH (reopen), then DELETE (comment).
    methods = [m for (m, _u) in fake.requests]
    assert "PATCH" in methods
    assert "DELETE" in methods
    found = store.find_intake(shared, "in-1")
    refreshed, _path, _sd = found
    assert refreshed.auto_action.undone is True


def test_undo_409_when_already_undone(app, monkeypatch):
    from evolve_admin.intake import auto_responder
    shared = app.config["_SHARED_DIR"]
    _seed_inbound(shared, id_="in-1")
    _seed_keystore_token(shared)
    monkeypatch.setattr(auto_responder, "default_transport", _FakeTransport())
    client = app.test_client()
    client.post("/api/inbox/triage/in-1/apply", json={"actor": "evo-bot"})
    client.post("/api/inbox/triage/in-1/undo")
    second = client.post("/api/inbox/triage/in-1/undo")
    assert second.status_code == 409


def test_apply_412_when_no_intake_github_config(tmp_path):
    """If intake.github isn't configured, /apply must surface a 412 with
    a guidance message — same shape as /promote."""
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes
    network = {"sharedDir": str(tmp_path), "primary": "evo"}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    _seed_inbound(tmp_path, id_="in-1")
    r = app.test_client().post("/api/inbox/triage/in-1/apply")
    assert r.status_code == 412
    assert "intake.github not configured" in r.get_json()["error"]
