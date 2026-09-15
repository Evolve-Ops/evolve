"""tests/test_signals_store.py — Phase 0 Signal store tests.

Spec: internal/spec-alerts-signal-store-2026-05-07.md.

Covers:
  - schema.signal: round-trip, validation
  - signals.state_machine: legal/illegal transitions, history append
  - signals.store: observe (find-or-create + re-open), iter_active,
    apply_transition, sweep_resolve, wake_due_snoozes, feedback log
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from schema.signal import (  # noqa: E402
    Signal,
    StateTransition,
    make_signature,
    new_signal_id,
)
from signals import store as signals_store  # noqa: E402
from signals.state_machine import (  # noqa: E402
    IllegalTransitionError,
    allowed_transitions,
    is_terminal,
    transition,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_signal(
    *,
    signature: str = "test:cost_spike:admin_bot",
    producer: str = "test",
    type: str = "cost_spike",
    flavor: str = "activity",
    severity: str = "warn",
    scope: str = "bot",
    bot_id: str | None = "admin_bot",
    title: str = "Test signal",
    body: str = "",
    details: dict | None = None,
) -> Signal:
    sig = Signal(
        id=new_signal_id(),
        signature=signature,
        producer=producer,
        type=type,
        flavor=flavor,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        bot_id=bot_id,
        title=title,
        body=body,
        details=details or {},
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
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Schema round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_signal_round_trip_preserves_all_fields():
    sig = _make_signal(
        details={"current": 4.20, "baseline": 1.05},
        body="cost rose 4x baseline",
    )
    sig.observation_count = 3
    sig.motivated_proposals = ["prop-1", "prop-2"]

    raw = sig.to_dict()
    revived = Signal.from_dict(raw)

    assert revived.id == sig.id
    assert revived.signature == sig.signature
    assert revived.producer == sig.producer
    assert revived.flavor == sig.flavor
    assert revived.severity == sig.severity
    assert revived.scope == sig.scope
    assert revived.bot_id == sig.bot_id
    assert revived.title == sig.title
    assert revived.body == sig.body
    assert revived.details == sig.details
    assert revived.observation_count == 3
    assert revived.motivated_proposals == ["prop-1", "prop-2"]
    assert len(revived.state_history) == len(sig.state_history)


def test_signal_validation_rejects_bot_scope_without_bot_id():
    with pytest.raises(ValueError):
        Signal(
            id="x",
            signature="x:y:z",
            producer="x",
            type="y",
            flavor="activity",
            severity="info",
            scope="bot",
            bot_id=None,
        )


def test_signal_validation_rejects_empty_required_fields():
    with pytest.raises(ValueError):
        Signal(
            id="",
            signature="x",
            producer="x",
            type="x",
            flavor="activity",
            severity="info",
            scope="pod",
        )
    with pytest.raises(ValueError):
        Signal(
            id="x",
            signature="",
            producer="x",
            type="x",
            flavor="activity",
            severity="info",
            scope="pod",
        )


def test_make_signature_is_canonical():
    assert make_signature("pod_report", "cost_spike", "admin_bot") == (
        "pod_report:cost_spike:admin_bot"
    )


# ─────────────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────────────


def test_legal_transitions():
    assert "snoozed" in allowed_transitions("firing")
    assert "resolved" in allowed_transitions("firing")
    assert "dismissed" in allowed_transitions("firing")
    assert "firing" in allowed_transitions("snoozed")
    assert "resolved" in allowed_transitions("snoozed")
    assert is_terminal("resolved")
    assert is_terminal("dismissed")
    assert not is_terminal("firing")
    assert not is_terminal("snoozed")


def test_illegal_transition_raises():
    sig = _make_signal()
    transition(sig, "dismissed", actor="user:pod_admin")
    # dismissed is truly terminal — no transitions out
    with pytest.raises(IllegalTransitionError):
        transition(sig, "firing", actor="test")
    with pytest.raises(IllegalTransitionError):
        transition(sig, "resolved", actor="test")


def test_transition_appends_history_and_sets_resolved_at():
    sig = _make_signal()
    history_before = len(sig.state_history)
    transition(sig, "resolved", actor="user:pod_admin", reason="false alarm")
    assert sig.state == "resolved"
    assert sig.resolved_at is not None
    assert len(sig.state_history) == history_before + 1
    assert sig.state_history[-1].from_state == "firing"
    assert sig.state_history[-1].to_state == "resolved"
    assert sig.state_history[-1].actor == "user:pod_admin"
    assert sig.state_history[-1].reason == "false alarm"


def test_snoozed_to_firing_clears_snoozed_until():
    sig = _make_signal()
    sig.snoozed_until = "2026-12-01T00:00:00+00:00"
    transition(sig, "snoozed", actor="user:pod_admin")
    transition(sig, "firing", actor="timer", reason="snooze expired")
    assert sig.snoozed_until is None
    assert sig.state == "firing"


# ─────────────────────────────────────────────────────────────────────────────
# Store: write / read
# ─────────────────────────────────────────────────────────────────────────────


def test_store_write_routes_by_state(tmp_path):
    sig = _make_signal()
    path = signals_store.write_signal(sig, tmp_path)
    assert path == signals_store.signal_path(tmp_path, sig.id, subdir="firing")
    assert path.exists()

    loaded = signals_store.load_signal_file(path)
    assert loaded is not None
    assert loaded.id == sig.id
    assert loaded.state == "firing"


def test_store_find_signal_across_subdirs(tmp_path):
    sig = _make_signal()
    signals_store.write_signal(sig, tmp_path)
    found = signals_store.find_signal(tmp_path, sig.id)
    assert found is not None
    found_sig, _, subdir = found
    assert found_sig.id == sig.id
    assert subdir == "firing"


# ─────────────────────────────────────────────────────────────────────────────
# Store: observe — find-or-create
# ─────────────────────────────────────────────────────────────────────────────


def _observe_kwargs(**overrides):
    base = dict(
        signature="pod_report:cost_spike:admin_bot",
        producer="pod_report",
        type="cost_spike",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="Cost spike on admin_bot",
    )
    base.update(overrides)
    return base


def test_observe_creates_new_signal_when_none_exists(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    assert sig.state == "firing"
    assert sig.observation_count == 1
    assert sig.signature == "pod_report:cost_spike:admin_bot"
    assert len(sig.state_history) == 1
    assert sig.state_history[0].from_state is None
    assert sig.state_history[0].to_state == "firing"
    # Lives in firing/
    assert signals_store.signal_path(tmp_path, sig.id, subdir="firing").exists()


def test_observe_dedups_to_existing_active_signal(tmp_path):
    first = signals_store.observe(
        tmp_path,
        **_observe_kwargs(details={"current": 4.0, "baseline": 1.0}),
    )
    second = signals_store.observe(
        tmp_path,
        **_observe_kwargs(details={"current": 5.0, "baseline": 1.0}),
    )

    assert first.id == second.id
    assert second.observation_count == 2
    # Merged details: latest values win
    assert second.details["current"] == 5.0
    assert second.details["baseline"] == 1.0


def test_observe_escalates_severity_on_re_observe(tmp_path):
    signals_store.observe(tmp_path, **_observe_kwargs(severity="warn"))
    bumped = signals_store.observe(tmp_path, **_observe_kwargs(severity="alert"))
    assert bumped.severity == "alert"


def test_observe_distinct_signatures_create_distinct_signals(tmp_path):
    a = signals_store.observe(
        tmp_path, **_observe_kwargs(signature="pod_report:cost_spike:admin_bot")
    )
    b = signals_store.observe(
        tmp_path, **_observe_kwargs(signature="pod_report:cost_spike:team_bot_b")
    )
    assert a.id != b.id


def test_observe_reopens_recently_resolved(tmp_path):
    # Create + resolve
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    signals_store.apply_transition(
        sig, "resolved", tmp_path, actor="producer", reason="cleared"
    )
    # File should be in archived/
    assert signals_store.signal_path(tmp_path, sig.id, subdir="archived").exists()

    # Re-observe within window
    re = signals_store.observe(tmp_path, **_observe_kwargs())

    assert re.id == sig.id
    assert re.state == "firing"
    # Snoozed_until should still be None; archived copy should be gone
    assert not signals_store.signal_path(
        tmp_path, sig.id, subdir="archived"
    ).exists()
    assert signals_store.signal_path(
        tmp_path, sig.id, subdir="firing"
    ).exists()
    # State history should now have: created, resolved, reopened
    states = [(h.from_state, h.to_state) for h in re.state_history]
    assert states[-1] == ("resolved", "firing")


def test_observe_does_not_reopen_dismissed(tmp_path):
    """Dismissing a Signal is a permanent "don't tell me again" — re-
    observing the same signature must NOT create a fresh firing
    sibling. (Pre-2026-06 the create-fresh path made dismiss work like
    a reset: dismiss → next monitor tick → new Signal id → notifier
    fires again, the exact opposite of what the operator asked for.)

    The contract now: re-observing a dismissed signature bumps
    ``last_observed_at`` and ``observation_count`` on the dismissed
    entry in place. No new Signal id, no firing/<id>.json, no notifier
    fire. The operator regains visibility only by explicitly re-
    opening the dismissed Signal from the Alerts UI.
    """
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    original_id = sig.id
    signals_store.apply_transition(
        sig, "dismissed", tmp_path, actor="user:pod_admin", reason="not actionable"
    )

    # Re-observe — should bump the dismissed entry, NOT create a new one
    new = signals_store.observe(tmp_path, **_observe_kwargs())

    # Same Signal id — the dismissed entry was bumped in place
    assert new.id == original_id, (
        "re-observing a dismissed signature must reuse the existing "
        "dismissed Signal, not create a fresh firing sibling"
    )
    assert new.state == "dismissed", (
        "dismissed must stay dismissed — observe() must not flip it back to firing"
    )
    assert new.observation_count == sig.observation_count + 1, (
        "observation_count should reflect that the condition is still occurring"
    )

    # No firing/<id>.json should exist — the dispatcher would have paged on it
    firing_path = signals_store.signal_path(tmp_path, new.id, subdir="firing")
    assert not firing_path.exists(), (
        "no firing entry should materialise for a dismissed signature; "
        "that would re-page the operator who said 'don't tell me again'"
    )

    # Dismissed Signal still in archived/, contents updated
    archived = signals_store.signal_path(tmp_path, original_id, subdir="archived")
    assert archived.exists()


def test_observe_with_zero_reopen_window_skips_reopen(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    signals_store.apply_transition(sig, "resolved", tmp_path, actor="test")

    new = signals_store.observe(
        tmp_path, **_observe_kwargs(), reopen_window_seconds=0
    )
    assert new.id != sig.id


# ─────────────────────────────────────────────────────────────────────────────
# Store: iter_active with filters
# ─────────────────────────────────────────────────────────────────────────────


def test_iter_active_filters_by_scope_and_bot(tmp_path):
    signals_store.observe(
        tmp_path, **_observe_kwargs(signature="a:b:admin_bot", bot_id="admin_bot")
    )
    signals_store.observe(
        tmp_path, **_observe_kwargs(signature="a:b:team_bot_b", bot_id="team_bot_b")
    )
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="x:y:pod", scope="pod", bot_id=None
        ),
    )

    admin_bot_only = list(
        signals_store.iter_active(tmp_path, scope="bot", bot_id="admin_bot")
    )
    assert len(admin_bot_only) == 1
    assert admin_bot_only[0].bot_id == "admin_bot"

    pod_only = list(signals_store.iter_active(tmp_path, scope="pod"))
    assert len(pod_only) == 1
    assert pod_only[0].bot_id is None


def test_iter_active_filters_by_flavor_and_severity(tmp_path):
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="m:1:admin_bot", flavor="maintenance", severity="alert"
        ),
    )
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="a:1:admin_bot", flavor="activity", severity="warn"
        ),
    )

    maintenance = list(
        signals_store.iter_active(tmp_path, flavor="maintenance")
    )
    assert len(maintenance) == 1
    assert maintenance[0].flavor == "maintenance"

    alerts = list(signals_store.iter_active(tmp_path, severity="alert"))
    assert len(alerts) == 1
    assert alerts[0].severity == "alert"


def test_iter_active_excludes_archived(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    signals_store.apply_transition(sig, "resolved", tmp_path, actor="test")
    assert list(signals_store.iter_active(tmp_path)) == []


def test_iter_active_filters_by_category(tmp_path):
    """The Alerts page tabs into top-level domain buckets. Filter exposes
    the same routing the schema's per-producer mapping uses, so 33
    producers don't all live in one flat list."""
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="cost_watchdog:spike:team_bot_c",
            producer="cost_watchdog",
            type="spike",
            bot_id="team_bot_c",
        ),
    )
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="security_warden:cred:security_bot",
            producer="security_warden",
            type="credential_exposure",
            bot_id="security_bot",
        ),
    )
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="backup_signal:no_pat:team_bot_b",
            producer="backup_signal",
            type="no_pat",
            bot_id="team_bot_b",
        ),
    )

    cost = list(signals_store.iter_active(tmp_path, category="cost"))
    assert [s.producer for s in cost] == ["cost_watchdog"]

    security = list(signals_store.iter_active(tmp_path, category="security"))
    assert [s.producer for s in security] == ["security_warden"]

    backup = list(signals_store.iter_active(tmp_path, category="backup"))
    assert [s.producer for s in backup] == ["backup_signal"]

    # platform is the catch-all; pod_report from the helper default lives there
    platform = list(signals_store.iter_active(tmp_path, category="platform"))
    assert {s.producer for s in platform} == set()  # no platform signals emitted in this test


