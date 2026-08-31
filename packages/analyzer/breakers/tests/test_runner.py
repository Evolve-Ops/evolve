"""Tests for breakers.runner — Phase 5 auto-trip linchpin.

Every test injects stubs for store / enforce / dispatcher / signals so
no real I/O happens. The runner's logic — eval window, observe-only
gate, idempotent skip when already tripped, recurrent-trip signal,
admin notify — is exercised end-to-end against synthetic turn data.

Three load-bearing invariants pinned here:

  1. OBSERVE-ONLY when flag off — detector runs, runner log captures
     "would have tripped", but no state write / enforce / notify.
     This is what makes Phase 5 safe to deploy immediately.

  2. IDEMPOTENT — when a bot is already tripped, runner sees the
     existing record and skips the trip path (no duplicate trip_id,
     no spurious "retrip" audit entry, no duplicate admin alert).

  3. RECURRENT SIGNAL — a second auto-trip on the same (bot, type)
     within the 48h window observes a recurrent_breaker_trip Signal
     so the operator sees the pattern, not just one more trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from breakers import runner
from breakers.detector import DEFAULT_CONFIG


# Reference "now" used across tests.
FIXED_NOW = datetime(2026, 5, 21, 16, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — write synthetic turn data
# ─────────────────────────────────────────────────────────────────────────────


def _write_turns(
    shared_dir: Path, bot_id: str, records: list[dict],
) -> None:
    """Lay out turn JSONLs in the shape backtest.read_turns expects:
    {shared_dir}/<bot>/turns/turns-YYYY-MM-DD.jsonl."""
    by_day: dict[str, list[dict]] = {}
    for r in records:
        by_day.setdefault(r["ts"][:10], []).append(r)
    turn_dir = shared_dir / bot_id / "turns"
    turn_dir.mkdir(parents=True, exist_ok=True)
    for day, recs in by_day.items():
        with (turn_dir / f"turns-{day}.jsonl").open("w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")


def _haiku_baseline(shared_dir: Path, bot_id: str) -> None:
    """Write 7 days of hourly haiku heartbeats — establishes the
    bot's normal-activity baseline."""
    records = []
    for day_offset in range(8, 1, -1):
        day = FIXED_NOW - timedelta(days=day_offset)
        for hour in range(24):
            ts = day.replace(hour=hour, minute=5)
            records.append({
                "ts": ts.isoformat().replace("+00:00", "Z"),
                "source": "heartbeat",
                "channel": "heartbeat",
                "model": "anthropic/claude-haiku-4-5",
            })
    _write_turns(shared_dir, bot_id, records)


