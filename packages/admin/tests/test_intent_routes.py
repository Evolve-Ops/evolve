"""Phase 4 of internal/spec-config-intent-system-2026-05-21.md — HTTP routes
for the Security → Intentional Deviations surface.

Pins the contract the UI consumes:

  GET  /api/intents                                — pod-wide list
  GET  /api/intents/<bot_id>                       — per-bot list
  POST /api/intents/<bot_id>/<intent_id>/revoke    — move to intents_archive
  POST /api/intents/<bot_id>/<intent_id>/edit-reason — update reason in place

The route layer is intentionally thin — every operation has a matching
``evolve_admin.config_intent`` helper. These tests exercise the HTTP
contract (status codes, JSON shape, body validation, 404 on missing
intent) using a tmp-path shared dir + Flask's test client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def shared_dir(tmp_path):
    s = tmp_path / "evolve"
    s.mkdir(parents=True)
    return s


@pytest.fixture
def app(tmp_path, shared_dir):
    from evolve_admin.web.server import create_app

    network = {
        "members": [],
        "bots": {"team-bot-a": {"role": "member"}},
        "sharedDir": str(shared_dir),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path=network_path)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _record_test_intent(shared: Path, bot_id: str, field: str, value,
                        reason: str = "test fixture",
                        set_by: str = "pod_admin (admin UI)") -> str:
    from evolve_admin.config_intent import set_intent
    return set_intent(
        bot_id=bot_id, field_path=field, value=value,
        reason=reason, set_by=set_by, shared_dir=shared,
    )


# ── GET /api/intents (pod-wide) ─────────────────────────────────────────────


class TestListAll:
    def test_empty_pod_returns_empty_dict(self, client):
        r = client.get("/api/intents")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload == {"bots": {}}

    def test_returns_intents_per_bot(self, client, shared_dir):
        _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="codex plugin requires exec",
            set_by="plugin_side_effect:codex",
        )
        _record_test_intent(
            shared_dir, "team-bot-c", "tools.fs.workspaceOnly", True,
            reason="bot is intentionally workspace-hardened",
        )
        r = client.get("/api/intents")
        assert r.status_code == 200
        bots = r.get_json()["bots"]
        assert set(bots.keys()) == {"team-bot-a", "team-bot-c"}
        assert len(bots["team-bot-a"]) == 1
        assert bots["team-bot-a"][0]["field_path"] == "tools.exec.security"
        assert bots["team-bot-a"][0]["value"] == "full"
        assert bots["team-bot-c"][0]["field_path"] == "tools.fs.workspaceOnly"


# ── GET /api/intents/<bot_id> ────────────────────────────────────────────────


class TestListPerBot:
    def test_returns_intents_for_bot(self, client, shared_dir):
        _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="codex plugin requires exec",
        )
        r = client.get("/api/intents/team-bot-a")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["bot_id"] == "team-bot-a"
        assert len(payload["intents"]) == 1
        assert payload["intents"][0]["value"] == "full"

    def test_unknown_bot_returns_empty_list(self, client):
        r = client.get("/api/intents/no-such-bot")
        assert r.status_code == 200
        assert r.get_json() == {"bot_id": "no-such-bot", "intents": []}


# ── POST /api/intents/<bot>/<intent>/revoke ──────────────────────────────────


class TestRevoke:
    def test_revoke_moves_to_archive(self, client, shared_dir):
        intent_id = _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="codex plugin requires exec",
        )
        r = client.post(
            f"/api/intents/team-bot-a/{intent_id}/revoke",
            json={"actor": "pod_admin (UI test)"},
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

        # The active list is now empty; the archive holds the intent.
        from evolve_admin.config_intent import get_intent
        assert get_intent("team-bot-a", "tools.exec.security",
                          shared_dir=shared_dir) is None

        sidecar = json.loads(
            (shared_dir / "config_intents" / "team-bot-a.json").read_text(),
        )
        assert sidecar["intents"] == []
        assert any(
            entry.get("id") == intent_id
            for entry in sidecar.get("intents_archive", [])
        )

    def test_revoke_unknown_intent_returns_404(self, client, shared_dir):
        # Seed something so the sidecar exists.
        _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="r",
        )
        r = client.post(
            "/api/intents/team-bot-a/intent-deadbeef/revoke",
            json={"actor": "pod_admin"},
        )
        assert r.status_code == 404
        assert r.get_json()["ok"] is False

    def test_revoke_defaults_actor_when_missing(self, client, shared_dir):
        intent_id = _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="r",
        )
        r = client.post(f"/api/intents/team-bot-a/{intent_id}/revoke",
                        json={})
        assert r.status_code == 200
        # Audit history captured the default actor label.
        sidecar = json.loads(
            (shared_dir / "config_intents" / "team-bot-a.json").read_text(),
        )
        archived = sidecar["intents_archive"][0]
        actor_events = [
            h for h in archived["audit_history"] if h["event"] == "revoked"
        ]
        assert len(actor_events) == 1
        assert actor_events[0]["actor"] == "pod_admin (admin UI)"


# ── POST /api/intents/<bot>/<intent>/edit-reason ────────────────────────────


class TestEditReason:
    def test_edit_updates_reason_and_appends_history(
        self, client, shared_dir,
    ):
        intent_id = _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="codex plugin requires exec",
        )
        r = client.post(
            f"/api/intents/team-bot-a/{intent_id}/edit-reason",
            json={
                "new_reason": "Operator note: kept for custom local script, "
                              "not codex",
                "actor": "pod_admin (UI manual correction)",
            },
        )
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["ok"] is True

        from evolve_admin.config_intent import get_intent
        intent = get_intent("team-bot-a", "tools.exec.security",
                            shared_dir=shared_dir)
        assert intent is not None
        assert intent["reason"].startswith("Operator note")
        # The value AND set_by are unchanged — only the reason moved.
        assert intent["value"] == "full"
        assert intent["set_by"] == "pod_admin (admin UI)"

        # Audit history captures the edit event.
        edits = [
            h for h in intent["audit_history"]
            if h["event"] == "reason_edited"
        ]
        assert len(edits) == 1
        assert edits[0]["from_reason"] == "codex plugin requires exec"
        assert edits[0]["to_reason"].startswith("Operator note")
        assert edits[0]["actor"] == "pod_admin (UI manual correction)"

    def test_edit_empty_reason_returns_400(self, client, shared_dir):
        intent_id = _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="r",
        )
        r = client.post(
            f"/api/intents/team-bot-a/{intent_id}/edit-reason",
            json={"new_reason": "  "},
        )
        assert r.status_code == 400
        assert "non-empty" in r.get_json()["error"]

    def test_edit_missing_reason_returns_400(self, client, shared_dir):
        intent_id = _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="r",
        )
        r = client.post(
            f"/api/intents/team-bot-a/{intent_id}/edit-reason",
            json={},
        )
        assert r.status_code == 400

    def test_edit_unknown_intent_returns_404(self, client, shared_dir):
        # Seed sidecar so the load succeeds.
        _record_test_intent(
            shared_dir, "team-bot-a", "tools.exec.security", "full",
            reason="r",
        )
        r = client.post(
            "/api/intents/team-bot-a/intent-ghost/edit-reason",
            json={"new_reason": "new"},
        )
        assert r.status_code == 404



# ── Phase 4.1 — POST /confirm-queued ────────────────────────────────────────


def _seed_queued_intent(shared_dir, bot_id="team-bot-a"):
    """Same helper shape as in test_config_intent — write a low-confidence
    inferred intent with queued=True so the /confirm-queued tests have
    a realistic target."""
    from evolve_admin.config_intent import set_intent
    intent_id = set_intent(
        bot_id=bot_id, field_path="tools.exec.security", value="full",
        reason="Inference unavailable. Click 'Edit reason' to record actual intent.",
        set_by="inferred:low",
        shared_dir=shared_dir,
    )
    sidecar = shared_dir / "config_intents" / f"{bot_id}.json"
    data = json.loads(sidecar.read_text())
    data["intents"][0]["queued"] = True
    sidecar.write_text(json.dumps(data))
    return intent_id


class TestConfirmQueued:
    def test_accepts_inferred_reason_when_no_new_reason(
        self, client, shared_dir,
    ):
        intent_id = _seed_queued_intent(shared_dir)
        r = client.post(
            f"/api/intents/team-bot-a/{intent_id}/confirm-queued",
            json={"actor": "pod_admin (UI test)"},
        )
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["ok"] is True

        from evolve_admin.config_intent import get_intent
        intent = get_intent("team-bot-a", "tools.exec.security",
                            shared_dir=shared_dir)
        assert intent is not None
        assert "queued" not in intent
        # Reason preserved.
        assert intent["reason"].startswith("Inference unavailable")

    def test_replaces_reason_when_new_reason_supplied(
        self, client, shared_dir,
    ):
        intent_id = _seed_queued_intent(shared_dir)
        r = client.post(
            f"/api/intents/team-bot-a/{intent_id}/confirm-queued",
            json={
                "new_reason": "codex plugin requires exec",
                "actor": "pod_admin (UI replace)",
            },
        )
        assert r.status_code == 200, r.get_json()

        from evolve_admin.config_intent import get_intent
        intent = get_intent("team-bot-a", "tools.exec.security",
                            shared_dir=shared_dir)
        assert intent["reason"].startswith("codex plugin")
        assert "queued" not in intent

    def test_unknown_intent_returns_404(self, client, shared_dir):
        # Seed sidecar so the load succeeds even though the id is wrong.
        _seed_queued_intent(shared_dir)
        r = client.post(
            "/api/intents/team-bot-a/intent-ghost/confirm-queued",
            json={"actor": "pod_admin"},
        )
        assert r.status_code == 404

    def test_default_actor_when_omitted(self, client, shared_dir):
        intent_id = _seed_queued_intent(shared_dir)
        r = client.post(
            f"/api/intents/team-bot-a/{intent_id}/confirm-queued",
            json={},
        )
        assert r.status_code == 200
        # Audit history captured the default actor.
        sidecar = json.loads(
            (shared_dir / "config_intents" / "team-bot-a.json").read_text(),
        )
        intent = sidecar["intents"][0]
        confirms = [h for h in intent["audit_history"]
                    if h["event"] == "confirmed_queued"]
        assert len(confirms) == 1
        assert confirms[0]["actor"] == "pod_admin (admin UI)"