def test_observe_threads_explicit_category_override(tmp_path):
    """A producer can override the per-producer default at emit time, so
    an audit finding that's really security (FileVault off, EMAIL_POLICY
    world-writable) lands under the Security tab rather than under
    audit's default 'platform' bucket."""
    sig = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="audit:filevault:host",
            producer="audit",
            type="filevault_off",
            scope="host",
            bot_id=None,
            category="security",
        ),
    )
    assert sig.category == "security"
    # Persists through the store roundtrip.
    [reloaded] = list(signals_store.iter_active(tmp_path, category="security"))
    assert reloaded.id == sig.id


# ─────────────────────────────────────────────────────────────────────────────
# Store: apply_transition file moves
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_transition_moves_file_between_subdirs(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    firing_path = signals_store.signal_path(tmp_path, sig.id, subdir="firing")
    assert firing_path.exists()

    signals_store.apply_transition(
        sig, "resolved", tmp_path, actor="producer", reason="cleared"
    )

    assert not firing_path.exists()
    assert signals_store.signal_path(
        tmp_path, sig.id, subdir="archived"
    ).exists()


def test_apply_transition_snooze_requires_snoozed_until(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    with pytest.raises(ValueError):
        signals_store.apply_transition(
            sig, "snoozed", tmp_path, actor="user:pod_admin"
        )


def test_apply_transition_snooze_sets_field_and_moves_file(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    until = (
        datetime.now(timezone.utc) + timedelta(hours=24)
    ).isoformat(timespec="seconds")
    signals_store.apply_transition(
        sig,
        "snoozed",
        tmp_path,
        actor="user:pod_admin",
        reason="check tomorrow",
        snoozed_until=until,
    )

    assert sig.snoozed_until == until
    assert signals_store.signal_path(
        tmp_path, sig.id, subdir="snoozed"
    ).exists()
    assert not signals_store.signal_path(
        tmp_path, sig.id, subdir="firing"
    ).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Store: state-change log (spec §7)
# ─────────────────────────────────────────────────────────────────────────────


def _read_state_change_log(tmp_path) -> list[dict]:
    """Read today's state-change log file as a list of records (or empty)."""
    path = signals_store.state_change_log_path(tmp_path)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_state_change_log_records_creation(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())

    records = _read_state_change_log(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["signal_id"] == sig.id
    assert rec["from_state"] is None
    assert rec["to_state"] == "firing"
    assert rec["producer"] == "pod_report"
    assert rec["signature"] == "pod_report:cost_spike:admin_bot"
    assert rec["bot_id"] == "admin_bot"
    assert rec["actor"] == "pod_report"
    assert rec["reason"] == "created"


def test_state_change_log_records_transition(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    signals_store.apply_transition(
        sig, "resolved", tmp_path, actor="producer", reason="cleared"
    )

    records = _read_state_change_log(tmp_path)
    # create + resolve
    assert len(records) == 2
    assert records[0]["to_state"] == "firing"
    assert records[1]["from_state"] == "firing"
    assert records[1]["to_state"] == "resolved"
    assert records[1]["reason"] == "cleared"


def test_state_change_log_records_reopen(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    signals_store.apply_transition(sig, "resolved", tmp_path, actor="producer")
    signals_store.observe(tmp_path, **_observe_kwargs())  # re-observe → reopen

    records = _read_state_change_log(tmp_path)
    # create + resolve + reopen
    assert len(records) == 3
    assert records[2]["from_state"] == "resolved"
    assert records[2]["to_state"] == "firing"
    assert records[2]["reason"] == "reopen within window"


def test_state_change_log_creates_dir_if_missing(tmp_path):
    # Confirm the log dir does NOT exist before observe()
    assert not (tmp_path / "signals" / "log").exists()
    signals_store.observe(tmp_path, **_observe_kwargs())
    # observe() must have created it
    assert (tmp_path / "signals" / "log").is_dir()
    assert signals_store.state_change_log_path(tmp_path).exists()


def test_state_change_log_sweep_resolve_records_each(tmp_path):
    a = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="pod_report:cost_spike:admin_bot", producer="pod_report"
        ),
    )
    b = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="pod_report:cost_spike:team_bot_b", producer="pod_report"
        ),
    )
    # Sweep keeps neither → both resolve.
    signals_store.sweep_resolve(
        tmp_path, producer="pod_report", kept_signatures=set()
    )

    records = _read_state_change_log(tmp_path)
    # 2 creates + 2 resolves
    assert len(records) == 4
    resolves = [r for r in records if r["to_state"] == "resolved"]
    assert len(resolves) == 2
    assert {r["signal_id"] for r in resolves} == {a.id, b.id}


# ─────────────────────────────────────────────────────────────────────────────
# Store: sweep_resolve
# ─────────────────────────────────────────────────────────────────────────────


def test_sweep_resolve_clears_unkept_signals_for_producer(tmp_path):
    a = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="pod_report:cost_spike:admin_bot", producer="pod_report"
        ),
    )
    b = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="pod_report:cost_spike:team_bot_b", producer="pod_report"
        ),
    )
    other = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="audit:critical:pod",
            producer="audit",
            scope="pod",
            bot_id=None,
        ),
    )

    resolved = signals_store.sweep_resolve(
        tmp_path,
        producer="pod_report",
        kept_signatures={a.signature},
    )

    resolved_ids = {s.id for s in resolved}
    assert resolved_ids == {b.id}

    # a still firing, b resolved, other untouched
    a_after = signals_store.find_signal(tmp_path, a.id)
    b_after = signals_store.find_signal(tmp_path, b.id)
    other_after = signals_store.find_signal(tmp_path, other.id)
    assert a_after is not None and a_after[0].state == "firing"
    assert b_after is not None and b_after[0].state == "resolved"
    assert other_after is not None and other_after[0].state == "firing"


