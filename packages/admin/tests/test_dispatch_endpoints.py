"""tests/test_dispatch_endpoints.py — Phase 3.1: dispatch endpoint tests.

Spec: docs/spec-take-this-on-evo-dispatch-2026-06-04.md.

Phase 3.1 adds three endpoints + the ``dispatched`` status to the
state machine. The target-side session-start hook is stubbed (Phase
3.3 wires it for real); the rest of the path — dispatch, result,
cancel, and the schema round-trip of dispatch_state on disk — is
covered here.

Endpoints tested:

  POST /api/arbiter/proposals/<id>/dispatch          — operator side
  POST /api/arbiter/proposals/<id>/dispatch/result   — target side
  POST /api/arbiter/proposals/<id>/dispatch/cancel   — operator side
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import find_proposal, write_proposal  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def arbiter_app(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    (shared_dir / "better-engine").mkdir(parents=True)
    network = {"members": ["team_bot_a", "ellie"], "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, shared_dir


def _seed_pending_with_target(shared_dir: Path, *, target: str = "evo",
                              message: str = "fix this",
                              bot_id: str = "team_bot_a"):
    """Create a pending Investigation proposal with dispatch_target set."""
    p = make_investigation_proposal(
        audience="pod_operator", bot_id=bot_id, problem="x",
    )
    p.dispatch_target = target
    p.dispatch_message = message
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared_dir)
    return p


def _seed_pending_without_target(shared_dir: Path, *, bot_id: str = "team_bot_a"):
    """Pending proposal with no dispatch_target — for negative cases."""
    p = make_investigation_proposal(
        audience="pod_operator", bot_id=bot_id, problem="x",
    )
    # leave dispatch_target as None (default)
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared_dir)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/proposals/<id>/dispatch  (happy path + rejections)
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_transitions_pending_to_dispatched(arbiter_app):
    """Happy path: a pending proposal with dispatch_target='evo' moves
    to ``dispatched`` and records dispatch_state with the target,
    timestamp, and message."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="evo", message="resolve X")

    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()
        assert data["ok"] is True
        assert data["new_status"] == "dispatched"

        # Round-trip from disk to confirm dispatch_state landed in the
        # serialized JSON, not just in the response view.
        located = find_proposal(shared, p.id)
        assert located is not None
        on_disk, _path, subdir = located
        assert on_disk.status == "dispatched"
        assert subdir == "pending", (
            "dispatched proposals stay in pending/ subdir until the result "
            "callback resolves them — they're still operator-actionable "
            "(cancel) and we don't want them invisible to the queue scan"
        )
        ds = on_disk.dispatch_state
        assert ds is not None, "dispatch_state must be written on dispatch"
        assert ds.target == "evo"
        assert ds.message == "resolve X"
        assert ds.dispatched_at, "dispatched_at must be stamped"
        assert ds.cancelled_at is None
        assert ds.result is None


