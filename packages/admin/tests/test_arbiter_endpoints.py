"""tests/test_arbiter_endpoints.py — Phase 1 /api/arbiter/* endpoint tests.

Exercises the routes registered in ``server._register_arbiter_routes`` via
Flask's test client, using temp directories for ``shared_dir`` and the
network config.
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


def _seed_pending(shared_dir: Path, **kwargs):
    p = make_investigation_proposal(audience="pod_operator", **kwargs)
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared_dir)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/arbiter/proposals
# ─────────────────────────────────────────────────────────────────────────────


def test_list_empty_when_no_proposals(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["proposals"] == []


def test_list_returns_seeded_proposals(arbiter_app):
    app, shared = arbiter_app
    _seed_pending(shared, bot_id="team_bot_a", problem="one")
    _seed_pending(shared, bot_id="team_bot_a", problem="two")
    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 2


def test_list_filters_by_bot(arbiter_app):
    app, shared = arbiter_app
    _seed_pending(shared, bot_id="team_bot_a")
    _seed_pending(shared, bot_id="ellie")
    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?bot_id=ellie")
        data = resp.get_json()
        assert data["count"] == 1
        assert data["proposals"][0]["bot_id"] == "ellie"


def test_list_filters_by_dimension(arbiter_app):
    app, shared = arbiter_app
    _seed_pending(shared, dimension="substrate_health")
    _seed_pending(shared, dimension="safety")
    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?dimension=safety")
        data = resp.get_json()
        assert data["count"] == 1
        assert data["proposals"][0]["dimension"] == "safety"


def test_list_orders_by_urgency(arbiter_app):
    app, shared = arbiter_app
    _seed_pending(shared, urgency="improvement", problem="mild")
    _seed_pending(shared, urgency="security_critical", problem="bad")
    _seed_pending(shared, urgency="cost_alert", problem="moderate")
    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals")
        data = resp.get_json()
        urgencies = [p["urgency"] for p in data["proposals"]]
        assert urgencies == [
            "security_critical",
            "cost_alert",
            "improvement",
        ]


def test_list_savings_bumps_ordering_within_same_urgency(arbiter_app):
    """PR H: a Proposal with estimated_savings_usd outranks a peer without.

    Motivating story: the team-bot-a $7.67 incident produced a
    cache_ttl_tuner Proposal that should have appeared at the top of the
    Watchlist (Inbox), not buried by more recent improvement-tier alerts
    with no savings claim. This test locks in that ordering: at the
    same urgency, a proposal with a meaningful savings estimate beats
    a proposal without one.
    """
    from arbiter.store import write_proposal as _wp

    app, shared = arbiter_app

    p_with = make_investigation_proposal(
        urgency="improvement", problem="cache TTL flip", audience="pod_operator",
    )
    p_with.estimated_savings_usd = 5.0
    transition(p_with, "pending", actor="test", reason="seed")
    _wp(p_with, shared)

    p_without = make_investigation_proposal(
        urgency="improvement", problem="other improvement", audience="pod_operator",
    )
    transition(p_without, "pending", actor="test", reason="seed")
    _wp(p_without, shared)

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals")
        data = resp.get_json()
        assert data["count"] == 2
        # First in the list should be the one with savings.
        first = data["proposals"][0]
        assert first["problem"] == "cache TTL flip"
        assert first["estimated_savings_usd"] == 5.0
        # And the breakdown should expose the bonus so the UI can render
        # the score popover row.
        bd = first["_score_breakdown"]
        assert bd is not None
        assert bd["savings_bonus"] > 0


def test_list_altitude_leads_ordering(arbiter_app):
    """Fit Reviewer Bite 2: a higher-altitude proposal sorts above a lower one
    even when the lower carries a higher base score (cost_alert > improvement).
    The rail's altitude fold depends on this (−altitude, −score) ordering.
    """
    from arbiter.store import write_proposal as _wp

    app, shared = arbiter_app
    # L0 with the higher base score (cost_alert 500 > improvement 200).
    _seed_pending(shared, generator_id="gen_l0", urgency="cost_alert",
                  problem="L0 nudge")
    # L2 capability via the per-proposal altitude override.
    high = make_investigation_proposal(
        audience="pod_operator", generator_id="gen_l2",
        urgency="improvement", problem="L2 capability",
    )
    high.altitude = 2
    transition(high, "pending", actor="test", reason="seed")
    _wp(high, shared)

    with app.test_client() as c:
        data = c.get("/api/arbiter/proposals").get_json()
        assert data["count"] == 2
        ordered = data["proposals"]
        assert ordered[0]["problem"] == "L2 capability"
        assert ordered[0]["altitude"] == 2
        assert ordered[1]["altitude"] == 0


def test_list_altitude_resolves_from_app_suggester_charter(arbiter_app):
    """app_suggester's charter declares altitude=2; the proposal view carries
    the charter-resolved altitude without the producer stamping it (same
    resolution shape as ``surface``).
    """
    app, shared = arbiter_app
    _seed_pending(shared, generator_id="app_suggester", urgency="improvement",
                  problem="consider installing Project Tracker")
    with app.test_client() as c:
        data = c.get("/api/arbiter/proposals").get_json()
        assert data["count"] == 1
        assert data["proposals"][0]["altitude"] == 2


def test_list_savings_does_not_override_higher_urgency(arbiter_app):
    """A capped savings improvement still loses to a cost_alert proposal."""
    from arbiter.store import write_proposal as _wp

    app, shared = arbiter_app

    p_savings = make_investigation_proposal(
        urgency="improvement", problem="big savings improvement",
        audience="pod_operator",
    )
    p_savings.estimated_savings_usd = 10_000.0  # saturates the cap
    transition(p_savings, "pending", actor="test", reason="seed")
    _wp(p_savings, shared)

    p_cost = make_investigation_proposal(
        urgency="cost_alert", problem="cost alert", audience="pod_operator",
    )
    transition(p_cost, "pending", actor="test", reason="seed")
    _wp(p_cost, shared)

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals")
        data = resp.get_json()
        urgencies = [p["urgency"] for p in data["proposals"]]
        assert urgencies[0] == "cost_alert"


def test_list_filters_by_exclude_generator_id(arbiter_app):
    """The Self-Improvement page passes exclude_generator_id=operator_ui so
    operator-clicked config changes (which auto-apply inline and live in the
    activity log) don't leak into the LLM-generator review queue.

    Defensive: today operator-UI proposals never reach pending — they
    transition to succeeded/failed_flagged on the click and land in
    archived/. But adding the filter at the API layer means a future code
    path can't accidentally surface them in Self-Improvement.
    """
    app, shared = arbiter_app
    _seed_pending(shared, generator_id="operator_ui", problem="op-click")
    _seed_pending(shared, generator_id="cost_curator", problem="LLM-suggested")
    _seed_pending(shared, generator_id="security_warden", problem="LLM-suggested")

    with app.test_client() as c:
        # No filter — all three are visible (default behavior preserved).
        resp = c.get("/api/arbiter/proposals")
        assert resp.get_json()["count"] == 3

        # Exclude operator_ui — only the two LLM-generated remain.
        resp = c.get("/api/arbiter/proposals?exclude_generator_id=operator_ui")
        data = resp.get_json()
        assert data["count"] == 2
        gids = {p["generator_id"] for p in data["proposals"]}
        assert "operator_ui" not in gids
        assert gids == {"cost_curator", "security_warden"}


def test_exclude_and_include_generator_id_can_combine(arbiter_app):
    """When both generator_id and exclude_generator_id are set, both
    filters apply (intersection). Used in unusual cases where the operator
    explicitly asks for one generator's view; in practice the UI sends
    one or the other, not both."""
    app, shared = arbiter_app
    _seed_pending(shared, generator_id="operator_ui")
    _seed_pending(shared, generator_id="cost_curator")
    _seed_pending(shared, generator_id="security_warden")

    with app.test_client() as c:
        resp = c.get(
            "/api/arbiter/proposals"
            "?generator_id=cost_curator&exclude_generator_id=operator_ui"
        )
        data = resp.get_json()
        assert data["count"] == 1
        assert data["proposals"][0]["generator_id"] == "cost_curator"


def test_list_limit_caps_results_and_reports_total(arbiter_app):
    """The per-bot activity log fetches the most recent N operator-UI
    changes from a potentially long archive — ``?limit=N`` truncates
    post-sort, and ``total`` carries the pre-truncation count so the UI
    can show "showing 5 of 23 entries"."""
    app, shared = arbiter_app
    for i in range(5):
        _seed_pending(shared, generator_id="operator_ui", problem=f"op-{i}")

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?limit=2")
        data = resp.get_json()
        assert data["count"] == 2
        assert data["total"] == 5

        # limit=0 (omitted) returns everything
        resp = c.get("/api/arbiter/proposals")
        data = resp.get_json()
        assert data["count"] == 5
        assert data["total"] == 5


def test_list_excludes_snoozed_by_default(arbiter_app):
    app, shared = arbiter_app
    p_snoozed = make_investigation_proposal(audience="pod_operator")
    transition(p_snoozed, "pending", actor="t")
    transition(p_snoozed, "snoozed", actor="t", reason="defer")
    p_snoozed.snoozed_until = "2026-12-01T00:00:00+00:00"
    write_proposal(p_snoozed, shared)
    _seed_pending(shared)

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals")
        data = resp.get_json()
        # Default include was pending,snoozed
        assert data["count"] == 2
        resp2 = c.get("/api/arbiter/proposals?include=pending")
        assert resp2.get_json()["count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/proposals/<id>/dismiss | reject | snooze
# ─────────────────────────────────────────────────────────────────────────────


def test_dismiss_transitions_and_moves(arbiter_app):
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dismiss")
        assert resp.status_code == 200
        assert resp.get_json()["new_status"] == "dismissed"

        # Next list returns zero pending
        resp = c.get("/api/arbiter/proposals?include=pending")
        assert resp.get_json()["count"] == 0


def test_reject_endpoint_is_gone(arbiter_app):
    """The /reject endpoint was removed once the human path to rejection
    was collapsed onto /dismiss (the calibration loop that would have
    consumed the distinction was never wired). Pin that the route is
    no longer registered so a future regression doesn't quietly reintroduce
    a dead-surface mutation endpoint."""
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/reject")
        assert resp.status_code == 404


def test_snooze_with_duration_sets_until(arbiter_app):
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{p.id}/snooze",
            json={"duration": "2d"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["new_status"] == "snoozed"

        # Listing with include=snoozed surfaces it
        resp = c.get("/api/arbiter/proposals?include=snoozed")
        data = resp.get_json()
        assert data["count"] == 1
        assert data["proposals"][0]["snoozed_until"]


def test_snooze_without_body_defaults_to_one_week(arbiter_app):
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/snooze")
        assert resp.status_code == 200


def test_404_on_missing_proposal(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post("/api/arbiter/proposals/no-such-id/dismiss")
        assert resp.status_code == 404


def test_409_on_illegal_transition(arbiter_app):
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{p.id}/dismiss").get_json()
        # Second dismiss is illegal (already in terminal 'dismissed' — archived)
        resp = c.post(f"/api/arbiter/proposals/{p.id}/dismiss")
        assert resp.status_code == 409


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/proposals/bulk-action
# ─────────────────────────────────────────────────────────────────────────────


def test_bulk_dismiss_transitions_all(arbiter_app):
    app, shared = arbiter_app
    ps = [_seed_pending(shared, problem=f"p{i}") for i in range(3)]
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={"proposal_ids": [p.id for p in ps], "action": "dismiss"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["applied"] == 3
        assert data["failed"] == 0
        # All gone from pending
        resp = c.get("/api/arbiter/proposals?include=pending")
        assert resp.get_json()["count"] == 0


def test_bulk_dismiss_is_idempotent_on_already_terminal(arbiter_app):
    """Concurrent operators shouldn't see operator-confusing 409s. Per
    spec, already-archived proposals report ok=true with a skip reason."""
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        # First dismiss — succeeds.
        c.post(f"/api/arbiter/proposals/{p.id}/dismiss")
        # Bulk dismiss including the already-archived id — top-level ok.
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={"proposal_ids": [p.id], "action": "dismiss"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["applied"] == 1
        result = data["results"][0]
        assert result["ok"] is True
        assert "skipped" in result


def test_bulk_dismiss_missing_ids_treated_as_idempotent(arbiter_app):
    """A vanished id is most likely 'archived by another operator', not
    an error. Surface as ok with skipped='not found'."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={"proposal_ids": ["no-such-id"], "action": "dismiss"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["applied"] == 1


