"""tests/test_release_control_panel.py — the Overview Release control panel
(D-5) endpoints: read-only status, rollback, and the pin/unpin toggle.

These exercise the real Flask handlers in ``web/release_routes.py`` end-to-end
(they fail against ``main``, which only had ``/api/release/promote``). The whole
point of the panel is that an operator never has to ``sudo evolve-admin
release …`` for the common cases: the admin server runs as the ``evolve`` user
that owns ``release.json`` + the fleet checkout, so every action here runs
server-side with zero privilege escalation. Modeled on
``test_release_promote_ux.py`` (PR #2890):

  * ``GET  /api/release/status``  returns the pointer/candidate/behind-tip dict
    and refreshes origin only under canary (so "N behind tip" is the true tip).
  * ``POST /api/release/rollback`` runs the real ``release_rollback`` as a
    background job with promote's restart-tolerant semantics; pre-flight 409s
    keep the button from opening a doomed drawer.
  * ``POST /api/release/pin`` toggles the ``release.json`` pin synchronously
    (pin-to-current only — no free-text ref, that stays on the CLI).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import release_manager as rm  # noqa: E402

_STABLE = "aaaaaaaaaaaa1111111111111111111111111111"
_PREV = "bbbbbbbbbbbb2222222222222222222222222222"
_CAND = "cccccccccccc3333333333333333333333333333"


class _SyncThread:
    """``threading.Thread`` stand-in that runs the target on ``.start()`` so a
    background job reaches a terminal state synchronously within the request."""

    def __init__(self, *a, target=None, daemon=None, **k):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class _FakeThreadingModule:
    Thread = _SyncThread


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A test client in canary mode with the release-job thread run inline."""
    from evolve_admin.web import server
    from evolve_admin.web import release_routes

    net_file = tmp_path / "network.json"
    net_file.write_text('{"sharedDir": "%s"}' % (tmp_path / "shared"))
    (tmp_path / "shared").mkdir()

    monkeypatch.setattr(
        rm, "resolve_release_config",
        lambda net: rm.ReleaseConfig(mode="canary", canary_bot="canary_bot",
                                     soak_minutes=20),
    )
    monkeypatch.setattr(release_routes, "threading", _FakeThreadingModule)
    server._active_job_id.clear()

    app = server.create_app(network_path=net_file)
    app.config["TESTING"] = True
    return app, server


# ── GET /api/release/status ──────────────────────────────────────────────────


