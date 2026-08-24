"""Tests for the bot-facing app capability route — ``/api/applications/expand``.

Spec: internal/spec-app-invocation-just-works-2026-06-29.md §2.1 (recognition, Tier-2).
The security-critical surface: the calling bot is bound server-side from the unix-socket
kernel peer uid, NEVER from a request field (same primitive as directory_bot_routes /
google_bot_routes). These tests assert, end-to-end through the route:

  (a) identity binding — TCP / unknown-uid → 403;
  (b) resolution        — id / name / display_name all resolve; a miss → 404 with the
      DEFINED ids that would resolve (recoverable);
  (c) discovered apps   — expandable on demand (the defined-only filter is on the Tier-1
      menu, not on this Tier-2 lookup);
  (d) read-only         — the route never writes; a bad body is a clean 400, not a 500.

Uses Flask's test client with manually-set request.environ (REMOTE_TRANSPORT /
REMOTE_PEER_UID) to simulate unix-socket peer credentials — same technique as
test_directory_bot_routes / test_google_bot_routes. ``list_manifests`` is monkeypatched
so the route logic is exercised without the per-bot workspace filesystem plumbing.

FAKE ids only (docs/PLACEHOLDER_NAMING.md).
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web import applications_bot_routes as abr  # noqa: E402
from evolve_admin.web.applications_bot_routes import (  # noqa: E402
    register_applications_bot_routes,
)
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402


_ME = pwd.getpwuid(os.getuid()).pw_name
BOT = "lex"


def _mk(**kw) -> ApplicationManifest:
    base = {"bot_id": BOT, "status": "active"}
    base.update(kw)
    return ApplicationManifest.from_dict(base)


DEFINED = _mk(
    id="task_manager", name="task_manager", display_name="Task Manager",
    definition_status="defined",
    identity={"purpose": "Track your to-dos and reminders"},
    interface_contract={"cli": [{"command": "tasks.py add", "key_flags": ["--due"]}]},
)
JOURNAL = _mk(
    id="journal", name="journal", display_name="Journal",
    definition_status="defined",
    identity={"purpose": "Keep a daily journal"},
)
DISCOVERED = _mk(
    id="scratch", name="scratch", display_name="Scratch",
    definition_status="discovered",
    identity={"purpose": "A discovered scratch script"},
)
HIDDEN = _mk(
    id="secret_paused", name="secret_paused", display_name="Paused App",
    status="paused", definition_status="defined",
    identity={"purpose": "should never be expandable"},
)

_MANIFESTS = [DEFINED, JOURNAL, DISCOVERED, HIDDEN]


def _write_network(tmp_path: Path) -> Path:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {BOT: {"role": "member", "user": _ME}},
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}, "names": {}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def app(tmp_path, monkeypatch):
    network_path = _write_network(tmp_path)
    # Exercise the route logic without the per-bot workspace filesystem.
    monkeypatch.setattr(abr, "list_manifests", lambda shared, bot: list(_MANIFESTS))
    # Capture audit calls instead of writing the real log.
    audits: list[tuple] = []
    monkeypatch.setattr(
        abr, "_audit_log_entry",
        lambda action, bot_id, details, **kw: audits.append((action, bot_id, details)))
    a = Flask(__name__)
    register_applications_bot_routes(a, network_path)
    a.config["TESTING"] = True
    a._audits = audits  # type: ignore[attr-defined]
    return a


def _unix_env(uid: int | None = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


# ── (a) identity binding ──────────────────────────────────────────────────────


def test_expand_403_for_tcp(app):
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "task_manager"},
                      environ_overrides={"REMOTE_PEER_UID": os.getuid()})  # no transport
    assert resp.status_code == 403


def test_expand_403_for_unknown_uid(app):
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "task_manager"},
                      environ_overrides=_unix_env(uid=4242424))
    assert resp.status_code == 403


# ── (b) resolution ────────────────────────────────────────────────────────────


def test_expand_by_id(app):
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "task_manager"},
                      environ_overrides=_unix_env())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["app_id"] == "task_manager"
    assert "tasks.py add" in data["detail"]
    assert data["definition_status"] == "defined"


def test_expand_by_display_name_case_insensitive(app):
    """A model that passes the display name (not the id) still hits — anti-rigidity."""
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "  TASK manager "},
                      environ_overrides=_unix_env())
    assert resp.status_code == 200
    assert resp.get_json()["app_id"] == "task_manager"


def test_expand_miss_lists_defined_available(app):
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "nope"},
                      environ_overrides=_unix_env())
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["ok"] is False
    # Recovery ids are the DEFINED menu — discovered/hidden apps are not advertised.
    assert set(data["available"]) == {"task_manager", "journal"}


# ── (c) discovered apps expandable on demand ──────────────────────────────────


def test_discovered_app_is_expandable_on_demand(app):
    """OQ-3: discovered apps stay OUT of the always-on menu but CAN be expanded if the
    model asks for one by id."""
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "scratch"},
                      environ_overrides=_unix_env())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["definition_status"] == "discovered"


def test_paused_app_is_not_expandable(app):
    """Non-visible (paused/hidden/deprecated) apps must not be expandable."""
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "secret_paused"},
                      environ_overrides=_unix_env())
    assert resp.status_code == 404


# ── (d) input validation (never 500) ──────────────────────────────────────────


def test_missing_app_id_400(app):
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={}, environ_overrides=_unix_env())
    assert resp.status_code == 400


def test_app_id_too_long_400(app):
    with app.test_client() as c:
        resp = c.post("/api/applications/expand", json={"app_id": "x" * 500},
                      environ_overrides=_unix_env())
    assert resp.status_code == 400


# ── observability (OQ-4) ──────────────────────────────────────────────────────


def test_expand_hit_is_audited(app):
    with app.test_client() as c:
        c.post("/api/applications/expand", json={"app_id": "task_manager"},
               environ_overrides=_unix_env())
    actions = [a for a in app._audits if a[0] == "capability_index.expand_app"]
    assert len(actions) == 1
    _, bot_id, details = actions[0]
    assert bot_id == BOT
    assert details["hit"] is True
    assert details["app_id"] == "task_manager"


def test_expand_miss_is_audited(app):
    with app.test_client() as c:
        c.post("/api/applications/expand", json={"app_id": "nope"},
               environ_overrides=_unix_env())
    actions = [a for a in app._audits if a[0] == "capability_index.expand_app"]
    assert len(actions) == 1
    assert actions[0][2]["hit"] is False