def test_bulk_snooze_sets_duration_on_each(arbiter_app):
    app, shared = arbiter_app
    ps = [_seed_pending(shared, problem=f"p{i}") for i in range(2)]
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={
                "proposal_ids": [p.id for p in ps],
                "action": "snooze",
                "duration": "2d",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["applied"] == 2
        # Each shows up in include=snoozed with snoozed_until set.
        resp = c.get("/api/arbiter/proposals?include=snoozed")
        data = resp.get_json()
        assert data["count"] == 2
        for prop in data["proposals"]:
            assert prop["snoozed_until"]


def test_bulk_snooze_defaults_to_one_week(arbiter_app):
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={"proposal_ids": [p.id], "action": "snooze"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["applied"] == 1


def test_bulk_action_rejects_unknown_action(arbiter_app):
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={"proposal_ids": [p.id], "action": "delete"},
        )
        assert resp.status_code == 400


def test_bulk_action_rejects_empty_list(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={"proposal_ids": [], "action": "dismiss"},
        )
        assert resp.status_code == 400


def test_bulk_action_dedupes_ids(arbiter_app):
    """Duplicate ids in the request collapse to one transition each —
    accidental double-clicks shouldn't inflate the failed-count."""
    app, shared = arbiter_app
    p = _seed_pending(shared)
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/bulk-action",
            json={"proposal_ids": [p.id, p.id, p.id], "action": "dismiss"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 1
        assert data["applied"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/arbiter/rate-limit-state
# ─────────────────────────────────────────────────────────────────────────────


def test_rate_limit_state_basic(arbiter_app):
    app, shared = arbiter_app
    for _ in range(3):
        _seed_pending(shared)
    with app.test_client() as c:
        resp = c.get("/api/arbiter/rate-limit-state")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["cap"] == 7
        assert data["surfaceable_now"] == 3


def test_rate_limit_state_held_when_over_cap(arbiter_app):
    app, shared = arbiter_app
    # 10 hygiene-level proposals (don't bypass the cap)
    for _ in range(10):
        _seed_pending(shared, urgency="hygiene")
    with app.test_client() as c:
        resp = c.get("/api/arbiter/rate-limit-state")
        data = resp.get_json()
        assert data["surfaceable_now"] == 7
        assert len(data["held"]) == 3


def test_rate_limit_critical_urgency_bypasses_cap(arbiter_app):
    app, shared = arbiter_app
    for _ in range(3):
        _seed_pending(shared, urgency="hygiene")
    for _ in range(10):
        _seed_pending(shared, urgency="security_critical")
    with app.test_client() as c:
        data = c.get("/api/arbiter/rate-limit-state").get_json()
        # All 10 critical + up to 7 hygiene → 10 + min(3, 7) = 13
        assert data["surfaceable_now"] >= 10


def test_rate_limit_returns_arrivals_and_decisions(arbiter_app):
    """Both metrics are returned: ``arrivals_this_week`` (newly
    promoted to pending — the cap's conceptual budget) and
    ``decisions_this_week`` (transitions out of pending — operator
    activity). Pre-fix these were conflated under a single
    ``surfaced_this_week`` field that counted decisions but was
    labeled as surfacings."""
    app, shared = arbiter_app
    # Seeding via transition(p, "pending", ...) creates a
    # draft → pending entry this ISO week, so arrivals==1 for each
    # seeded proposal. No decisions yet.
    _seed_pending(shared, urgency="hygiene")
    with app.test_client() as c:
        data = c.get("/api/arbiter/rate-limit-state").get_json()
        assert "arrivals_this_week" in data
        assert "decisions_this_week" in data
        assert data["arrivals_this_week"] == 1
        assert data["decisions_this_week"] == 0
        # Back-compat aliases retained for external callers.
        assert data["surfaced_this_week"] == data["arrivals_this_week"]
        assert data["surfaceable_now"] == data["ready_to_surface_now"]


def test_rate_limit_decisions_counts_transitions_out_of_pending(arbiter_app):
    """Make a decision (dismiss) and confirm it bumps
    ``decisions_this_week`` while leaving ``arrivals_this_week``
    unchanged — confirms the two metrics measure distinct things."""
    app, shared = arbiter_app
    p = _seed_pending(shared, urgency="hygiene")
    with app.test_client() as c:
        # Dismiss it — that's a pending → dismissed transition this
        # week, counting as a decision.
        c.post(f"/api/arbiter/proposals/{p.id}/dismiss")
        data = c.get("/api/arbiter/rate-limit-state").get_json()
        assert data["arrivals_this_week"] == 1
        assert data["decisions_this_week"] == 1
        # The proposal is no longer pending, so pending_now is 0.
        assert data["pending_now"] == 0


def test_rate_limit_surface_filter_pod_wide_by_default(arbiter_app):
    """No ``surface`` query param → response is pod-wide (every
    pending proposal contributes). The ``surface`` field on the
    response is ``None`` for this case so the caller can tell which
    view they got back."""
    app, shared = arbiter_app
    _seed_pending(shared, urgency="hygiene")
    _seed_pending(shared, urgency="hygiene")
    with app.test_client() as c:
        data = c.get("/api/arbiter/rate-limit-state").get_json()
        assert data["surface"] is None
        # Both proposals counted regardless of (unknown) charter
        # surface — registry won't resolve ``test_generator``, but
        # without a filter that doesn't matter.
        assert data["surfaceable_now"] == 2


def test_rate_limit_surface_filter_excludes_non_matching_proposals(arbiter_app):
    """A ``surface=improvement`` filter excludes proposals whose
    generator's charter surface is anything else (firing, drift,
    cleanup, or unclassified). This is the primary bugfix: pre-
    filter, the Inbox banner could claim "N ready to surface" while
    the Inbox list showed zero rows because the other surfaces
    routed to Alerts.

    Uses real generator IDs with known charter surfaces to exercise
    the real registry-lookup path (no monkeypatch). Charter surfaces
    were stable at the time of writing:
      - efficiency_hawk → improvement
      - bloat_investigator → firing
    """
    app, shared = arbiter_app
    # improvement-surface generator (matches filter)
    _seed_pending(shared, urgency="hygiene", generator_id="efficiency_hawk")
    # firing-surface generator (excluded by filter)
    _seed_pending(shared, urgency="hygiene", generator_id="bloat_investigator")
    _seed_pending(shared, urgency="hygiene", generator_id="bloat_investigator")

    with app.test_client() as c:
        data = c.get(
            "/api/arbiter/rate-limit-state?surface=improvement",
        ).get_json()
        assert data["surface"] == "improvement"
        # Only the one improvement-surface proposal counts.
        assert data["surfaceable_now"] == 1


def test_rate_limit_surface_filter_unknown_generator_excluded(arbiter_app):
    """Defensive: a proposal whose ``generator_id`` doesn't resolve to
    any registry entry (and therefore has no charter surface) is
    excluded by any non-empty surface filter. Without this rule, a
    typo'd generator_id could quietly leak into a surface-filtered
    view."""
    app, shared = arbiter_app
    # Unknown generator — registry won't have a charter for it.
    _seed_pending(shared, urgency="hygiene", generator_id="no_such_generator")

    with app.test_client() as c:
        # Filtered view excludes it.
        data = c.get(
            "/api/arbiter/rate-limit-state?surface=improvement",
        ).get_json()
        assert data["surfaceable_now"] == 0
        # Unfiltered view still includes it.
        data = c.get("/api/arbiter/rate-limit-state").get_json()
        assert data["surfaceable_now"] == 1


def test_rate_limit_surface_filter_empty_string_treated_as_no_filter(
    arbiter_app,
):
    """``?surface=`` (empty value) is equivalent to no param — same
    pod-wide response. Defensive: clients that forget to omit the
    param when the user clears it shouldn't get an empty-result
    surprise."""
    app, shared = arbiter_app
    _seed_pending(shared, urgency="hygiene")
    with app.test_client() as c:
        data = c.get("/api/arbiter/rate-limit-state?surface=").get_json()
        assert data["surface"] is None
        assert data["surfaceable_now"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/arbiter/profile/<bot>
# ─────────────────────────────────────────────────────────────────────────────


def test_profile_get_returns_no_content_when_missing(arbiter_app):
    """No profile on disk → has_content=False. The endpoint deliberately
    does not expose archetype, sections, or contents — those are user-private
    and not for the admin UI."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/profile/team_bot_a")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["bot_id"] == "team_bot_a"
        assert data["has_content"] is False
        # Regression: profile contents must not leak into admin UI.
        assert "sections" not in data
        assert "archetype" not in data
        assert "surfacing_cadence" not in data
        assert "updated_at" not in data
        assert "dimension_weights" not in data


def test_profile_get_has_content_false_for_empty_default_profile(arbiter_app):
    """A freshly-created profile with no populated sections still reports
    has_content=False — only sections with actual user data flip the flag."""
    app, shared = arbiter_app
    from profile.init_profile import create_default_profile  # type: ignore
    from profile import ARCHETYPE_PRIMARY  # type: ignore

    create_default_profile(
        shared_dir=shared, bot_id="team_bot_a", archetype=ARCHETYPE_PRIMARY
    )
    with app.test_client() as c:
        resp = c.get("/api/arbiter/profile/team_bot_a")
        data = resp.get_json()
        assert data["has_content"] is False


def test_profile_get_has_content_true_when_sections_populated(arbiter_app):
    app, shared = arbiter_app
    from profile.init_profile import create_default_profile  # type: ignore
    from profile.storage import load_profile, save_profile  # type: ignore

    create_default_profile(shared_dir=shared, bot_id="team_bot_a")
    profile = load_profile(shared, "team_bot_a")
    assert profile is not None
    profile.sections["Vocation"] = "Software engineer at a small startup."
    save_profile(profile, shared)

    with app.test_client() as c:
        resp = c.get("/api/arbiter/profile/team_bot_a")
        data = resp.get_json()
        assert data["has_content"] is True
        # Still must not leak contents.
        assert "sections" not in data


def test_profile_weight_endpoint_is_gone(arbiter_app):
    """The weight-edit endpoint was removed in the weights deletion pass."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/profile/team_bot_a/weight",
            json={"dimension": "utility", "new_weight": 1.5, "reason": "r"},
        )
        assert resp.status_code in (404, 405)


# ─────────────────────────────────────────────────────────────────────────────
# GET / POST /api/arbiter/bot-setup/<bot>
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_setup_get_returns_defaults_for_new_bot(arbiter_app):
    """A bot with no profile + no per-bot budget config returns clean
    defaults (None / None / pod default)."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/bot-setup/new_bot")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["bot_id"] == "new_bot"
        assert data["archetype"] is None
        assert data["surfacing_cadence"] is None
        assert data["monthly_cap_usd"] is None
        # Reference values for the UI to show defaults
        assert data["pod_monthly_cap_usd"] > 0
        assert "primary" in data["archetypes"]
        assert "as_it_arises" in data["cadences"]


def test_bot_setup_post_creates_profile_and_config(arbiter_app):
    app, shared = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={
                "archetype": "primary",
                "surfacing_cadence": "weekly",
                "monthly_cap_usd": 25.0,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["archetype"] == "primary"
        assert data["surfacing_cadence"] == "weekly"
        assert data["monthly_cap_usd"] == 25.0

        # Round-trip via GET
        resp = c.get("/api/arbiter/bot-setup/team_bot_a")
        data = resp.get_json()
        assert data["archetype"] == "primary"
        assert data["surfacing_cadence"] == "weekly"
        assert data["monthly_cap_usd"] == 25.0


def test_bot_setup_post_partial_update_preserves_other_fields(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        # Set all three
        c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={
                "archetype": "primary",
                "surfacing_cadence": "daily",
                "monthly_cap_usd": 30.0,
            },
        )
        # Update only the cadence
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"surfacing_cadence": "weekly"},
        )
        data = resp.get_json()
        assert data["archetype"] == "primary"  # preserved
        assert data["surfacing_cadence"] == "weekly"  # updated
        assert data["monthly_cap_usd"] == 30.0  # preserved


def test_bot_setup_post_clears_monthly_cap_with_null(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        c.post("/api/arbiter/bot-setup/team_bot_a", json={"monthly_cap_usd": 30.0})
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"monthly_cap_usd": None},
        )
        assert resp.status_code == 200
        assert resp.get_json()["monthly_cap_usd"] is None


def test_bot_setup_post_rejects_invalid_archetype(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"archetype": "ceo_bot"},
        )
        assert resp.status_code == 400


def test_bot_setup_post_rejects_invalid_cadence(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"surfacing_cadence": "fortnightly"},
        )
        assert resp.status_code == 400


def test_bot_setup_post_rejects_negative_cap(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"monthly_cap_usd": -10},
        )
        assert resp.status_code == 400


def test_bot_setup_post_rejects_zero_cap(arbiter_app):
    """Zero is meaningless as a spend cap — should reject."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"monthly_cap_usd": 0},
        )
        assert resp.status_code == 400


def test_bot_setup_post_rejects_non_numeric_cap(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"monthly_cap_usd": "lots"},
        )
        assert resp.status_code == 400


def test_bot_setup_get_returns_pod_timezone_reference(arbiter_app, tmp_path):
    """bot-setup GET surfaces the pod-wide timezone (from network.json) so
    the UI can show it as the default placeholder when no per-bot tz is set."""
    # Patch the network.json with a pod tz and rebuild the app
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve2"
    (shared_dir / "better-engine").mkdir(parents=True)
    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(shared_dir),
        "timezone": "America/Los_Angeles",
    }
    network_path = tmp_path / "network2.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/arbiter/bot-setup/team_bot_a")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pod_timezone"] == "America/Los_Angeles"
        assert data["timezone"] is None  # no per-bot override yet