def test_status_returns_release_dict_and_fetches_origin_under_canary(client, monkeypatch):
    """The panel's read endpoint hands back exactly the ``release_status`` dict
    (behind-tip number included) and asks it to refresh origin under canary so
    "N commits behind tip" reflects the real tip, not the last puller fetch."""
    app, _server = client
    seen = {}

    def fake_status(repo, shared_dir, cfg=None, deps=None, *, fetch_origin=False):
        seen["fetch_origin"] = fetch_origin
        return {"mode": "canary", "behind_origin": 4,
                "stable": {"sha": _STABLE, "version": "2026.0614.2884"},
                "previous": {"sha": _PREV}, "pin": None, "candidate": None}

    monkeypatch.setattr(rm, "release_status", fake_status)
    with app.test_client() as c:
        resp = c.get("/api/release/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["behind_origin"] == 4
    assert body["stable"]["sha"] == _STABLE
    assert seen["fetch_origin"] is True, "origin must be refreshed under canary"


def test_status_skips_origin_fetch_in_direct_mode(client, monkeypatch):
    """A direct-mode pod has no pointer; the status read must not pay the fetch
    (and the UI hides the panel)."""
    app, _server = client
    monkeypatch.setattr(
        rm, "resolve_release_config",
        lambda net: rm.ReleaseConfig(mode="direct"))
    seen = {}

    def fake_status(repo, shared_dir, cfg=None, deps=None, *, fetch_origin=False):
        seen["fetch_origin"] = fetch_origin
        return {"mode": "direct"}

    monkeypatch.setattr(rm, "release_status", fake_status)
    with app.test_client() as c:
        resp = c.get("/api/release/status")
    assert resp.status_code == 200
    assert resp.get_json()["mode"] == "direct"
    assert seen["fetch_origin"] is False


# ── POST /api/release/rollback ───────────────────────────────────────────────


def _state(stable=_STABLE, previous=_PREV):
    return rm.ReleaseState(
        stable={"sha": stable, "version": "2026.0614.2884"},
        previous=({"sha": previous, "version": "2026.0613.2885"} if previous else None),
    )


def test_rollback_runs_release_rollback_and_finishes_complete(client, monkeypatch):
    """A successful rollback finishes the job as ``complete`` with the moved-to
    sha — and actually calls the real ``release_rollback`` (not promote)."""
    app, server = client
    monkeypatch.setattr(rm, "load_release_state", lambda shared_dir: _state())
    called = {}

    def fake_rollback(repo, shared_dir):
        called["ran"] = True
        return rm.ReleaseTickResult(success=True, fleet_sha=_PREV,
                                    steps=["rollback: aaaa → bbbb", "post-move hooks: ok"])

    monkeypatch.setattr(rm, "release_rollback", fake_rollback)
    with app.test_client() as c:
        resp = c.post("/api/release/rollback")
    assert resp.status_code == 202, resp.get_json()
    assert called.get("ran") is True
    job = server._jobs[resp.get_json()["jobId"]]
    assert job["status"] == "complete"
    assert job["error"] is None
    assert job["result"]["rolled_back_to"] == _PREV
    assert job["result"]["pinned"] is True
    logged = " ".join(e["msg"] for e in job["log"])
    assert "rollback: aaaa" in logged and "post-move hooks" in logged


def test_rollback_failure_finishes_failed_self_contained(client, monkeypatch):
    """A failed rollback surfaces the manager's error verbatim — no 'steps
    above' phantom (the web flow has no separate drawer)."""
    app, server = client
    monkeypatch.setattr(rm, "load_release_state", lambda shared_dir: _state())
    monkeypatch.setattr(
        rm, "release_rollback",
        lambda repo, shared_dir: rm.ReleaseTickResult(
            success=False, error="fleet move failed", steps=["tried"]))
    with app.test_client() as c:
        resp = c.post("/api/release/rollback")
    assert resp.status_code == 202
    job = server._jobs[resp.get_json()["jobId"]]
    assert job["status"] == "failed"
    assert job["error"] == "fleet move failed"
    assert "steps above" not in (job["error"] or "")


def test_rollback_without_previous_is_a_clean_409(client, monkeypatch):
    """No previous release recorded → a human 409, never a doomed job. The
    button is disabled for this case, but the server enforces it too."""
    app, server = client
    monkeypatch.setattr(rm, "load_release_state", lambda shared_dir: _state(previous=None))
    ran = {"rollback": False}
    monkeypatch.setattr(rm, "release_rollback",
                        lambda *a, **k: ran.__setitem__("rollback", True))
    with app.test_client() as c:
        resp = c.post("/api/release/rollback")
    assert resp.status_code == 409
    assert "previous release" in resp.get_json()["error"]
    assert ran["rollback"] is False
    assert not server._active_job_id


def test_rollback_refused_outside_canary(client, monkeypatch):
    app, _server = client
    monkeypatch.setattr(rm, "resolve_release_config",
                        lambda net: rm.ReleaseConfig(mode="direct"))
    with app.test_client() as c:
        resp = c.post("/api/release/rollback")
    assert resp.status_code == 409
    assert "canary" in resp.get_json()["error"]


def test_rollback_on_corrupt_state_is_frozen_409(client, monkeypatch):
    app, _server = client

    def boom(shared_dir):
        raise rm.ReleaseStateCorrupt("release.json missing stable.sha")

    monkeypatch.setattr(rm, "load_release_state", boom)
    with app.test_client() as c:
        resp = c.post("/api/release/rollback")
    assert resp.status_code == 409
    assert "corrupt" in resp.get_json()["error"]


# ── POST /api/release/pin (toggle) ───────────────────────────────────────────


def test_pin_calls_release_pin_and_returns_message(client, monkeypatch):
    app, _server = client
    calls = {}

    def fake_pin(shared_dir, *, repo=None, reason="operator pin", deps=None):
        calls["reason"] = reason
        return True, "pinned to aaaaaaaaaaaa — auto-promotion held until `release unpin`"

    monkeypatch.setattr(rm, "release_pin", fake_pin)
    with app.test_client() as c:
        resp = c.post("/api/release/pin", json={"pinned": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["pinned"] is True
    assert "auto-promotion held" in body["message"]
    # The admin-UI source is recorded in the pin reason for the audit trail.
    assert "admin UI" in calls["reason"]


def test_unpin_calls_release_unpin(client, monkeypatch):
    app, _server = client
    monkeypatch.setattr(rm, "release_unpin",
                        lambda shared_dir: "unpinned — auto-promotion resumes next tick")
    with app.test_client() as c:
        resp = c.post("/api/release/pin", json={"pinned": False})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["pinned"] is False
    assert "auto-promotion resumes" in body["message"]


def test_pin_refusal_surfaces_as_409(client, monkeypatch):
    """``release_pin`` refuses (e.g. a stale ref assertion) → the message is
    handed back as a 409, not a silent success."""
    app, _server = client
    monkeypatch.setattr(
        rm, "release_pin",
        lambda shared_dir, **k: (False, "cannot resolve 'deadbeef': bad ref"))
    with app.test_client() as c:
        resp = c.post("/api/release/pin", json={"pinned": True})
    assert resp.status_code == 409
    assert "cannot resolve" in resp.get_json()["error"]


def test_pin_refused_outside_canary(client, monkeypatch):
    app, _server = client
    monkeypatch.setattr(rm, "resolve_release_config",
                        lambda net: rm.ReleaseConfig(mode="direct"))
    with app.test_client() as c:
        resp = c.post("/api/release/pin", json={"pinned": True})
    assert resp.status_code == 409
    assert "canary" in resp.get_json()["error"]


# ── GET /api/version — sidebar footer legibility (D-2 bug #2) ─────────────────


def test_api_version_surfaces_running_pod_version(client):
    """The sidebar footer (#ui-version) leads with the date-led RUNNING pod
    version, not the raw origin-tip ``git describe`` sha (e.g. ``v0.44-ga985d7c5``)
    it used to show. ``/api/version`` must carry ``evolve_version`` (==
    ``deploy.EVOLVE_VERSION``) so the footer can render ``verDateLabel`` + the
    build sha. The legacy ``version``/``commit``/``date`` keys stay for the
    multi-pod peer switcher (peers_routes) + back-compat with cached pages.

    This is the one operator-facing surface where git-describe leaked (bug #2);
    locking the field here keeps the footer from regressing back to the
    PR-tail/marketing-prefix string the date scheme made misleading."""
    app, _server = client
    from evolve_admin import deploy
    with app.test_client() as c:
        resp = c.get("/api/version")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["evolve_version"] == deploy.EVOLVE_VERSION
    for legacy_key in ("version", "commit", "date"):
        assert legacy_key in body