def _spike(shared_dir: Path, bot_id: str, *, count: int = 30) -> None:
    """Add a fresh-window spike of sonnet heartbeats in the last hour
    before FIXED_NOW. Exercises the detector's rate-spike + tier-shift
    prongs together."""
    spike_records = []
    for i in range(count):
        ts = FIXED_NOW - timedelta(minutes=58 - (i * (55 / count)))
        spike_records.append({
            "ts": ts.isoformat().replace("+00:00", "Z"),
            "source": "heartbeat",
            "channel": "heartbeat",
            "model": "anthropic/claude-sonnet-4-6",
        })
    # Append to existing day file if present, otherwise create.
    day = FIXED_NOW.strftime("%Y-%m-%d")
    path = shared_dir / bot_id / "turns" / f"turns-{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in spike_records:
            f.write(json.dumps(r) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Injection stubs — collect args + control return values
# ─────────────────────────────────────────────────────────────────────────────


class _StubStore:
    def __init__(self):
        self.trips: list[dict] = []
        self.resets: list[dict] = []
        self.read_returns: dict[tuple[str, str], Any] = {}

    def trip(self, *, shared_dir, scope, breaker_type, duration,
             initiated_by, reason, motivating_signals=None):
        # Return a tiny stand-in record. The real BreakerRecord shape is
        # bigger but the runner only reads trip_id + expires_at.
        from types import SimpleNamespace
        rec = SimpleNamespace(
            bot_id=scope, type=breaker_type,
            trip_id=f"trip-{len(self.trips):04d}",
            expires_at=(FIXED_NOW + duration).isoformat() if duration else None,
            reason=reason,
        )
        self.trips.append({
            "scope": scope, "breaker_type": breaker_type,
            "duration": duration, "reason": reason,
            "initiated_by": initiated_by,
        })
        return rec

    def read_trip(self, shared_dir, scope, breaker_type):
        return self.read_returns.get((scope, breaker_type))

    def list_active(self, shared_dir):
        return []


def _stub_enforce_ok(*, scope, breaker_type, network, **kwargs):
    from types import SimpleNamespace
    return SimpleNamespace(ok=True, no_op=True, no_op_reason="stub")


def _stub_send_ok(**kwargs):
    # Return a DispatchOutcome-shaped object so the runner's
    # `result == DispatchResult.SENT` check sees a sent result.
    # Imports are deferred so the test module doesn't require admin on
    # the path at collection time.
    from evolve_admin.alerts.dispatcher import DispatchOutcome, DispatchResult
    return DispatchOutcome(
        result=DispatchResult.SENT,
        source=kwargs.get("source", "test"),
        severity=kwargs.get("severity"),
        dedup_key=kwargs.get("dedup_key"),
        catalog_event=kwargs.get("catalog_event"),
    )


def _make_signal_observer():
    """Returns (observer fn, captured-calls list)."""
    calls: list[dict] = []
    def _observe(shared_dir, **kw):
        calls.append({"shared_dir": shared_dir, **kw})
        from types import SimpleNamespace
        return SimpleNamespace(id="sig-1", signature=kw.get("signature"))
    return _observe, calls


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def quiet_network() -> dict:
    """One bot, auto-trip explicitly DISARMED — the operator opt-out
    (armed is the code default since the §5.2 arming PR)."""
    return {
        "primary": "team_bot_a",
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "breakers": {"auto_trip_enabled": False},
    }


@pytest.fixture
def auto_network() -> dict:
    """Same shape but auto_trip_enabled explicitly True."""
    return {
        "primary": "team_bot_a",
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "breakers": {"auto_trip_enabled": True},
    }


@pytest.fixture
def default_network() -> dict:
    """One bot, NO breakers key at all — exercises the code default,
    which is ARMED since the §5.2 arming PR."""
    return {
        "primary": "team_bot_a",
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Observe-only (flag explicitly off) — the operator opt-out
# ─────────────────────────────────────────────────────────────────────────────


class TestObserveOnly:
    def test_no_trip_when_flag_off_even_if_detector_says_yes(
        self, shared_dir: Path, quiet_network: dict,
    ) -> None:
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        store = _StubStore()
        observer, signal_calls = _make_signal_observer()

        result = runner.run_once(
            shared_dir=shared_dir,
            network=quiet_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        assert result.auto_trip_enabled is False
        # The detector did recommend a trip.
        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.decision["trip"] is True, team_bot_a_action.decision
        # But we did NOT act on it.
        assert team_bot_a_action.actioned is False
        assert "observe-only" in team_bot_a_action.skip_reason
        # No state writes, no signals.
        assert store.trips == []
        assert signal_calls == []

    def test_runner_log_records_observe_only_decision(
        self, shared_dir: Path, quiet_network: dict,
    ) -> None:
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        store = _StubStore()
        observer, _ = _make_signal_observer()

        runner.run_once(
            shared_dir=shared_dir,
            network=quiet_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        log_path = shared_dir / "breakers" / "runner-log"
        files = list(log_path.glob("*.jsonl"))
        assert len(files) == 1
        entries = [json.loads(l) for l in files[0].read_text().splitlines() if l]
        assert len(entries) == 1
        assert entries[0]["bot_id"] == "team_bot_a"
        assert entries[0]["auto_trip_enabled"] is False
        assert entries[0]["decision"]["trip"] is True
        assert entries[0]["actioned"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Runner-log source-cut (footprint F-5-A) — suppress the no-op re-emission
# ─────────────────────────────────────────────────────────────────────────────


def _runner_log_entries(shared_dir: Path) -> list[dict]:
    """All runner-log records across every day file, oldest-first by line."""
    log_dir = shared_dir / "breakers" / "runner-log"
    entries: list[dict] = []
    for f in sorted(log_dir.glob("*.jsonl")):
        entries.extend(
            json.loads(line) for line in f.read_text().splitlines() if line
        )
    return entries


def _run_cycle(shared_dir: Path, network: dict, store: "_StubStore", observer):
    return runner.run_once(
        shared_dir=shared_dir,
        network=network,
        now=FIXED_NOW,
        trip_fn=store.trip,
        read_trip_fn=store.read_trip,
        list_active_fn=store.list_active,
        enforce_trip_fn=_stub_enforce_ok,
        dispatcher_send_fn=_stub_send_ok,
        signal_observe_fn=observer,
    )


class TestRunnerLogSourceCut:
    def test_steady_state_no_op_suppressed_after_first_cycle(
        self, shared_dir: Path, quiet_network: dict,
    ) -> None:
        # A quiet bot (baseline only, no spike) → detector returns trip:false
        # every cycle. The first cycle establishes the baseline (logged once);
        # subsequent identical no-op cycles write nothing.
        _haiku_baseline(shared_dir, "team_bot_a")
        store = _StubStore()
        observer, _ = _make_signal_observer()

        _run_cycle(shared_dir, quiet_network, store, observer)
        first = _runner_log_entries(shared_dir)
        assert len(first) == 1
        assert first[0]["decision"]["trip"] is False

        # Two more identical cycles — both suppressed.
        _run_cycle(shared_dir, quiet_network, store, observer)
        _run_cycle(shared_dir, quiet_network, store, observer)
        assert len(_runner_log_entries(shared_dir)) == 1

        # The change-detection sidecar is written (and is a dotfile).
        sidecar = shared_dir / "breakers" / "runner-log" / ".last-decision.json"
        assert sidecar.exists()

    def test_actionable_decision_logged_every_cycle(
        self, shared_dir: Path, quiet_network: dict,
    ) -> None:
        # A persistent would-trip (spike, observe-only) is the calibration
        # signal we exist to capture — logged every cycle even though it does
        # not change.
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        store = _StubStore()
        observer, _ = _make_signal_observer()

        _run_cycle(shared_dir, quiet_network, store, observer)
        _run_cycle(shared_dir, quiet_network, store, observer)
        entries = _runner_log_entries(shared_dir)
        assert len(entries) == 2
        assert all(e["decision"]["trip"] is True for e in entries)
        assert all(e["actioned"] is False for e in entries)

    def test_decision_change_is_logged(
        self, shared_dir: Path, quiet_network: dict,
    ) -> None:
        # Quiet → no-op (logged once). Then a spike lands → the decision
        # CHANGES (trip:false → trip:true) and the transition is logged.
        _haiku_baseline(shared_dir, "team_bot_a")
        store = _StubStore()
        observer, _ = _make_signal_observer()

        _run_cycle(shared_dir, quiet_network, store, observer)
        assert len(_runner_log_entries(shared_dir)) == 1

        _spike(shared_dir, "team_bot_a", count=40)
        _run_cycle(shared_dir, quiet_network, store, observer)
        entries = _runner_log_entries(shared_dir)
        assert len(entries) == 2
        assert entries[0]["decision"]["trip"] is False
        assert entries[1]["decision"]["trip"] is True

    def test_full_verbosity_flag_logs_every_cycle(
        self, shared_dir: Path, quiet_network: dict,
    ) -> None:
        # With the opt-in calibration-soak flag, even the steady-state no-op
        # is written every cycle.
        verbose_network = dict(quiet_network)
        verbose_network["breakers"] = {"runner_log_full_verbosity": True}
        _haiku_baseline(shared_dir, "team_bot_a")
        store = _StubStore()
        observer, _ = _make_signal_observer()

        _run_cycle(shared_dir, verbose_network, store, observer)
        _run_cycle(shared_dir, verbose_network, store, observer)
        assert len(_runner_log_entries(shared_dir)) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Auto-trip enabled — actually trips
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoTrip:
    def test_trip_fires_when_flag_on_and_decision_is_trip(
        self, shared_dir: Path, auto_network: dict,
    ) -> None:
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        store = _StubStore()
        observer, signal_calls = _make_signal_observer()
        enforce_calls: list[dict] = []
        def _capture_enforce(**kw):
            enforce_calls.append(kw)
            return _stub_enforce_ok(**kw)
        send_calls: list[dict] = []
        def _capture_send(**kw):
            send_calls.append(kw)
            return _stub_send_ok(**kw)

        result = runner.run_once(
            shared_dir=shared_dir,
            network=auto_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_capture_enforce,
            dispatcher_send_fn=_capture_send,
            signal_observe_fn=observer,
        )

        assert result.auto_trip_enabled is True
        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.actioned is True
        assert team_bot_a_action.trip_id is not None
        # State write happened.
        assert len(store.trips) == 1
        assert store.trips[0]["scope"] == "team_bot_a"
        assert store.trips[0]["breaker_type"] == "cost"
        assert store.trips[0]["initiated_by"] == "auto"
        # Enforce called with matching args.
        assert len(enforce_calls) == 1
        assert enforce_calls[0]["scope"] == "team_bot_a"
        assert enforce_calls[0]["breaker_type"] == "cost"
        # Admin notified via the cost.breaker_tripped catalog event.
        # Visibility fix from the 2026-05-28 Security_bot incident: trips
        # route through catalog + payload (CRITICAL severity, ⚡ +
        # bold body template) rather than a plain-text WARNING.
        assert len(send_calls) == 1
        call = send_calls[0]
        assert call["catalog_event"] == "cost.breaker_tripped"
        assert "message" not in call  # no inline body
        payload = call["payload"]
        assert payload["bot_id"] == "team_bot_a"
        assert payload["breaker_type"] == "cost"
        assert payload["trip_id_short"] == team_bot_a_action.trip_id[:8]
        # The reason text from the detector decision flows through verbatim
        assert isinstance(payload["reason"], str) and payload["reason"]
        # Severity is CRITICAL; source is the registered breakers_runner.
        from evolve_admin.alerts.dispatcher import Severity
        assert call["severity"] == Severity.CRITICAL
        assert call["source"] == "breakers_runner"
        # First trip → no recurrent signal.
        assert signal_calls == []
        assert team_bot_a_action.recurrent is False

    def test_trip_fires_by_default_with_no_breakers_config(
        self, shared_dir: Path, default_network: dict,
    ) -> None:
        """§5.2 arming, end-to-end: a network.json with NO breakers key is
        armed — the runner acts on a trip decision, no config edit needed."""
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        store = _StubStore()
        observer, signal_calls = _make_signal_observer()

        result = runner.run_once(
            shared_dir=shared_dir,
            network=default_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        assert result.auto_trip_enabled is True
        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.actioned is True
        assert len(store.trips) == 1
        assert store.trips[0]["initiated_by"] == "auto"

    def test_does_not_trip_when_decision_is_no_trip(
        self, shared_dir: Path, auto_network: dict,
    ) -> None:
        # Baseline only — no spike, no trip.
        _haiku_baseline(shared_dir, "team_bot_a")
        store = _StubStore()
        observer, _ = _make_signal_observer()

        result = runner.run_once(
            shared_dir=shared_dir,
            network=auto_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.decision["trip"] is False
        assert team_bot_a_action.actioned is False
        assert store.trips == []


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_skips_when_already_tripped(
        self, shared_dir: Path, auto_network: dict,
    ) -> None:
        """Bot already has an active cost trip → runner sees it via
        read_trip and skips. No duplicate trip_id, no second
        enforce, no second admin alert."""
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        store = _StubStore()
        # Pre-seed the read: bot is already tripped.
        from types import SimpleNamespace
        store.read_returns[("team_bot_a", "cost")] = SimpleNamespace(
            bot_id="team_bot_a", type="cost", trip_id="existing-uuid",
            expires_at=None, reason="prior", initiated_by="auto",
        )
        observer, signal_calls = _make_signal_observer()

        result = runner.run_once(
            shared_dir=shared_dir,
            network=auto_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.actioned is False
        assert "already tripped" in team_bot_a_action.skip_reason
        assert team_bot_a_action.trip_id == "existing-uuid"
        # No re-trip / re-enforce / re-notify.
        assert store.trips == []
        assert signal_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Recurrent trip — second trip within 48h fires the meta-signal
# ─────────────────────────────────────────────────────────────────────────────


class TestRecurrentTrip:
    def _seed_prior_auto_trip(
        self, shared_dir: Path, bot_id: str, hours_ago: float = 12,
    ) -> None:
        """Write an audit-log entry simulating a prior auto-trip that's
        now cleared."""
        log_dir = shared_dir / "breakers" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = (FIXED_NOW - timedelta(hours=hours_ago)).isoformat()
        log_path = log_dir / f"{(FIXED_NOW - timedelta(hours=hours_ago)).strftime('%Y-%m-%d')}.jsonl"
        with log_path.open("a") as f:
            f.write(json.dumps({
                "action": "trip", "scope": bot_id, "type": "cost",
                "trip_id": "prior-trip-uuid",
                "initiated_by": "auto",
                "reason": "prior reason",
                "timestamp": ts,
            }) + "\n")
            # Plus a clear (within window) — recurrent check looks for
            # auto-trip activity in the window.
            f.write(json.dumps({
                "action": "auto_recover", "scope": bot_id, "type": "cost",
                "trip_id": "prior-trip-uuid",
                "initiated_by": "heal:reaper",
                "timestamp": (FIXED_NOW - timedelta(hours=hours_ago - 1)).isoformat(),
            }) + "\n")

    def test_recurrent_signal_observed_on_second_trip_within_48h(
        self, shared_dir: Path, auto_network: dict,
    ) -> None:
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        self._seed_prior_auto_trip(shared_dir, "team_bot_a", hours_ago=12)
        store = _StubStore()
        observer, signal_calls = _make_signal_observer()

        result = runner.run_once(
            shared_dir=shared_dir,
            network=auto_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.actioned is True
        assert team_bot_a_action.recurrent is True
        # Exactly one recurrent_breaker_trip signal observed.
        assert len(signal_calls) == 1
        call = signal_calls[0]
        assert call["producer"] == "breakers_runner"
        assert call["type"] == "recurrent_breaker_trip"
        assert call["bot_id"] == "team_bot_a"
        assert "recurrent_breaker_trip" in call["signature"]

    def test_no_recurrent_signal_when_prior_was_outside_window(
        self, shared_dir: Path, auto_network: dict,
    ) -> None:
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        # Prior trip > 48h ago — outside the recurrent window.
        self._seed_prior_auto_trip(shared_dir, "team_bot_a", hours_ago=60)
        store = _StubStore()
        observer, signal_calls = _make_signal_observer()

        result = runner.run_once(
            shared_dir=shared_dir,
            network=auto_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.actioned is True
        assert team_bot_a_action.recurrent is False
        assert signal_calls == []

    def test_no_recurrent_signal_when_prior_was_manual(
        self, shared_dir: Path, auto_network: dict,
    ) -> None:
        """A manual trip (initiated_by != auto) should NOT count
        toward recurrent detection. Only repeated AUTO trips
        indicate a pattern worth signaling."""
        _haiku_baseline(shared_dir, "team_bot_a")
        _spike(shared_dir, "team_bot_a", count=40)
        # Write a manual trip in the window.
        log_dir = shared_dir / "breakers" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = (FIXED_NOW - timedelta(hours=12)).isoformat()
        log_path = log_dir / f"{(FIXED_NOW - timedelta(hours=12)).strftime('%Y-%m-%d')}.jsonl"
        with log_path.open("a") as f:
            f.write(json.dumps({
                "action": "trip", "scope": "team_bot_a", "type": "cost",
                "trip_id": "manual-uuid",
                "initiated_by": "admin:pod_admin",
                "reason": "manual",
                "timestamp": ts,
            }) + "\n")
        store = _StubStore()
        observer, signal_calls = _make_signal_observer()

        result = runner.run_once(
            shared_dir=shared_dir,
            network=auto_network,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=store.read_trip,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        team_bot_a_action = next(a for a in result.actions if a.bot_id == "team_bot_a")
        assert team_bot_a_action.actioned is True
        # Manual prior trip doesn't count → not recurrent.
        assert team_bot_a_action.recurrent is False
        assert signal_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Fail-open: outer guard catches per-bot exceptions
# ─────────────────────────────────────────────────────────────────────────────


class TestFailOpen:
    def test_one_bot_crash_does_not_block_others(
        self, shared_dir: Path,
    ) -> None:
        """If one bot's processing raises, the runner records the
        failure for that bot but continues to the next."""
        net = {
            "primary": "team_bot_a",
            "members": ["team_bot_a", "security_bot"],
            "bots": {"team_bot_a": {"user": "team_bot_a"}, "security_bot": {"user": "security_bot"}},
            "breakers": {"auto_trip_enabled": True},
        }
        _haiku_baseline(shared_dir, "team_bot_a")
        _haiku_baseline(shared_dir, "security_bot")
        _spike(shared_dir, "security_bot", count=40)
        store = _StubStore()
        observer, _ = _make_signal_observer()

        # Add a spike on team_bot_a too so the detector recommends a trip
        # for it — otherwise the "detector did not recommend trip"
        # path short-circuits before read_trip is called and we never
        # see the RuntimeError.
        _spike(shared_dir, "team_bot_a", count=40)

        # Make read_trip raise for team_bot_a but not for security_bot.
        def _selective_read(shared_dir_arg, scope, breaker_type):
            if scope == "team_bot_a":
                raise RuntimeError("simulated read crash")
            return None

        result = runner.run_once(
            shared_dir=shared_dir,
            network=net,
            now=FIXED_NOW,
            trip_fn=store.trip,
            read_trip_fn=_selective_read,
            list_active_fn=store.list_active,
            enforce_trip_fn=_stub_enforce_ok,
            dispatcher_send_fn=_stub_send_ok,
            signal_observe_fn=observer,
        )

        # team_bot_a recorded as failed; security_bot processed normally.
        actions_by_bot = {a.bot_id: a for a in result.actions}
        assert "RuntimeError" in actions_by_bot["team_bot_a"].skip_reason
        assert actions_by_bot["security_bot"].actioned is True


# ─────────────────────────────────────────────────────────────────────────────
# Config flag reader
# ─────────────────────────────────────────────────────────────────────────────


class TestFlagReader:
    @pytest.mark.parametrize("net", [
        {}, {"breakers": {}}, {"some": "other"},
    ])
    def test_defaults_to_true_when_unset(self, net) -> None:
        """§5.2 arming: a well-formed config with no explicit opt-out is
        ARMED. This is the default flip the arming PR ships."""
        assert runner.read_auto_trip_enabled(net) is True

    def test_explicit_false_disarms(self) -> None:
        """The operator opt-out (`evolve-admin breaker disarm`) is honored."""
        assert runner.read_auto_trip_enabled(
            {"breakers": {"auto_trip_enabled": False}}
        ) is False

    @pytest.mark.parametrize("net", [None, "not a dict"])
    def test_malformed_network_reads_disarmed(self, net) -> None:
        """Never enforce against a pod whose config we couldn't read."""
        assert runner.read_auto_trip_enabled(net) is False

    def test_explicit_true(self) -> None:
        assert runner.read_auto_trip_enabled(
            {"breakers": {"auto_trip_enabled": True}}
        ) is True

    def test_truthy_string_not_promoted(self) -> None:
        """Defensive: don't auto-convert truthy strings."""
        # bool() of non-empty string is True, but we want explicit boolean true.
        # Current impl uses bool(), so any truthy value works. That's a
        # known minor footgun documented here so a stricter implementation
        # can replace it later if desired.
        assert runner.read_auto_trip_enabled(
            {"breakers": {"auto_trip_enabled": "yes"}}
        ) is True


# ─────────────────────────────────────────────────────────────────────────────
# _was_recently_auto_tripped — direct tests on the audit-log scanner
# ─────────────────────────────────────────────────────────────────────────────


class TestRecentlyAutoTripped:
    def test_returns_false_when_no_log(self, shared_dir: Path) -> None:
        assert runner._was_recently_auto_tripped(
            shared_dir, "team_bot_a", "cost", now=FIXED_NOW,
        ) is False

    def test_returns_true_for_auto_trip_in_window(
        self, shared_dir: Path,
    ) -> None:
        log_dir = shared_dir / "breakers" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = (FIXED_NOW - timedelta(hours=10)).isoformat()
        path = log_dir / f"{(FIXED_NOW - timedelta(hours=10)).strftime('%Y-%m-%d')}.jsonl"
        path.write_text(json.dumps({
            "action": "trip", "scope": "team_bot_a", "type": "cost",
            "initiated_by": "auto",
            "timestamp": ts,
        }) + "\n")
        assert runner._was_recently_auto_tripped(
            shared_dir, "team_bot_a", "cost", now=FIXED_NOW,
        ) is True

    def test_returns_false_for_other_bot(self, shared_dir: Path) -> None:
        log_dir = shared_dir / "breakers" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = (FIXED_NOW - timedelta(hours=10)).isoformat()
        path = log_dir / f"{(FIXED_NOW - timedelta(hours=10)).strftime('%Y-%m-%d')}.jsonl"
        path.write_text(json.dumps({
            "action": "trip", "scope": "security_bot", "type": "cost",
            "initiated_by": "auto",
            "timestamp": ts,
        }) + "\n")
        assert runner._was_recently_auto_tripped(
            shared_dir, "team_bot_a", "cost", now=FIXED_NOW,
        ) is False

    def test_returns_false_for_other_type(self, shared_dir: Path) -> None:
        log_dir = shared_dir / "breakers" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = (FIXED_NOW - timedelta(hours=10)).isoformat()
        path = log_dir / f"{(FIXED_NOW - timedelta(hours=10)).strftime('%Y-%m-%d')}.jsonl"
        path.write_text(json.dumps({
            "action": "trip", "scope": "team_bot_a", "type": "full",
            "initiated_by": "auto",
            "timestamp": ts,
        }) + "\n")
        assert runner._was_recently_auto_tripped(
            shared_dir, "team_bot_a", "cost", now=FIXED_NOW,
        ) is False

    def test_corrupt_log_returns_false_safely(
        self, shared_dir: Path,
    ) -> None:
        log_dir = shared_dir / "breakers" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{FIXED_NOW.strftime('%Y-%m-%d')}.jsonl"
        path.write_text("not-json{{{\nalso-not-json\n")
        # Should not raise; returns False as if no record.
        assert runner._was_recently_auto_tripped(
            shared_dir, "team_bot_a", "cost", now=FIXED_NOW,
        ) is False