def test_bot_setup_post_persists_valid_timezone(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": "Europe/London"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["timezone"] == "Europe/London"

        resp = c.get("/api/arbiter/bot-setup/team_bot_a")
        assert resp.get_json()["timezone"] == "Europe/London"


def test_bot_setup_post_clears_timezone_with_null(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": "America/New_York"},
        )
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": None},
        )
        assert resp.status_code == 200
        assert resp.get_json()["timezone"] is None


def test_bot_setup_post_clears_timezone_with_empty_string(arbiter_app):
    """An empty string from a cleared text input should also clear the override."""
    app, _ = arbiter_app
    with app.test_client() as c:
        c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": "America/New_York"},
        )
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": "   "},
        )
        assert resp.status_code == 200
        assert resp.get_json()["timezone"] is None


def test_bot_setup_post_rejects_invalid_timezone(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": "Mars/Olympus_Mons"},
        )
        assert resp.status_code == 400


def test_bot_setup_post_rejects_non_string_timezone(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": 7},
        )
        assert resp.status_code == 400


def test_bot_setup_post_partial_update_preserves_timezone(arbiter_app):
    """Setting timezone then updating only cadence should leave the timezone alone."""
    app, _ = arbiter_app
    with app.test_client() as c:
        c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"timezone": "Europe/London", "surfacing_cadence": "daily"},
        )
        resp = c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"surfacing_cadence": "weekly"},
        )
        data = resp.get_json()
        assert data["timezone"] == "Europe/London"  # preserved
        assert data["surfacing_cadence"] == "weekly"  # updated


