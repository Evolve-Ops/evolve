"""tests/test_signals_phase1_watchdog.py — Phase 1 watchdog dual-write.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 1).

Covers:
  - events.write_events dual-writes to Signal store (per-event_type
    flavor + producer mapping)
  - Repeated emission dedups by signature (same condition over multiple
    days collapses into one rolling Signal)
  - signal_id_for_event() locates the mirroring Signal
  - watchdog proposals carry motivating_signals[] populated
  - /api/signals/<id>/snooze, /dismiss, /resolve mutate state correctly
    and persist across reloads
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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _evt(
    event_type: str,
    *,
    severity: str = "warn",
    bot_id: str | None = None,
    details: dict | None = None,
) -> WatchdogEvent:
    return WatchdogEvent(
        id=new_watchdog_event_id(),
        bot_id=bot_id,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        event_type=event_type,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        details=details or {},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dual-write: every WatchdogEvent → Signal
# ─────────────────────────────────────────────────────────────────────────────


def test_write_events_dual_writes_signal(tmp_path):
    event = _evt("proposal_volume_deviation", severity="warn",
                 details={"ratio": 4.0})
    wd_events.write_events([event], shared_dir=tmp_path)

    signals = list(signals_store.iter_active(tmp_path))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.producer == "evolve_watchdog"
    assert sig.type == "proposal_volume_deviation"
    assert sig.flavor == "activity"
    assert sig.severity == "warn"
    assert sig.scope == "pod"
    assert sig.bot_id is None
    assert sig.details["ratio"] == 4.0


def test_write_events_dedups_by_signature(tmp_path):
    """Same event_type + scope on multiple days = one rolling Signal."""
    e1 = _evt("calibration_drift", severity="warn", details={"drift": 0.32})
    wd_events.write_events([e1], shared_dir=tmp_path)
    e2 = _evt("calibration_drift", severity="alert", details={"drift": 0.55})
    wd_events.write_events([e2], shared_dir=tmp_path)

    signals = list(signals_store.iter_active(tmp_path))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.observation_count == 2
    # Producer can escalate severity by re-observing
    assert sig.severity == "alert"
    # Latest details merged
    assert sig.details["drift"] == 0.55


def test_write_events_distinct_event_types_create_distinct_signals(tmp_path):
    e1 = _evt("calibration_drift")
    e2 = _evt("meta_layer_cost_spike")
    wd_events.write_events([e1, e2], shared_dir=tmp_path)

    signals = list(signals_store.iter_active(tmp_path))
    assert len(signals) == 2
    types = {s.type for s in signals}
    assert types == {"calibration_drift", "meta_layer_cost_spike"}


def test_write_events_routes_maintenance_flavor_for_gateway(tmp_path):
    """Sysadmin watchdog event types land in Maintenance lane."""
    e = _evt("gateway_instability", severity="alert", bot_id="admin_bot",
             details={"flap_count": 7})
    wd_events.write_events([e], shared_dir=tmp_path)

    signals = list(signals_store.iter_active(tmp_path))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.flavor == "maintenance"
    assert sig.producer == "sysadmin_watchdog"
    assert sig.scope == "bot"
    assert sig.bot_id == "admin_bot"


def test_write_events_unmapped_event_type_skips_signal(tmp_path, monkeypatch):
    """An event_type not in the flavor map writes JSONL but no Signal —
    fail-soft so a new event type doesn't crash the writer."""
    e = _evt("calibration_drift", severity="warn")
    # Pretend this event_type isn't classified
    monkeypatch.setitem(wd_events._SIGNAL_FLAVOR_BY_EVENT_TYPE, "calibration_drift", None)
    wd_events.write_events([e], shared_dir=tmp_path)
    # JSONL still written
    files = list((tmp_path / "watchdog").glob("*.jsonl"))
    assert len(files) == 1
    # No signal
    assert list(signals_store.iter_active(tmp_path)) == []


def test_write_events_empty_list_is_noop(tmp_path):
    wd_events.write_events([], shared_dir=tmp_path)
    assert list(signals_store.iter_active(tmp_path)) == []
    assert not (tmp_path / "watchdog").exists()


# ─────────────────────────────────────────────────────────────────────────────
# signal_id_for_event lookup
# ─────────────────────────────────────────────────────────────────────────────