def test_sweep_resolve_types_filter_only_touches_listed_types(tmp_path):
    """A partial-coverage runner that re-checks only one type must not
    resolve Signals of other types it did not re-check."""
    gate_team_bot_a = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="pod_health:pod_health_gateways:team_bot_a:gateway",
            producer="pod_health",
            type="pod_health_gateways",
        ),
    )
    gate_team_bot_b = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="pod_health:pod_health_gateways:team_bot_b:gateway",
            producer="pod_health",
            type="pod_health_gateways",
            bot_id="team_bot_b",
        ),
    )
    launchd_team_bot_a = signals_store.observe(
        tmp_path,
        **_observe_kwargs(
            signature="pod_health:pod_health_launchd:team_bot_a:plist",
            producer="pod_health",
            type="pod_health_launchd",
            bot_id="team_bot_a",
        ),
    )

    # Liveness-only sweep: keeps gate_team_bot_a firing, scoped to gateways type only.
    resolved = signals_store.sweep_resolve(
        tmp_path,
        producer="pod_health",
        kept_signatures={gate_team_bot_a.signature},
        types={"pod_health_gateways"},
    )

    resolved_ids = {s.id for s in resolved}
    # gate_team_bot_b resolved (in scope, not kept). launchd_team_bot_a untouched (out of scope).
    assert resolved_ids == {gate_team_bot_b.id}

    gate_team_bot_a_after = signals_store.find_signal(tmp_path, gate_team_bot_a.id)
    gate_team_bot_b_after = signals_store.find_signal(tmp_path, gate_team_bot_b.id)
    launchd_team_bot_a_after = signals_store.find_signal(tmp_path, launchd_team_bot_a.id)
    assert gate_team_bot_a_after is not None and gate_team_bot_a_after[0].state == "firing"
    assert gate_team_bot_b_after is not None and gate_team_bot_b_after[0].state == "resolved"
    assert launchd_team_bot_a_after is not None and launchd_team_bot_a_after[0].state == "firing"