# ─────────────────────────────────────────────────────────────────────────────
# Surfacing cadence affects the proposals listing for a specific bot
# ─────────────────────────────────────────────────────────────────────────────


def test_proposals_cadence_weekly_caps_to_one_non_urgent(arbiter_app):
    app, shared = arbiter_app
    # Set bot cadence to weekly
    with app.test_client() as c:
        c.post("/api/arbiter/bot-setup/team_bot_a", json={"surfacing_cadence": "weekly"})

    # Seed 5 improvement-level proposals for team_bot_a
    for _ in range(5):
        _seed_pending(shared, bot_id="team_bot_a", urgency="improvement")

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?bot_id=team_bot_a")
        data = resp.get_json()
        # Weekly cadence caps non-urgent to 1
        assert data["count"] == 1
        assert data["cadence_held_count"] == 4


def test_proposals_cadence_urgent_only_filters_to_critical(arbiter_app):
    app, shared = arbiter_app
    with app.test_client() as c:
        c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"surfacing_cadence": "urgent_only"},
        )
    _seed_pending(shared, bot_id="team_bot_a", urgency="improvement")
    _seed_pending(shared, bot_id="team_bot_a", urgency="hygiene")
    _seed_pending(shared, bot_id="team_bot_a", urgency="security_critical")
    _seed_pending(shared, bot_id="team_bot_a", urgency="operational_urgent")

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?bot_id=team_bot_a")
        data = resp.get_json()
        # Only the bypass-urgency items remain
        assert data["count"] == 2
        urgencies = {p["urgency"] for p in data["proposals"]}
        assert urgencies == {"security_critical", "operational_urgent"}
        assert data["cadence_held_count"] == 2