def test_signal_id_for_event_returns_id_after_dual_write(tmp_path):
    e = _evt("verification_reliability_drop", severity="warn",
             details={"generator_id": "team_bot_a_extender"})
    wd_events.write_events([e], shared_dir=tmp_path)

    sig_id = wd_events.signal_id_for_event(e, tmp_path)
    assert sig_id is not None

    located = signals_store.find_signal(tmp_path, sig_id)
    assert located is not None
    assert located[0].type == "verification_reliability_drop"


def test_signal_id_for_event_returns_none_when_no_signal(tmp_path):
    """Lookup before write returns None (no exception)."""
    e = _evt("calibration_drift")
    assert wd_events.signal_id_for_event(e, tmp_path) is None


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog proposals carry motivating_signals[]
# ─────────────────────────────────────────────────────────────────────────────


def test_watchdog_investigation_proposal_links_to_signal(tmp_path):
    """End-to-end: dual-write happens, then proposal is built, and the
    proposal's motivating_signals[] points at the freshly-created Signal."""
    from generators.evolve_watchdog.observe import (
        EvolveWatchdogContext,
        _build_investigation,
    )

    ctx = EvolveWatchdogContext(
        bot_id=None,
        shared_dir=tmp_path,
        history_reader=lambda gid, n: {},
        dominance_reader=lambda: {},
        pod_stats_reader=lambda: {},
        active_generator_ids=[],
        now=datetime.now(timezone.utc),
    )
    e = _evt("calibration_drift", severity="warn", details={"drift": 0.4})

    # The dual-write must happen before the proposal builder runs
    wd_events.write_events([e], shared_dir=tmp_path)

    proposal = _build_investigation(e, ctx)
    assert len(proposal.motivating_signals) == 1

    sig_id = proposal.motivating_signals[0]
    located = signals_store.find_signal(tmp_path, sig_id)
    assert located is not None
    assert located[0].type == "calibration_drift"


def test_watchdog_throttle_proposal_aggregates_motivating_signals(tmp_path):
    """A throttle proposal triggered by multiple events should reference
    each motivating Signal (deduped by id)."""
    from generators.evolve_watchdog.observe import (
        EvolveWatchdogContext,
        _build_throttle,
    )

    ctx = EvolveWatchdogContext(
        bot_id=None,
        shared_dir=tmp_path,
        history_reader=lambda gid, n: {},
        dominance_reader=lambda: {},
        pod_stats_reader=lambda: {},
        active_generator_ids=[],
        now=datetime.now(timezone.utc),
    )
    # Two distinct event types about the same generator
    e1 = _evt(
        "auto_revert_rate_spike",
        severity="alert",
        details={"generator_id": "team_bot_a_extender", "delta": 0.3},
    )
    e2 = _evt(
        "verification_reliability_drop",
        severity="alert",
        details={"generator_id": "team_bot_a_extender", "drop": 0.2},
    )
    wd_events.write_events([e1, e2], shared_dir=tmp_path)

    proposal = _build_throttle(
        "team_bot_a_extender",
        ctx,
        throttle_type="reduce_cadence",
        new_value="weekly",
        reason="reliability drop",
        related_events=[e1, e2],
    )
    assert len(proposal.motivating_signals) == 2
    # Each links to a distinct Signal in the store
    for sig_id in proposal.motivating_signals:
        located = signals_store.find_signal(tmp_path, sig_id)
        assert located is not None


# ─────────────────────────────────────────────────────────────────────────────
# Mutation endpoints: snooze / dismiss / resolve
# ─────────────────────────────────────────────────────────────────────────────


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


def _seed_signal(shared, **overrides):
    base = dict(
        signature="evolve_watchdog:proposal_volume_deviation:pod",
        producer="evolve_watchdog",
        type="proposal_volume_deviation",
        flavor="activity",
        severity="warn",
        scope="pod",
        bot_id=None,
        title="Volume deviation",
    )
    base.update(overrides)
    return signals_store.observe(shared, **base)


