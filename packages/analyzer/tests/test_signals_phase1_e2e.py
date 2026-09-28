"""tests/test_signals_phase1_e2e.py — Phase 1 exit-gate smoke test.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 1 exit gate).

Asserts the full pipeline end-to-end:

  watchdog observe()  →  WatchdogEvent in JSONL
                      →  Signal in /api/signals
                      →  Proposal carries motivating_signals
                      →  user snoozes via POST /api/signals/<id>/snooze
                      →  Signal stays snoozed across reloads
                      →  user dismisses via POST /api/signals/<id>/dismiss
                      →  Dismissed Signal is NOT re-opened by next observe()
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import pytest  # noqa: E402

from generators.evolve_watchdog import events as wd_events  # noqa: E402
from schema.watchdog import WatchdogEvent, new_watchdog_event_id  # noqa: E402
from signals import store as signals_store  # noqa: E402


@pytest.fixture
def admin_client(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(
        json.dumps({"sharedDir": str(shared), "bots": []}), encoding="utf-8"
    )
    from evolve_admin.web.server import create_app
    app = create_app(network)
    return app.test_client(), shared


def _seed_watchdog_event(shared: Path, **overrides) -> WatchdogEvent:
    base = dict(
        id=new_watchdog_event_id(),
        bot_id=None,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        event_type="proposal_volume_deviation",
        severity="warn",
        details={"ratio": 4.0},
    )
    base.update(overrides)
    event = WatchdogEvent(**base)  # type: ignore[arg-type]
    wd_events.write_events([event], shared_dir=shared)
    return event


def test_phase1_exit_gate(admin_client):
    """End-to-end walk through Phase 1's exit gate.

    1. Watchdog event lands → Signal exists in /api/signals.
    2. Snooze persists across reloads.
    3. Dismissed signal is NOT re-opened by repeated watchdog observation.
    """
    client, shared = admin_client

    # ── 1. Watchdog event → Signal in API ──────────────────────────────────
    _seed_watchdog_event(shared)

    resp = client.get("/api/signals?producer=evolve_watchdog")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    sig = data["signals"][0]
    assert sig["type"] == "proposal_volume_deviation"
    assert sig["flavor"] == "activity"
    assert sig["state"] == "firing"
    sig_id = sig["id"]

    # ── 2. Snooze through the API; verify persists across "reload" ────────
    resp = client.post(f"/api/signals/{sig_id}/snooze", json={"duration": "24h"})
    assert resp.status_code == 200
    assert resp.get_json()["new_state"] == "snoozed"

    # New client = new request = simulates reload
    resp = client.get(f"/api/signals/{sig_id}")
    assert resp.status_code == 200
    assert resp.get_json()["signal"]["state"] == "snoozed"
    assert resp.get_json()["signal"]["snoozed_until"] is not None

    # ── 3. Dismiss the signal; verify it's not resurfaced by next observe ──
    resp = client.post(f"/api/signals/{sig_id}/dismiss", json={})
    assert resp.status_code == 200
    assert resp.get_json()["new_state"] == "dismissed"

    # The watchdog runs again — same condition, same signature
    _seed_watchdog_event(shared)

    # The dismissed signal stays dismissed. The recurring observation
    # bumps the dismissed entry's last_observed_at in place — it does
    # NOT mint a fresh firing sibling. (Earlier this test asserted the
    # opposite — that a fresh sibling appeared — which inadvertently
    # pinned the bug where dismissing acted as a notifier reset.
    # Operator-facing fix landed 2026-06-01 after the Evolve Security
    # Audit dismiss → re-page screenshot.)
    located = signals_store.find_signal(shared, sig_id)
    assert located is not None
    sig_after, _, subdir = located
    assert sig_after.state == "dismissed"
    assert subdir == "archived"
    # observation_count reflects the recurrence even though no firing
    # entry materialised.
    assert sig_after.observation_count >= 2

    # No active Signal — the dispatcher has nothing to page on.
    actives = list(signals_store.iter_active(shared))
    assert actives == []


def test_phase1_proposal_motivating_signals_round_trips_through_api(admin_client):
    """When watchdog emits a proposal, its motivating_signals[] array
    points at signals that the bidirectional API can resolve back."""
    from arbiter import store as arbiter_store
    from arbiter.state_machine import transition
    from generators.evolve_watchdog.observe import (
        EvolveWatchdogContext,
        _build_investigation,
    )

    client, shared = admin_client

    # Seed an event AND its dual-written signal
    e = _seed_watchdog_event(
        shared,
        event_type="calibration_drift",
        details={"drift": 0.42},
    )

    # Build the proposal the way observe() would
    ctx = EvolveWatchdogContext(
        bot_id=None,
        shared_dir=shared,
        history_reader=lambda gid, n: {},
        dominance_reader=lambda: {},
        pod_stats_reader=lambda: {},
        active_generator_ids=[],
        now=datetime.now(timezone.utc),
    )
    proposal = _build_investigation(e, ctx)
    assert proposal.motivating_signals
    sig_id = proposal.motivating_signals[0]

    # Persist the proposal
    transition(proposal, "pending", actor="test")
    arbiter_store.write_proposal(proposal, shared)

    # Forward edge: signal → proposals
    resp = client.get(f"/api/signals/{sig_id}/proposals")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    assert data["proposals"][0]["id"] == proposal.id

    # Reverse edge: proposal carries the link in its serialized form
    resp = client.get("/api/arbiter/proposals?include=pending")
    assert resp.status_code == 200
    proposals = resp.get_json().get("proposals", [])
    matching = [p for p in proposals if p["id"] == proposal.id]
    assert matching
    assert sig_id in matching[0]["motivating_signals"]


def test_phase1_old_endpoint_still_works_with_deprecation_header(admin_client):
    """The old /api/arbiter/health/watchdog-events keeps reading JSONL
    so non-migrated callers don't break, but signals the migration via
    the Deprecation header."""
    client, shared = admin_client
    _seed_watchdog_event(shared, event_type="gateway_instability",
                         severity="alert", bot_id="admin_bot",
                         details={"flap_count": 3})

    resp = client.get("/api/arbiter/health/watchdog-events?since=2026-01-01")
    assert resp.status_code == 200
    assert resp.headers.get("Deprecation") == "true"
    assert "successor-version" in (resp.headers.get("Link") or "")
    data = resp.get_json()
    assert data["count"] >= 1
    assert "_deprecated" in data


def test_phase1_backfill_idempotent_with_live_writes(admin_client):
    """If an operator runs backfill after the dual-write has started,
    the backfill skips signatures that already have an active signal."""
    from signals import backfill

    client, shared = admin_client

    # Live event → active signal
    _seed_watchdog_event(shared, event_type="meta_layer_cost_spike")

    # Pretend a historical JSONL exists with the same signature
    historical_day = (datetime.now(timezone.utc) - timedelta(days=10))
    p = shared / "watchdog" / f"{historical_day.date().isoformat()}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    historical_event = {
        "id": "old-1",
        "bot_id": None,
        "timestamp": historical_day.isoformat(timespec="seconds"),
        "event_type": "meta_layer_cost_spike",
        "severity": "warn",
        "details": {"ratio": 1.6},
    }
    # Append (the live write may have already created today's file)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(historical_event) + "\n")

    result = backfill.backfill_watchdog_events(shared)
    # Live signal kept the signature occupied
    assert result.skipped_existing == 1
    assert result.created == 0

    # Active signal still firing, single signal in active state
    actives = list(signals_store.iter_active(shared))
    assert len(actives) == 1
    assert actives[0].state == "firing"