def test_proposals_cadence_urgent_only_keeps_critical_when_no_others(arbiter_app):
    """Urgent-only doesn't accidentally hide the critical items themselves."""
    app, shared = arbiter_app
    with app.test_client() as c:
        c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"surfacing_cadence": "urgent_only"},
        )
    _seed_pending(shared, bot_id="team_bot_a", urgency="security_critical")

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?bot_id=team_bot_a")
        data = resp.get_json()
        assert data["count"] == 1


def test_proposals_cadence_does_not_filter_pod_wide_listing(arbiter_app):
    """Without bot_id filter, cadence is ignored — operator sees everything."""
    app, shared = arbiter_app
    with app.test_client() as c:
        c.post("/api/arbiter/bot-setup/team_bot_a", json={"surfacing_cadence": "weekly"})
    for _ in range(5):
        _seed_pending(shared, bot_id="team_bot_a", urgency="improvement")

    with app.test_client() as c:
        # No bot_id — pod-wide listing
        resp = c.get("/api/arbiter/proposals")
        data = resp.get_json()
        assert data["count"] == 5  # cadence not applied
        assert data["cadence_held_count"] == 0


def test_proposals_cadence_as_it_arises_shows_everything(arbiter_app):
    app, shared = arbiter_app
    with app.test_client() as c:
        c.post(
            "/api/arbiter/bot-setup/team_bot_a",
            json={"surfacing_cadence": "as_it_arises"},
        )
    for _ in range(5):
        _seed_pending(shared, bot_id="team_bot_a", urgency="improvement")

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?bot_id=team_bot_a")
        data = resp.get_json()
        assert data["count"] == 5
        assert data["cadence_held_count"] == 0