def test_snooze_endpoint_with_duration(admin_client):
    client, shared = admin_client
    sig = _seed_signal(shared)

    resp = client.post(f"/api/signals/{sig.id}/snooze", json={"duration": "24h"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["new_state"] == "snoozed"
    assert data["signal"]["snoozed_until"] is not None

    # Persists across reloads (read it back from disk)
    located = signals_store.find_signal(shared, sig.id)
    assert located is not None
    assert located[0].state == "snoozed"


def test_snooze_endpoint_with_explicit_until(admin_client):
    client, shared = admin_client
    sig = _seed_signal(shared)
    until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")

    resp = client.post(f"/api/signals/{sig.id}/snooze", json={"until": until})
    assert resp.status_code == 200
    assert resp.get_json()["signal"]["snoozed_until"] == until


def test_snooze_endpoint_default_24h(admin_client):
    client, shared = admin_client
    sig = _seed_signal(shared)
    resp = client.post(f"/api/signals/{sig.id}/snooze", json={})
    assert resp.status_code == 200
    until = resp.get_json()["signal"]["snoozed_until"]
    assert until is not None


def test_snooze_endpoint_rejects_invalid_duration(admin_client):
    client, shared = admin_client
    sig = _seed_signal(shared)
    resp = client.post(f"/api/signals/{sig.id}/snooze", json={"duration": "junk"})
    assert resp.status_code == 400


def test_dismiss_endpoint_terminates_signal(admin_client):
    client, shared = admin_client
    sig = _seed_signal(shared)

    resp = client.post(f"/api/signals/{sig.id}/dismiss", json={})
    assert resp.status_code == 200
    assert resp.get_json()["new_state"] == "dismissed"

    located = signals_store.find_signal(shared, sig.id)
    assert located is not None
    assert located[0].state == "dismissed"


def test_dismiss_endpoint_does_not_resurface_via_observe(admin_client):
    """Dismissed signals must not resurface — neither re-opened nor
    cloned. That's the load-bearing UX promise of dismiss.

    Earlier this test asserted ``new_sig.id != sig.id`` to confirm
    dismissed Signals were not re-opened. That assertion accidentally
    pinned the inverse bug: each new observe() of the same signature
    minted a fresh firing sibling that signal_notifier paged on, so
    dismissing actually re-paged the operator. Fixed 2026-06-01 by
    making observe() bump the dismissed entry in place when no active/
    recently-resolved Signal with the signature exists.
    """
    client, shared = admin_client
    sig = _seed_signal(shared)
    client.post(f"/api/signals/{sig.id}/dismiss", json={})

    # Re-observe with same signature — should bump the dismissed entry
    # in place, NOT create a new firing sibling.
    new_sig = _seed_signal(shared)
    assert new_sig.id == sig.id, (
        "re-observing must reuse the dismissed Signal; a new id would "
        "cause signal_notifier to re-page the operator"
    )
    assert new_sig.state == "dismissed", "dismissed must stay dismissed"
    firing_path = signals_store.signal_path(shared, new_sig.id, subdir="firing")
    assert not firing_path.exists(), (
        "no firing entry should materialise for a dismissed signature"
    )


def test_dismiss_endpoint_with_verdict_writes_feedback(admin_client):
    """User flags signal as bad → feedback.jsonl gets an entry."""
    client, shared = admin_client
    sig = _seed_signal(shared)

    resp = client.post(
        f"/api/signals/{sig.id}/dismiss",
        json={"verdict": "false_positive", "note": "expected on Mondays"},
    )
    assert resp.status_code == 200

    log = signals_store.feedback_log_path(shared)
    assert log.exists()
    rec = json.loads(log.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert rec["signal_id"] == sig.id
    assert rec["verdict"] == "false_positive"
    assert rec["note"] == "expected on Mondays"


def test_resolve_endpoint_marks_signal_resolved(admin_client):
    client, shared = admin_client
    sig = _seed_signal(shared)

    resp = client.post(f"/api/signals/{sig.id}/resolve", json={})
    assert resp.status_code == 200
    assert resp.get_json()["new_state"] == "resolved"


def test_mutation_404_for_unknown_signal(admin_client):
    client, _ = admin_client
    for path in ["snooze", "dismiss", "resolve"]:
        resp = client.post(f"/api/signals/does-not-exist/{path}", json={})
        assert resp.status_code == 404


def test_snooze_then_dismiss_is_legal(admin_client):
    client, shared = admin_client
    sig = _seed_signal(shared)
    client.post(f"/api/signals/{sig.id}/snooze", json={"duration": "1d"})
    resp = client.post(f"/api/signals/{sig.id}/dismiss", json={})
    assert resp.status_code == 200
    assert resp.get_json()["new_state"] == "dismissed"


def test_dismiss_then_snooze_is_409(admin_client):
    """Dismissed is terminal — no transitions out."""
    client, shared = admin_client
    sig = _seed_signal(shared)
    client.post(f"/api/signals/{sig.id}/dismiss", json={})
    resp = client.post(f"/api/signals/{sig.id}/snooze", json={"duration": "1d"})
    assert resp.status_code == 409