def test_dispatch_uses_proposal_target_when_body_empty(arbiter_app):
    """If the request body doesn't override, the proposal's
    dispatch_target is used."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="team_bot_a")

    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        assert resp.status_code == 200
        assert resp.get_json()["proposal"]["dispatch_state"]["target"] == "team_bot_a"


def test_dispatch_request_overrides_proposal_target(arbiter_app):
    """The request body's ``target`` overrides proposal.dispatch_target.
    Lets the operator redirect to a different bot if the generator
    picked the wrong one."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="evo")

    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch",
            json={"target": "team_bot_a"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["proposal"]["dispatch_state"]["target"] == "team_bot_a"


def test_dispatch_rejects_when_no_target_resolvable(arbiter_app):
    """Proposal has no dispatch_target and request didn't override —
    return 400 instead of silently transitioning."""
    app, shared = arbiter_app
    p = _seed_pending_without_target(shared)

    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        assert resp.status_code == 400
        assert "no dispatch target" in resp.get_json()["error"]


def test_dispatch_rejects_non_pending(arbiter_app):
    """A proposal that's already snoozed / applied / etc. cannot be
    dispatched. Status check returns 409."""
    from arbiter.store import move_proposal

    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    # Transition + persist via move_proposal so the file lands in the
    # snoozed/ subdir; otherwise find_proposal reads the stale
    # pending/ copy and the status check incorrectly sees 'pending'.
    transition(p, "snoozed", actor="test", reason="snooze for test")
    move_proposal(p, shared, from_subdir="pending")

    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        assert resp.status_code == 409


def test_dispatch_404_on_missing_proposal(arbiter_app):
    """Stale or wrong id → 404, not 500."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post("/api/arbiter/proposals/does-not-exist/dispatch", json={})
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/proposals/<id>/dispatch/result  (target callback)
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_result_applied_moves_to_applied_status(arbiter_app):
    """outcome='applied' transitions dispatched → applied. The
    proposal also moves from pending/ to applied/ subdir so the
    queue scan reflects the new state."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch/result",
            json={"outcome": "applied", "message": "fixed it"},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["new_status"] == "applied"

    located = find_proposal(shared, p.id)
    assert located is not None
    on_disk, _path, subdir = located
    assert on_disk.status == "applied"
    assert subdir == "applied", "applied proposals live in applied/ subdir"
    assert on_disk.dispatch_state is not None
    assert on_disk.dispatch_state.result is not None
    assert on_disk.dispatch_state.result.outcome == "applied"
    assert on_disk.dispatch_state.result.message == "fixed it"


def test_dispatch_result_failed_moves_to_failed_flagged(arbiter_app):
    """outcome='failed' transitions dispatched → failed_flagged.
    Operator can then retry or dismiss."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch/result",
            json={"outcome": "failed", "message": "couldn't"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["new_status"] == "failed_flagged"


def test_dispatch_result_in_process_also_moves_to_applied(arbiter_app):
    """outcome='in_process' (target asked operator to take over)
    also transitions to applied — the existing _IN_PROCESS_KINDS UI
    logic on the client will surface it under the In Process tab
    via the action_kind check, not via a separate status."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch/result",
            json={"outcome": "in_process", "message": "needs human judgment"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["new_status"] == "applied"


def test_dispatch_result_rejects_invalid_outcome(arbiter_app):
    """A malformed outcome shouldn't transition the proposal. 400
    return with the source value echoed so the caller can debug."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch/result",
            json={"outcome": "weird"},
        )
        assert resp.status_code == 400
        assert "weird" in resp.get_json()["error"]


def test_dispatch_result_rejects_non_dispatched(arbiter_app):
    """Result for a proposal that isn't dispatched is rejected with
    409. Prevents stale callbacks from racing the operator's cancel."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    # NOT dispatched yet.
    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch/result",
            json={"outcome": "applied"},
        )
        assert resp.status_code == 409


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/proposals/<id>/dispatch/cancel  (operator cancel)
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_cancel_transitions_back_to_pending(arbiter_app):
    """Cancel rewinds dispatched → pending. dispatch_state stays for
    audit with cancelled_at stamped."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch/cancel", json={})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["new_status"] == "pending"

    located = find_proposal(shared, p.id)
    assert located is not None
    on_disk, _path, subdir = located
    assert on_disk.status == "pending"
    assert subdir == "pending"
    # dispatch_state stays for audit; cancelled_at is the audit signal.
    assert on_disk.dispatch_state is not None
    assert on_disk.dispatch_state.cancelled_at, (
        "cancelled_at must be stamped so the audit log can reconstruct "
        "the dispatch lifecycle"
    )


def test_dispatch_cancel_rejects_non_dispatched(arbiter_app):
    """Cancel on a proposal that isn't dispatched is rejected with 409.
    Operator can't accidentally rewind a pending or applied proposal."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared)
    # NOT dispatched yet.
    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch/cancel", json={})
        assert resp.status_code == 409


def test_dispatch_cancel_404_on_missing_proposal(arbiter_app):
    """Stale id → 404."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/does-not-exist/dispatch/cancel", json={}
        )
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Full lifecycle smoke
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3.3: dispatch envelope persistence
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_writes_envelope_to_shared_dir(arbiter_app):
    """Phase 3.3: dispatch persists a JSON envelope at
    {shared_dir}/dispatches/{target}/<proposal_id>.json. The target's
    polling loop reads from there. Without persistence the dispatch
    is invisible to the target."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="evo", message="fix X")

    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        assert resp.status_code == 200

    env_path = shared / "dispatches" / "evo" / f"{p.id}.json"
    assert env_path.exists(), (
        f"dispatch envelope missing at {env_path} — target's polling "
        f"loop has nothing to read"
    )

    import json
    envelope = json.loads(env_path.read_text())
    assert envelope["proposal_id"] == p.id
    assert envelope["target"] == "evo"
    assert envelope["message"] == "fix X"
    assert envelope["dispatched_at"], "envelope must stamp dispatched_at"
    # Full proposal payload so the target has context without
    # re-fetching from disk.
    assert envelope["proposal"]["id"] == p.id
    assert envelope["proposal"]["bot_id"] == p.bot_id


def test_dispatch_to_bot_target_writes_under_bot_id_dir(arbiter_app):
    """Bot-targeted dispatches go to dispatches/<bot_id>/ so each bot
    can poll its own directory without seeing others."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="team_bot_a")

    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        assert resp.status_code == 200

    env_path = shared / "dispatches" / "team_bot_a" / f"{p.id}.json"
    assert env_path.exists()


def test_dispatch_rejects_path_traversal_targets(arbiter_app):
    """Defense in depth: a target slug like '../foo' must not write
    outside the dispatches/ directory. The slug validator rejects
    anything that isn't lowercase alnum + - + _."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="evo")

    with app.test_client() as c:
        # Override target via request body.
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch",
            json={"target": "../etc"},
        )
        # The transition still happens (status: dispatched), but the
        # envelope write is skipped (session_id will be None). The
        # important thing is no file gets written outside
        # dispatches/.
        assert resp.status_code == 200

    # Confirm nothing escaped the dispatches/ tree.
    assert not (shared / "etc").exists()
    # And no envelope was written under the bad slug name either.
    bad_dir = shared / "dispatches" / "..etc"
    assert not bad_dir.exists()