def test_proposals_no_cadence_set_shows_everything(arbiter_app):
    """Default behavior (no cadence set) is the same as as_it_arises."""
    app, shared = arbiter_app
    for _ in range(5):
        _seed_pending(shared, bot_id="team_bot_a", urgency="improvement")

    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals?bot_id=team_bot_a")
        data = resp.get_json()
        assert data["count"] == 5
        assert data["cadence_held_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Generator endpoints
# ─────────────────────────────────────────────────────────────────────────────


def test_generators_list_returns_live_registry(arbiter_app):
    """The registry loads charters from the real generators code dir.
    Phase 2 ships 11 of them; every entry should have a dimension + status."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/generators")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["count"] >= 1
        ids = {g["id"] for g in data["generators"]}
        # sanity: a couple of known ones
        assert "sysadmin_watchdog" in ids
        assert "budget_hawk" in ids
        for g in data["generators"]:
            assert g["status"] in ("active", "paused", "quarantined")
            assert g["dimension"]
            assert g["type"] in ("optimizer", "guardian", "meta_guardian")
            assert "track_record" in g
            assert "authority" in g


def test_generator_detail_returns_charter_and_recent(arbiter_app):
    app, shared = arbiter_app
    # Seed a proposal from sysadmin_watchdog so recent_proposals isn't empty
    p = make_investigation_proposal(
        generator_id="sysadmin_watchdog",
        audience="pod_operator",
        problem="test",
    )
    transition(p, "pending", actor="test")
    write_proposal(p, shared)

    with app.test_client() as c:
        resp = c.get("/api/arbiter/generators/sysadmin_watchdog")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        g = data["generator"]
        assert g["id"] == "sysadmin_watchdog"
        assert g["type"] == "guardian"
        assert "invariants" in g
        # Recent proposals include the one we seeded
        ids = [rp["id"] for rp in g["recent_proposals"]]
        assert p.id in ids


def test_generator_detail_404_on_unknown(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/generators/no_such_gen")
        assert resp.status_code == 404


def test_generator_pause_then_resume(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/generators/sysadmin_watchdog/pause",
            json={"reason": "testing pause flow"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["status"] == "paused"

        # List should show it paused
        resp = c.get("/api/arbiter/generators")
        data = resp.get_json()
        watchdog = next(g for g in data["generators"] if g["id"] == "sysadmin_watchdog")
        assert watchdog["status"] == "paused"

        # Resume brings it back
        resp = c.post("/api/arbiter/generators/sysadmin_watchdog/resume")
        data = resp.get_json()
        assert data["status"] == "active"


def test_generator_pause_404_on_unknown(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post("/api/arbiter/generators/no_such_gen/pause", json={})
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot generator_config tunables (efficiency_hawk, gateway_diagnostician)
# ─────────────────────────────────────────────────────────────────────────────


def test_generator_config_writes_per_bot_override_for_gateway(arbiter_app):
    """POST /generators/<id>/config writes
    record.config["per_bot"][bot_id][field] and the GET detail reflects it."""
    app, shared = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/generators/gateway_diagnostician/config",
            json={
                "bot_id": "team_bot_a",
                "params": {"window_days": 21, "min_incidents": 9},
            },
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["ok"] is True

        # Persisted on disk under the per_bot section.
        record_path = shared / "generators" / "gateway_diagnostician.json"
        import json as _json

        record = _json.loads(record_path.read_text())
        assert record["config"]["per_bot"]["team_bot_a"] == {
            "window_days": 21,
            "min_incidents": 9,
        }

        # GET detail surfaces the override.
        detail = c.get("/api/arbiter/generators/gateway_diagnostician").get_json()
        bots = detail["generator"]["per_bot_tunables"]["bots"]
        row = bots["team_bot_a"]
        assert row["window_days"]["value"] == 21
        assert row["window_days"]["source"] == "override"
        assert row["window_days"]["effective"] == 21


def test_generator_config_nests_efficiency_hawk_cost_fields(arbiter_app):
    app, shared = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/generators/efficiency_hawk/config",
            json={
                "bot_id": "team_bot_a",
                "params": {"background_share_threshold": 0.55},
            },
        )
        assert resp.status_code == 200, resp.get_json()

        record_path = shared / "generators" / "efficiency_hawk.json"
        import json as _json

        record = _json.loads(record_path.read_text())
        assert record["config"]["per_bot"]["team_bot_a"] == {
            "cost": {"background_share_threshold": 0.55}
        }

        # The runner's resolution + factory must see the override on the
        # efficiency hawk context.
        from generator_runner import (
            _make_efficiency_hawk_ctx,
            _resolve_gen_config,
        )
        from datetime import datetime, timezone

        resolved = _resolve_gen_config(
            record["config"], "team_bot_a"
        )
        ctx = _make_efficiency_hawk_ctx(
            shared_dir=shared,
            network_config={"bots": {"team_bot_a": {"role": "member"}}},
            bot_id="team_bot_a",
            gen_config=resolved,
            now=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert ctx.cost_overrides == {"background_share_threshold": 0.55}


def test_generator_config_clears_override_on_null(arbiter_app):
    app, shared = arbiter_app
    with app.test_client() as c:
        c.post(
            "/api/arbiter/generators/gateway_diagnostician/config",
            json={"bot_id": "team_bot_a", "params": {"window_days": 21}},
        )
        resp = c.post(
            "/api/arbiter/generators/gateway_diagnostician/config",
            json={"bot_id": "team_bot_a", "params": {"window_days": None}},
        )
        assert resp.status_code == 200

        import json as _json

        record_path = shared / "generators" / "gateway_diagnostician.json"
        record = _json.loads(record_path.read_text())
        # No leftover empty per_bot section.
        assert "per_bot" not in record["config"]


def test_generator_config_rejects_unknown_param(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/generators/gateway_diagnostician/config",
            json={"bot_id": "team_bot_a", "params": {"not_a_field": 1}},
        )
        assert resp.status_code == 400


def test_generator_config_404_on_unknown_generator(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/generators/no_such_gen/config",
            json={"bot_id": "team_bot_a", "params": {"window_days": 21}},
        )
        # The route guard short-circuits on "no per-bot tunables" for
        # generators not in _GENERATOR_TUNABLE_PARAMS.
        assert resp.status_code in (400, 404)


def test_per_bot_tunables_returns_dataclass_default_when_unset(arbiter_app):
    """With no record overrides anywhere, the GET detail must surface the
    dataclass default as the pod_default and as each bot's effective
    value — otherwise the UI shows a misleading 0.00."""
    app, _ = arbiter_app
    with app.test_client() as c:
        detail = c.get("/api/arbiter/generators/gateway_diagnostician").get_json()
        tunables = detail["generator"]["per_bot_tunables"]
        # GatewayDiagnosticianContext.window_days default = 7
        assert tunables["pod_defaults"]["window_days"] == 7
        bots = tunables["bots"]
        # Both seeded bots from the fixture.
        for bot_id in ("team_bot_a", "ellie"):
            row = bots[bot_id]
            assert row["window_days"]["value"] is None
            assert row["window_days"]["source"] == "default"
            assert row["window_days"]["effective"] == 7


def test_per_bot_tunables_flat_config_without_per_bot_section(arbiter_app):
    """A pre-existing flat record.config (operator wrote a pod-wide value
    with no per_bot section) must be surfaced as the effective value for
    every bot. This is the back-compat path."""
    app, shared = arbiter_app
    # Force-load the registry to materialize gateway_diagnostician's
    # record on disk, then write a flat override.
    from evolve_admin.web.server import _import_analyzer
    reg_mod = _import_analyzer("registry.registry")
    registry = reg_mod.Registry(
        generators_code_dir=Path(__file__).parent.parent.parent / "analyzer" / "generators",
        records_dir=shared / "generators",
    )
    registry.load_all(strict=False)
    registry.update_config("gateway_diagnostician", {"window_days": 30})

    with app.test_client() as c:
        detail = c.get("/api/arbiter/generators/gateway_diagnostician").get_json()
        tunables = detail["generator"]["per_bot_tunables"]
        # Pod default reflects the flat override.
        assert tunables["pod_defaults"]["window_days"] == 30
        for bot_id in ("team_bot_a", "ellie"):
            row = tunables["bots"][bot_id]
            # No per-bot override → value is None, but effective inherits the flat.
            assert row["window_days"]["value"] is None
            assert row["window_days"]["effective"] == 30
            assert row["window_days"]["source"] == "default"


def test_generator_config_rejects_budget_only_generator(arbiter_app):
    """budget_hawk's tunables use write_path=bot_setup, not generator_config —
    so this endpoint refuses to write them."""
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/generators/budget_hawk/config",
            json={"bot_id": "team_bot_a", "params": {"daily_warn_usd": 1.0}},
        )
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Score breakdown on proposals endpoint
# ─────────────────────────────────────────────────────────────────────────────


def test_proposals_list_includes_score_breakdown(arbiter_app):
    app, shared = arbiter_app
    _seed_pending(shared, urgency="operational_urgent")
    with app.test_client() as c:
        resp = c.get("/api/arbiter/proposals")
        data = resp.get_json()
        assert data["count"] == 1
        p = data["proposals"][0]
        assert "_score_breakdown" in p
        bd = p["_score_breakdown"]
        assert bd is not None
        assert bd["urgency"] == 700  # operational_urgent in URGENCY_SCORE
        assert "authority" in bd
        assert "tiebreak" in bd
        assert "score" in bd
        # Regression: dimension_weight is no longer a scoring factor
        assert "dimension_weight" not in bd
        assert "_rank" in p
        assert p["_rank"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Watchdog events
# ─────────────────────────────────────────────────────────────────────────────


def _seed_watchdog_event(shared_dir, **kwargs):
    from datetime import datetime as _dt2, timezone as _tz2

    from generators.evolve_watchdog.events import write_events  # type: ignore
    from schema.watchdog import WatchdogEvent, new_watchdog_event_id  # type: ignore

    ev = WatchdogEvent(
        id=kwargs.get("id") or new_watchdog_event_id(),
        bot_id=kwargs.get("bot_id"),
        timestamp=kwargs.get("timestamp") or _dt2.now(_tz2.utc).isoformat(
            timespec="seconds"
        ),
        event_type=kwargs.get("event_type", "proposal_volume_deviation"),
        severity=kwargs.get("severity", "warn"),
        details=kwargs.get("details") or {"generator_id": "sysadmin_watchdog", "ratio": 3.0},
    )
    write_events([ev], shared_dir=shared_dir)
    return ev


def test_watchdog_events_empty_window(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/health/watchdog-events")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["count"] == 0
        # Descriptions are always included so the UI can tooltip event types.
        # 8 meta-layer types + 2 operational (gateway_instability,
        # config_drift_unexplained from heal). test_failure_pattern was
        # retired 2026-06-08 with the rest of the app-test surface.
        descriptions = data["event_type_descriptions"]
        assert "gateway_instability" in descriptions
        assert "config_drift_unexplained" in descriptions


def test_watchdog_events_list_and_filter(arbiter_app):
    app, shared = arbiter_app
    _seed_watchdog_event(shared, event_type="proposal_volume_deviation", severity="warn")
    _seed_watchdog_event(shared, event_type="auto_revert_rate_spike", severity="alert")
    _seed_watchdog_event(shared, event_type="calibration_drift", severity="info")
    with app.test_client() as c:
        resp = c.get("/api/arbiter/health/watchdog-events")
        assert resp.get_json()["count"] == 3

        resp = c.get("/api/arbiter/health/watchdog-events?severity=alert")
        data = resp.get_json()
        assert data["count"] == 1
        assert data["events"][0]["event_type"] == "auto_revert_rate_spike"

        resp = c.get("/api/arbiter/health/watchdog-events?event_type=calibration_drift")
        data = resp.get_json()
        assert data["count"] == 1
        assert data["events"][0]["severity"] == "info"


def test_watchdog_events_invalid_since(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/health/watchdog-events?since=not-a-date")
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Observation browser
# ─────────────────────────────────────────────────────────────────────────────


def _seed_observation_tuples(shared_dir, bot_id="team_bot_a", items=None):
    from datetime import datetime as _dt2, timezone as _tz2

    from observations.tuples import write_tuples  # type: ignore
    from schema.observation import ObservationTuple, new_tuple_id  # type: ignore

    now = _dt2.now(_tz2.utc)
    items = items or [
        ("fitness", "tracking", "enthusiastic", 5),
        ("fitness", "tracking", "frustrated", 2),
        ("email", "drafting", "neutral", 3),
        ("code", "reviewing", "neutral", 8),
    ]
    tuples = [
        ObservationTuple(
            id=new_tuple_id(),
            bot_id=bot_id,
            session_id=f"s{i}",
            segment_id="seg",
            noun=noun,
            verb=verb,
            mood=mood,
            engagement=eng,
            timestamp_start=now.isoformat(timespec="seconds"),
            timestamp_end=now.isoformat(timespec="seconds"),
            source_hash=f"h{i}",
        )
        for i, (noun, verb, mood, eng) in enumerate(items)
    ]
    write_tuples(tuples, shared_dir=shared_dir, bot_id=bot_id, day=now)


def test_observations_requires_bot_id(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/observations")
        assert resp.status_code == 400


def test_observations_summary_and_filters(arbiter_app):
    app, shared = arbiter_app
    _seed_observation_tuples(shared, bot_id="team_bot_a")

    with app.test_client() as c:
        resp = c.get("/api/arbiter/observations?bot_id=team_bot_a")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["matched"] == 4
        assert data["total_in_window"] == 4
        assert data["engagement_total"] == 5 + 2 + 3 + 8
        # Top verbs includes tracking (appears twice)
        verb_counts = dict(data["top_verbs"])
        assert verb_counts["tracking"] == 2

        # Filter by noun
        resp = c.get("/api/arbiter/observations?bot_id=team_bot_a&noun=fitness")
        data = resp.get_json()
        assert data["matched"] == 2

        # Filter by mood
        resp = c.get("/api/arbiter/observations?bot_id=team_bot_a&mood=frustrated")
        data = resp.get_json()
        assert data["matched"] == 1
        assert data["sample"][0]["mood"] == "frustrated"

        # Filter by verb with no matches
        resp = c.get("/api/arbiter/observations?bot_id=team_bot_a&verb=celebrating")
        data = resp.get_json()
        assert data["matched"] == 0


def test_observations_empty_when_no_data(arbiter_app):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.get("/api/arbiter/observations?bot_id=team_bot_a")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["matched"] == 0
        assert data["total_in_window"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/proposals/<id>/act — AdoptModel parameterized approval
# (spec-model-rungs-and-roles-2026-06-09 §Addendum A)
# ─────────────────────────────────────────────────────────────────────────────


def _seed_adopt_model(shared_dir: Path):
    """Seed a pending AdoptModel proposal (the model_discovery shape)."""
    from schema.proposal import (
        Proposal, Provenance, RiskTag, AdoptModel, new_proposal_id,
    )

    p = Proposal(
        id=new_proposal_id(),
        bot_id="<pod>",
        generator_id="model_discovery",
        dimension="substrate_health",
        trigger_observations=["model_discovery:anthropic:claude-fable-5"],
        provenance=Provenance(technique="model_discovery.listing_diff", confidence=0.9),
        problem="anthropic/claude-fable-5 is available but in no rung.",
        action=AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", position=2, cost_class="premium",
        ),
        risk_tag=RiskTag(blast_radius="pod", reversibility="manual", touches=["models.rungs"]),
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary="New model: anthropic/claude-fable-5",
    )
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, shared_dir)
    return p


def test_act_adopt_model_with_max_role_and_cap(arbiter_app):
    """POST /act with {role: max, cap: 1} patches the action and the applier
    writes the rung + role + cap to network.json::models."""
    app, shared = arbiter_app
    p = _seed_adopt_model(shared)

    # Redirect the applier's network IO to an in-memory store so the test
    # doesn't touch the real pod network.json.
    from arbiter.appliers.adopt_model import set_network_io
    store = {"net": {"models": {"rungs": [
        {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
        {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
    ], "roles": {"fast": "haiku-class", "standard": "sonnet-class"}}}}

    def _read():
        import copy
        return copy.deepcopy(store["net"])

    def _write(net):
        import copy
        store["net"] = copy.deepcopy(net)

    set_network_io(_read, _write)
    try:
        with app.test_client() as c:
            resp = c.post(
                f"/api/arbiter/proposals/{p.id}/act",
                json={"role": "max", "cap": 1},
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            assert resp.get_json()["ok"] is True

        models = store["net"]["models"]
        assert [r["id"] for r in models["rungs"]] == ["haiku-class", "sonnet-class", "fable-class"]
        assert models["roles"]["max"] == "fable-class"
        assert models["roleCaps"]["max"]["maxPerDayPerBot"] == 1
    finally:
        set_network_io(None, None)


def test_act_adopt_model_dormant_default_no_role(arbiter_app):
    """POST /act with no body (or role=none) adopts as a dormant rung — no
    role mapped, no cap. max is never armed by default."""
    app, shared = arbiter_app
    p = _seed_adopt_model(shared)

    from arbiter.appliers.adopt_model import set_network_io
    store = {"net": {"models": {"rungs": [], "roles": {}}}}
    set_network_io(
        lambda: __import__("copy").deepcopy(store["net"]),
        lambda net: store.update(net=__import__("copy").deepcopy(net)),
    )
    try:
        with app.test_client() as c:
            resp = c.post(f"/api/arbiter/proposals/{p.id}/act", json={"role": "none"})
            assert resp.status_code == 200, resp.get_data(as_text=True)
            assert resp.get_json()["ok"] is True
        models = store["net"]["models"]
        assert [r["id"] for r in models["rungs"]] == ["fable-class"]
        assert not models.get("roles")
        assert "roleCaps" not in models
    finally:
        set_network_io(None, None)


def test_act_adopt_model_rejects_invalid_role(arbiter_app):
    """A bad role in the body is rejected 400 before any apply."""
    app, shared = arbiter_app
    p = _seed_adopt_model(shared)
    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/act", json={"role": "bogus"})
        assert resp.status_code == 400
        assert "invalid role" in resp.get_json()["error"]
    # Proposal stays pending (no transition on a rejected payload).
    from arbiter.store import find_proposal
    located = find_proposal(shared, p.id)
    assert located is not None and located[0].status == "pending"


def test_act_adopt_model_rejects_bad_cap(arbiter_app):
    app, shared = arbiter_app
    p = _seed_adopt_model(shared)
    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{p.id}/act", json={"role": "max", "cap": 0})
        assert resp.status_code == 400
        assert "cap must be >= 1" in resp.get_json()["error"]


# ─────────────────────────────────────────────────────────────────────────────
# _import_analyzer: stdlib `profile` shadow regression
# ─────────────────────────────────────────────────────────────────────────────


def test_import_analyzer_promotes_analyzer_dir_over_stdlib_profile():
    """Regression for the pod 500 on every /api/arbiter/bot-setup read+write.

    On Python 3.14 the stdlib pulls in ``profile`` (the profiler — a single
    ``.py`` module, NOT a package), and the editable install of
    evolve-analyzer leaves analyzer_dir on ``sys.path`` but *behind* the
    stdlib. ``import_module("profile.query")`` then resolved ``profile`` to
    the stdlib module and died with "'profile' is not a package", which the
    cost-cap UI surfaced as dashed tiles ("—") and an inert Custom field.

    ``_import_analyzer`` must force analyzer_dir to the FRONT of ``sys.path``
    (not merely ensure it is present) so the analyzer ``profile`` package wins
    the name race. This test reproduces the pod state — a cached non-package
    ``profile`` plus a demoted analyzer_dir — and asserts recovery.
    """
    import sys
    import types

    from evolve_admin.web import routes_arbiter

    analyzer_dir = str(routes_arbiter._ANALYZER_DIR)
    saved_path = list(sys.path)
    saved_mods = {
        k: sys.modules[k]
        for k in list(sys.modules)
        if k == "profile" or k.startswith("profile.")
    }
    try:
        # (a) a stdlib-style single-file ``profile`` (no __path__) is cached
        for k in list(sys.modules):
            if k == "profile" or k.startswith("profile."):
                del sys.modules[k]
        shadow = types.ModuleType("profile")  # no __path__ → "not a package"
        shadow.__file__ = "<stdlib>/profile.py"
        sys.modules["profile"] = shadow
        # (b) analyzer_dir is present but NOT first (the conftest normally
        #     prepends it, which is why no existing test caught this).
        sys.path[:] = [p for p in sys.path if p != analyzer_dir] + [analyzer_dir]
        assert sys.path[0] != analyzer_dir, "precondition: analyzer_dir demoted"

        mod = routes_arbiter._import_analyzer("profile.query")

        assert mod.__name__ == "profile.query"
        # The analyzer package (a real package with __path__) replaced the shadow.
        assert hasattr(sys.modules["profile"], "__path__")
        # analyzer_dir was promoted to the front — the actual fix.
        assert sys.path[0] == analyzer_dir
    finally:
        sys.path[:] = saved_path
        for k in list(sys.modules):
            if k == "profile" or k.startswith("profile."):
                del sys.modules[k]
        sys.modules.update(saved_mods)