def test_sweep_resolve_includes_snoozed(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    until = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat(timespec="seconds")
    signals_store.apply_transition(
        sig, "snoozed", tmp_path, actor="user", snoozed_until=until
    )

    resolved = signals_store.sweep_resolve(
        tmp_path, producer="pod_report", kept_signatures=set()
    )
    assert len(resolved) == 1
    assert resolved[0].id == sig.id


# ─────────────────────────────────────────────────────────────────────────────
# Store: wake_due_snoozes
# ─────────────────────────────────────────────────────────────────────────────


def test_wake_due_snoozes_returns_only_due(tmp_path):
    due_sig = signals_store.observe(
        tmp_path, **_observe_kwargs(signature="a:b:admin_bot")
    )
    not_due_sig = signals_store.observe(
        tmp_path, **_observe_kwargs(signature="a:b:team_bot_b", bot_id="team_bot_b")
    )

    past = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat(timespec="seconds")
    future = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat(timespec="seconds")

    signals_store.apply_transition(
        due_sig, "snoozed", tmp_path, actor="user", snoozed_until=past
    )
    signals_store.apply_transition(
        not_due_sig, "snoozed", tmp_path, actor="user", snoozed_until=future
    )

    waked = signals_store.wake_due_snoozes(tmp_path)
    assert len(waked) == 1
    assert waked[0].id == due_sig.id
    assert waked[0].state == "firing"
    assert waked[0].snoozed_until is None

    # Not-due still snoozed
    nd = signals_store.find_signal(tmp_path, not_due_sig.id)
    assert nd is not None and nd[0].state == "snoozed"


# ─────────────────────────────────────────────────────────────────────────────
# Store: feedback log
# ─────────────────────────────────────────────────────────────────────────────


def test_write_feedback_appends_jsonl(tmp_path):
    signals_store.write_feedback(
        tmp_path,
        signal_id="sig-1",
        signal_signature="pod_report:cost_spike:admin_bot",
        proposal_id="prop-1",
        verdict="false_positive",
        note="Black Friday — expected",
    )
    signals_store.write_feedback(
        tmp_path,
        signal_id="sig-2",
        signal_signature="audit:critical:pod",
        proposal_id="prop-2",
        verdict="bad_inference",
    )

    log = signals_store.feedback_log_path(tmp_path)
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])

    assert rec1["signal_id"] == "sig-1"
    assert rec1["verdict"] == "false_positive"
    assert rec1["note"] == "Black Friday — expected"
    assert rec2["proposal_id"] == "prop-2"
    assert rec2["verdict"] == "bad_inference"