def test_dispatch_envelope_atomic_write(arbiter_app):
    """No mid-write artifacts: between the temp file creation and the
    rename, the destination either doesn't exist or contains valid
    JSON. After the dispatch, no .dispatch-*.json tempfiles remain."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="evo")

    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})

    dispatch_dir = shared / "dispatches" / "evo"
    # The only file in the dir should be the proposal envelope, not a
    # stray temp.
    files = sorted(p.name for p in dispatch_dir.iterdir())
    assert files == [f"{p.id}.json"], (
        f"unexpected files in dispatches/evo/: {files} — atomic write "
        f"may have left a tempfile or duplicate"
    )


def test_dispatch_cancel_unlinks_envelope(arbiter_app):
    """Operator cancel best-effort removes the envelope so the
    target's next poll doesn't pick up the stale dispatch."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="evo")

    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        env_path = shared / "dispatches" / "evo" / f"{p.id}.json"
        assert env_path.exists()

        c.post(f"/api/arbiter/proposals/{p.id}/dispatch/cancel", json={})
        assert not env_path.exists(), (
            "cancel must unlink the dispatch envelope so a slow target "
            "doesn't pick up the stale dispatch on its next poll"
        )


def test_full_lifecycle_dispatch_then_result_then_marks_complete(arbiter_app):
    """End-to-end: pending -> dispatched -> applied. Confirms the
    on-disk state matches the API responses at each step + the
    proposal moves through the subdirs correctly."""
    app, shared = arbiter_app
    p = _seed_pending_with_target(shared, target="evo", message="fix X")

    with app.test_client() as c:
        # Step 1: dispatch
        r1 = c.post(f"/api/arbiter/proposals/{p.id}/dispatch", json={})
        assert r1.status_code == 200
        assert r1.get_json()["new_status"] == "dispatched"

        # Step 2: target reports success
        r2 = c.post(
            f"/api/arbiter/proposals/{p.id}/dispatch/result",
            json={
                "outcome": "applied",
                "message": "done",
                "applied_changes": [
                    {"file": "ea-pack/scripts/x.py", "kind": "patch"},
                ],
            },
        )
        assert r2.status_code == 200
        assert r2.get_json()["new_status"] == "applied"

    # Confirm history captures both transitions.
    located = find_proposal(shared, p.id)
    on_disk, _path, subdir = located
    assert on_disk.status == "applied"
    assert subdir == "applied"
    statuses = [h.to_status for h in on_disk.history]
    assert "pending" in statuses
    assert "dispatched" in statuses
    assert "applied" in statuses

    # applied_changes survives the round-trip.
    changes = on_disk.dispatch_state.result.applied_changes
    assert changes == [{"file": "ea-pack/scripts/x.py", "kind": "patch"}]
