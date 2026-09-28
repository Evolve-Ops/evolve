"""tests/test_signals_list_endpoint.py — GET /api/signals.

Pins the truncation-disclosure contract added in response to the
2026-05-26 bulk-dismiss bug: the Reports → Alerts page was fetching
``?flavor=maintenance&limit=200`` against a pod with 239 active
maintenance signals, silently dropping 39. The client-side producer
chip filter then operated only on the truncated 200, so
bulk-dismissing "all visible compliance_scan" left the hidden tail
firing and the operator concluded the action failed.

The list endpoint now returns both ``count`` (returned after slice)
and ``total`` (matching before slice) plus ``limit``, so the UI can
render a "showing N of M" banner whenever the cap is hit.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _seed_signal(shared_dir: Path, sig_id: str, *,
                 producer: str = "compliance_scan",
                 sig_type: str = "missing_required_field",
                 bot_id: str | None = "team_bot_a",
                 severity: str = "alert") -> str:
    from schema.signal import Signal, StateTransition

    sig = Signal(
        id=sig_id,
        signature=f"{producer}:{sig_type}:{bot_id or 'pod'}::{sig_id}",
        producer=producer,
        type=sig_type,
        flavor="maintenance",
        severity=severity,
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=f"test {sig_type} on {bot_id or 'pod'}",
        body="seed",
    )
    sig.state_history.append(
        StateTransition(
            from_state=None,
            to_state="firing",
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor="test",
            reason="seed",
        )
    )
    from signals import store as signals_store
    signals_store.write_signal(sig, shared_dir, subdir="firing")
    return sig.id


@pytest.fixture
def app(tmp_path):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (shared / "signals" / sub).mkdir(parents=True)

    network = {"members": [], "bots": {}, "sharedDir": str(shared)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    app.config["_test_shared"] = str(shared)
    return app


def test_response_includes_total_count_and_limit(app):
    shared = Path(app.config["_test_shared"])
    for i in range(5):
        _seed_signal(shared, f"sig_{i}")

    with app.test_client() as c:
        resp = c.get("/api/signals?flavor=maintenance&limit=200")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["count"] == 5
    assert data["total"] == 5
    assert data["limit"] == 200
    assert len(data["signals"]) == 5


def test_total_exceeds_count_when_limit_truncates(app):
    """``total`` reflects matches before slice; ``count`` after.

    This is the load-bearing field for the truncation banner.
    """
    shared = Path(app.config["_test_shared"])
    for i in range(12):
        _seed_signal(shared, f"sig_{i:02d}")

    with app.test_client() as c:
        resp = c.get("/api/signals?flavor=maintenance&limit=5")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 5
    assert data["total"] == 12
    assert data["limit"] == 5
    assert len(data["signals"]) == 5


def test_total_respects_producer_filter(app):
    """``total`` counts only signals matching the query filters.

    Compliance-scan signals coexist with audit signals; a
    ?producer=compliance_scan request must not include audit signals
    in the total, otherwise the banner over-reports.
    """
    shared = Path(app.config["_test_shared"])
    for i in range(8):
        _seed_signal(shared, f"comp_{i}", producer="compliance_scan")
    for i in range(3):
        _seed_signal(shared, f"aud_{i}", producer="audit_config",
                     sig_type="stale_baseline", severity="warn")

    with app.test_client() as c:
        resp = c.get("/api/signals?producer=compliance_scan&limit=200")
    data = resp.get_json()
    assert data["count"] == 8
    assert data["total"] == 8
    assert all(s["producer"] == "compliance_scan" for s in data["signals"])


def test_empty_signals_dir_returns_zero_total(app):
    with app.test_client() as c:
        resp = c.get("/api/signals?flavor=maintenance&limit=200")
    data = resp.get_json()
    assert data["count"] == 0
    assert data["total"] == 0
    assert data["signals"] == []


# ── 2026-06-04: hydrate motivated_proposals_view ────────────────────────────


def _seed_proposal(shared_dir: Path, prop_id: str, *,
                   motivating_signals: list[str] | None = None,
                   problem: str = "test proposal",
                   status: str = "pending"):
    """Seed a Proposal so signal-hydration has a target. Proposal has
    no literal title field; admin_surface_summary or problem are what
    server-side hydration surfaces as ``title`` in the API view."""
    from arbiter.store import write_proposal
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal(bot_id="team_bot_a", problem=problem)
    p.id = prop_id
    p.status = status
    p.motivating_signals = list(motivating_signals or [])
    write_proposal(p, shared_dir)
    return p


def test_signal_view_hydrates_motivated_proposals(app):
    """When a Signal has motivated_proposals[<id>] and that Proposal
    exists on disk, the API response must include
    motivated_proposals_view with the proposal's title + status +
    kind. This is what lets the Alerts UI render the paired-row Act
    button without an extra fetch per signal."""
    shared = Path(app.config["_test_shared"])

    # Seed: one signal + one proposal that motivates it. The proposal
    # write path (arbiter.store.write_proposal) maintains the
    # Signal.motivated_proposals backref automatically.
    sid = "sig_with_proposal"
    _seed_signal(shared, sid)
    _seed_proposal(shared, "prop_a", motivating_signals=[sid],
                   problem="Add missing caps to cron 'X'", status="pending")

    with app.test_client() as c:
        resp = c.get("/api/signals?flavor=maintenance&limit=200")
    data = resp.get_json()
    [view] = [s for s in data["signals"] if s["id"] == sid]
    assert "motivated_proposals_view" in view, (
        "Signal view must include the hydrated motivated_proposals_view "
        "so the Alerts UI can render the paired-row Act button"
    )
    [pview] = view["motivated_proposals_view"]
    assert pview["id"] == "prop_a"
    assert pview["title"] == "Add missing caps to cron 'X'"
    assert pview["status"] == "pending"


def test_signal_view_hydration_handles_missing_proposal(app):
    """If a Signal's motivated_proposals references a proposal that's
    been deleted (e.g. manual cleanup, or a bug), the hydration must
    NOT crash the signal-list endpoint. The view emits a stub entry
    with status='missing' so the UI can render it as 'archived link'
    rather than dropping the row entirely."""
    shared = Path(app.config["_test_shared"])
    sid = "sig_dangling_link"
    _seed_signal(shared, sid)
    # Manually attach a backref to a proposal id that doesn't exist
    from signals import store as signals_store
    located = signals_store.find_signal(shared, sid)
    assert located is not None
    sig, _path, _subdir = located
    sig.motivated_proposals.append("ghost_proposal_id")
    signals_store.write_signal(sig, shared, subdir="firing")

    with app.test_client() as c:
        resp = c.get("/api/signals?flavor=maintenance&limit=200")
    assert resp.status_code == 200, (
        "Endpoint must NOT 500 on dangling proposal references"
    )
    data = resp.get_json()
    [view] = [s for s in data["signals"] if s["id"] == sid]
    hydrated = view.get("motivated_proposals_view") or []
    assert len(hydrated) == 1
    assert hydrated[0]["id"] == "ghost_proposal_id"
    assert hydrated[0]["status"] == "missing"


def test_signal_view_omits_hydration_when_no_proposals(app):
    """A Signal with empty motivated_proposals shouldn't get an empty
    motivated_proposals_view field — keep the wire payload tight."""
    shared = Path(app.config["_test_shared"])
    _seed_signal(shared, "lonely_sig")
    with app.test_client() as c:
        resp = c.get("/api/signals?flavor=maintenance&limit=200")
    data = resp.get_json()
    [view] = [s for s in data["signals"] if s["id"] == "lonely_sig"]
    assert "motivated_proposals_view" not in view, (
        "Don't bloat the payload with an empty hydration array — the "
        "absence of the field is the 'nothing linked' signal"
    )