# ─────────────────────────────────────────────────────────────────────────────
# Store: cross-link helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_attach_and_detach_proposal_idempotent(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())

    signals_store.attach_proposal(sig, tmp_path, proposal_id="prop-1")
    signals_store.attach_proposal(sig, tmp_path, proposal_id="prop-1")
    signals_store.attach_proposal(sig, tmp_path, proposal_id="prop-2")

    assert sig.motivated_proposals == ["prop-1", "prop-2"]

    signals_store.detach_proposal(sig, tmp_path, proposal_id="prop-1")
    assert sig.motivated_proposals == ["prop-2"]


# ─────────────────────────────────────────────────────────────────────────────
# Store: delivery audit
# ─────────────────────────────────────────────────────────────────────────────


def test_record_delivery_appends_to_audit(tmp_path):
    sig = signals_store.observe(tmp_path, **_observe_kwargs())
    signals_store.record_delivery(sig, tmp_path, channel="telegram")
    signals_store.record_delivery(
        sig,
        tmp_path,
        channel="slack",
        suppressed_reason="severity below routing threshold",
    )
    assert len(sig.deliveries) == 2
    assert sig.deliveries[0].channel == "telegram"
    assert sig.deliveries[1].suppressed_reason is not None


# ─────────────────────────────────────────────────────────────────────────────
# Title hygiene — soft warn at 80, hard truncate at 120
# ─────────────────────────────────────────────────────────────────────────────


