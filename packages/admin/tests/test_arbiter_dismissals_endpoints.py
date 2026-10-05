"""tests/test_arbiter_dismissals_endpoints.py — Phase A.5 endpoints.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md §"Decline buttons".

Exercises the three routes touching the signature-based dismissal store:

  - POST   /api/arbiter/proposals/<id>/dismiss   (hook: records suppression)
  - GET    /api/arbiter/dismissals               (lists active suppressions)
  - DELETE /api/arbiter/dismissals/<key>         (lifts a suppression)

The endpoints route through ``arbiter.dismissals`` for the storage layer;
the module-level tests in ``analyzer/tests/test_arbiter_dismissals.py``
already pin its semantics. These tests pin the wiring: dismiss writes
to the store, GET reads it, DELETE flips state, and bot-scoping flows
through correctly.
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
from arbiter.store import write_proposal  # noqa: E402
from arbiter import dismissals  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def arbiter_app(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    (shared_dir / "better-engine").mkdir(parents=True)
    (shared_dir / "proposals").mkdir(parents=True)
    network = {"members": ["team_bot_a", "ellie"], "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, shared_dir


def _seed_pending_with_signature(
    shared_dir: Path,
    *,
    bot_id: str = "team_bot_a",
    signature: str = "test_gen:abc123",
    scope: str = "kind",
):
    """Seed a pending proposal with a dismiss_signature for Phase A.5."""
    p = make_investigation_proposal(bot_id=bot_id, problem="seeded")
    p.dismiss_signature = signature
    p.dismiss_scope = scope  # type: ignore[assignment]
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared_dir)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/proposals/<id>/dismiss — Phase A.5 store hook
# ─────────────────────────────────────────────────────────────────────────────


def test_dismiss_records_signature_suppression(arbiter_app):
    """Dismissing a proposal with dismiss_signature writes the
    suppression so future emissions of the same finding skip."""
    app, shared = arbiter_app
    p = _seed_pending_with_signature(
        shared, bot_id="team_bot_a", signature="cache_ttl:high_miss"
    )
    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/dismiss",
            json={"rationale": "irrelevant for this bot"},
        )
        assert resp.status_code == 200, resp.get_json()

    assert dismissals.is_suppressed(
        shared, signature="cache_ttl:high_miss", bot_id="team_bot_a"
    ) is True


def test_dismiss_per_bot_scoping_default(arbiter_app):
    """Per-bot is the default — a dismiss for bot A does NOT suppress
    the same signature for bot B."""
    app, shared = arbiter_app
    p = _seed_pending_with_signature(
        shared, bot_id="team_bot_a", signature="auth_drift:slack"
    )
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dismiss", json={})

    # team_bot_a is suppressed:
    assert dismissals.is_suppressed(
        shared, signature="auth_drift:slack", bot_id="team_bot_a"
    ) is True
    # ellie is not:
    assert dismissals.is_suppressed(
        shared, signature="auth_drift:slack", bot_id="ellie"
    ) is False


def test_dismiss_pod_wide_flag_suppresses_all_bots(arbiter_app):
    """pod_wide=True drops bot scoping — applies to any bot."""
    app, shared = arbiter_app
    p = _seed_pending_with_signature(
        shared, bot_id="team_bot_a", signature="audit:weekly_summary"
    )
    with app.test_client() as c:
        c.post(
            f"/api/arbiter/proposals/{p.id}/dismiss",
            json={"pod_wide": True},
        )

    # Any bot is now suppressed for that signature.
    assert dismissals.is_suppressed(
        shared, signature="audit:weekly_summary", bot_id="team_bot_a"
    ) is True
    assert dismissals.is_suppressed(
        shared, signature="audit:weekly_summary", bot_id="ellie"
    ) is True


def test_dismiss_without_signature_falls_back_to_instance(arbiter_app):
    """A proposal without dismiss_signature still gets an entry in the
    store — instance-scoped, so only this proposal id is suppressed."""
    app, shared = arbiter_app
    p = make_investigation_proposal(bot_id="team_bot_a", problem="no sig")
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared)
    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dismiss", json={})
        assert resp.status_code == 200

    # The instance key got recorded; future kind-wide checks on an empty
    # signature still return False (instance suppressions don't leak).
    assert dismissals.is_suppressed(
        shared, signature="", bot_id="team_bot_a"
    ) is False
    # iter_active should yield the instance entry.
    entries = list(dismissals.iter_active(shared))
    assert any(e["key"] == f"proposal:{p.id}" for e in entries)


def test_dismiss_ttl_zero_means_permanent(arbiter_app):
    """ttl_days=0 (UI 'permanent') stores expires_at=None — never
    expires until explicitly lifted."""
    app, shared = arbiter_app
    p = _seed_pending_with_signature(
        shared, signature="security:noisy_pattern"
    )
    with app.test_client() as c:
        c.post(
            f"/api/arbiter/proposals/{p.id}/dismiss",
            json={"ttl_days": 0},
        )

    entries = list(dismissals.iter_active(shared))
    matching = [e for e in entries if e["signature"] == "security:noisy_pattern"]
    assert len(matching) == 1
    assert matching[0]["expires_at"] is None


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/arbiter/dismissals — list
# ─────────────────────────────────────────────────────────────────────────────


def test_list_dismissals_empty(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/dismissals")
        assert resp.status_code == 200
        assert resp.get_json() == {"dismissals": []}


def test_list_dismissals_returns_active_suppressions(arbiter_app):
    """List endpoint returns each active suppression."""
    app, shared = arbiter_app
    dismissals.record_dismissal(
        shared,
        signature="cache_ttl:miss",
        bot_id="team_bot_a",
        scope="kind",
        ttl_days=90,
        rationale="not yet",
    )
    dismissals.record_dismissal(
        shared,
        signature="audit:summary",
        bot_id="ellie",
        scope="kind",
        ttl_days=90,
        rationale="quiet please",
    )

    with app.test_client() as c:
        resp = c.get("/api/arbiter/dismissals")
        assert resp.status_code == 200
        items = resp.get_json()["dismissals"]
        assert len(items) == 2
        sigs = {e["signature"] for e in items}
        assert sigs == {"cache_ttl:miss", "audit:summary"}


def test_list_dismissals_skips_lifted_entries(arbiter_app):
    """An entry lifted via DELETE should not appear in the list."""
    app, shared = arbiter_app
    dismissals.record_dismissal(
        shared,
        signature="cache_ttl:miss",
        bot_id="team_bot_a",
        scope="kind",
        ttl_days=90,
        rationale="initial",
    )
    dismissals.lift_dismissal(
        shared, key="cache_ttl:miss", bot_id="team_bot_a", rationale="re-enable",
    )
    with app.test_client() as c:
        items = c.get("/api/arbiter/dismissals").get_json()["dismissals"]
        assert items == []


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/arbiter/dismissals/<key> — lift
# ─────────────────────────────────────────────────────────────────────────────


def test_lift_dismissal_flips_is_suppressed(arbiter_app):
    """DELETE flips is_suppressed back to False."""
    app, shared = arbiter_app
    dismissals.record_dismissal(
        shared,
        signature="cache_ttl:miss",
        bot_id="team_bot_a",
        scope="kind",
        ttl_days=90,
        rationale="initial",
    )
    assert dismissals.is_suppressed(
        shared, signature="cache_ttl:miss", bot_id="team_bot_a"
    ) is True

    with app.test_client() as c:
        resp = c.delete(
            "/api/arbiter/dismissals/cache_ttl:miss?bot_id=team_bot_a",
            json={"rationale": "operator changed mind"},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["lifted"]["signature"] == "cache_ttl:miss"

    assert dismissals.is_suppressed(
        shared, signature="cache_ttl:miss", bot_id="team_bot_a"
    ) is False


def test_lift_dismissal_returns_404_when_nothing_active(arbiter_app):
    """Lifting a non-existent or already-lifted suppression returns
    404 so the UI can refresh its (possibly stale) view."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.delete(
            "/api/arbiter/dismissals/never_recorded?bot_id=team_bot_a",
            json={},
        )
        assert resp.status_code == 404