def test_observe_truncates_over_hard_limit_and_preserves_original(tmp_path):
    """Producer hands in a 300-char title (cost_watchdog config_drift on a
    fallback list change is the canonical case). Store truncates with ellipsis
    and stashes the original in details.full_title so the row can show it
    on expand."""
    long_title = "team-bot-b: model fallbacks changed " + "x" * 300
    sig = signals_store.observe(
        tmp_path,
        **_observe_kwargs(title=long_title),
    )
    assert len(sig.title) == signals_store.TITLE_HARD_LIMIT
    assert sig.title.endswith("…")
    assert sig.details["full_title"] == long_title


def test_observe_passes_through_short_title_untouched(tmp_path):
    """Titles under the hard limit are untouched and no full_title is set."""
    short = "admin_bot: heartbeat cadence changed (since 2026-05-27)"
    sig = signals_store.observe(tmp_path, **_observe_kwargs(title=short))
    assert sig.title == short
    assert "full_title" not in sig.details


def test_observe_logs_soft_limit_warning(tmp_path, caplog):
    """Titles over the soft limit (80) but under the hard limit (120) emit
    a warning to logs so dev/CI noise nudges producers to fix."""
    soft_offender = "x" * 100  # over 80, under 120
    with caplog.at_level("WARNING", logger="signals.store"):
        signals_store.observe(tmp_path, **_observe_kwargs(title=soft_offender))
    assert any("exceeds soft limit" in r.message for r in caplog.records)


def test_observe_clears_stale_full_title_on_re_observe_with_short_title(tmp_path):
    """If the producer was emitting long titles then fixes itself to short,
    the carried-over full_title from prior bumps should clear so the
    expanded row doesn't keep showing the obsolete long form."""
    long_title = "y" * 300
    sig1 = signals_store.observe(
        tmp_path, **_observe_kwargs(title=long_title)
    )
    assert "full_title" in sig1.details

    sig2 = signals_store.observe(
        tmp_path, **_observe_kwargs(title="short title now")
    )
    assert sig2.id == sig1.id  # same signature → same signal
    assert sig2.title == "short title now"
    assert "full_title" not in sig2.details


# ─────────────────────────────────────────────────────────────────────────────
# iter_active — min_severity floor
# ─────────────────────────────────────────────────────────────────────────────


def test_iter_active_min_severity_warn_hides_info(tmp_path):
    """min_severity='warn' returns warn + alert, drops info — matches the
    Alerts page's default 'hide info-tier' behavior."""
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(signature="s:1:a", severity="info"),
    )
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(signature="s:2:b", severity="warn"),
    )
    signals_store.observe(
        tmp_path,
        **_observe_kwargs(signature="s:3:c", severity="alert"),
    )

    floored = list(signals_store.iter_active(tmp_path, min_severity="warn"))
    severities = sorted(s.severity for s in floored)
    assert severities == ["alert", "warn"]


def test_iter_active_min_severity_alert_only_returns_alert(tmp_path):
    signals_store.observe(
        tmp_path, **_observe_kwargs(signature="s:1:a", severity="info"),
    )
    signals_store.observe(
        tmp_path, **_observe_kwargs(signature="s:2:b", severity="warn"),
    )
    signals_store.observe(
        tmp_path, **_observe_kwargs(signature="s:3:c", severity="alert"),
    )

    floored = list(signals_store.iter_active(tmp_path, min_severity="alert"))
    assert len(floored) == 1
    assert floored[0].severity == "alert"


def test_iter_active_min_severity_info_returns_everything(tmp_path):
    """min_severity='info' is the floor for the lowest tier — equivalent
    to not filtering by severity at all."""
    signals_store.observe(
        tmp_path, **_observe_kwargs(signature="s:1:a", severity="info"),
    )
    signals_store.observe(
        tmp_path, **_observe_kwargs(signature="s:2:b", severity="alert"),
    )

    floored = list(signals_store.iter_active(tmp_path, min_severity="info"))
    assert len(floored) == 2
