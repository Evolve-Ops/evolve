"""tests/test_signal_subscriber.py — Event-driven generator dispatch.

Covers:
  - Charter.subscribes_to round-trip via from_dict/to_dict (backwards-
    compatible default).
  - Subscriber discovery picks up new Signal files in firing/ and
    matches them against the subscription map.
  - Ledger prevents re-fire across daemon restarts.
  - dispatch_signal invokes the registered run_one_generator and
    records the fire.
  - End-to-end: simulate a Signal write → run_loop with stop_after_seconds
    → dispatch invoked exactly once.

The runner callable is stubbed in every test so we do not need to load
real charter YAML or import generator modules.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from schema.generator import Charter  # noqa: E402
from schema.signal import Signal  # noqa: E402
from signals import store as signals_store  # noqa: E402
from signals import subscriber  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_firing_signal(
    shared_dir: Path,
    *,
    signal_id: str,
    signal_type: str,
    bot_id: str | None = "team-bot-a",
    signature: str | None = None,
    producer: str = "cost_watchdog",
) -> Signal:
    """Write a fresh firing Signal to the store via the public producer API.

    Uses signals.store.observe so the file lands atomically and the
    state-change log gets written — matches production behaviour.
    """
    sig = signals_store.observe(
        shared_dir,
        signature=signature or f"{producer}:{signal_type}:{bot_id or 'pod'}:{signal_id}",
        producer=producer,
        type=signal_type,
        flavor="diagnostic",
        severity="warn",
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=f"Synthetic {signal_type}",
    )
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Charter schema
# ─────────────────────────────────────────────────────────────────────────────


def test_charter_subscribes_to_round_trip() -> None:
    """Charter round-trips a non-empty subscribes_to list."""
    raw = {
        "id": "demo",
        "schema_version": 1,
        "type": "optimizer",
        "dimension": "cost",
        "purpose": "demo",
        "cadence": "daily",
        "bucket": "improve",
        "invariants": [],
        "resolves_when_silent": False,
        "subscribes_to": ["cost_spike", "session_token_outlier"],
    }
    c = Charter.from_dict(raw)
    assert c.subscribes_to == ["cost_spike", "session_token_outlier"]
    serialized = c.to_dict()
    assert serialized["subscribes_to"] == ["cost_spike", "session_token_outlier"]


def test_charter_subscribes_to_defaults_to_empty() -> None:
    """A charter without subscribes_to loads with an empty list (backwards
    compatibility for every existing charter on disk)."""
    raw = {
        "id": "demo",
        "schema_version": 1,
        "type": "optimizer",
        "dimension": "cost",
        "purpose": "demo",
        "cadence": "daily",
        "bucket": "improve",
        "invariants": [],
        "resolves_when_silent": False,
    }
    c = Charter.from_dict(raw)
    assert c.subscribes_to == []


def test_charter_subscribes_to_malformed_falls_back_to_empty() -> None:
    """Wrong type (string instead of list) coerces to [] instead of raising —
    a typo in one charter must not block the registry."""
    raw = {
        "id": "demo",
        "schema_version": 1,
        "type": "optimizer",
        "dimension": "cost",
        "purpose": "demo",
        "cadence": "daily",
        "bucket": "improve",
        "invariants": [],
        "subscribes_to": "cost_spike",  # operator forgot the brackets
    }
    c = Charter.from_dict(raw)
    assert c.subscribes_to == []


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────


def test_find_new_signal_ids_returns_only_new_of_interest(tmp_path: Path) -> None:
    """Only firing/ files matching the type filter are surfaced as new."""
    shared = tmp_path / "evolve"

    sig_a = _write_firing_signal(shared, signal_id="a", signal_type="cost_spike")
    sig_b = _write_firing_signal(shared, signal_id="b", signal_type="something_else")

    seen: set[str] = set()
    new = subscriber.find_new_signal_ids(
        shared, seen=seen, types_of_interest={"cost_spike"}
    )
    assert sig_a.id in new
    assert sig_b.id not in new
    # Both should now be in seen so the next call returns []
    assert sig_a.id in seen and sig_b.id in seen
    again = subscriber.find_new_signal_ids(
        shared, seen=seen, types_of_interest={"cost_spike"}
    )
    assert again == []


def test_bootstrap_seen_includes_existing_firing(tmp_path: Path) -> None:
    """On daemon start we treat existing firing/ Signals as already-seen —
    they are the daily sweep's problem, not the subscriber's."""
    shared = tmp_path / "evolve"
    sig = _write_firing_signal(shared, signal_id="x", signal_type="cost_spike")
    seen = subscriber.bootstrap_seen_set(shared)
    assert sig.id in seen


# ─────────────────────────────────────────────────────────────────────────────
# Ledger
# ─────────────────────────────────────────────────────────────────────────────


def test_ledger_has_fired_and_record(tmp_path: Path) -> None:
    """record_fire then has_fired returns True; unrelated keys still False."""
    shared = tmp_path / "evolve"
    assert subscriber.has_fired(shared, generator_id="g1", signal_id="s1") is False
    subscriber.record_fire(
        shared,
        generator_id="g1",
        signal_id="s1",
        signal_type="cost_spike",
        proposals_ingested=1,
    )
    assert subscriber.has_fired(shared, generator_id="g1", signal_id="s1") is True
    assert subscriber.has_fired(shared, generator_id="g2", signal_id="s1") is False
    assert subscriber.has_fired(shared, generator_id="g1", signal_id="s2") is False


def test_ledger_prune_drops_old_entries(tmp_path: Path) -> None:
    """prune_ledger removes entries past LEDGER_RETENTION_SECONDS."""
    shared = tmp_path / "evolve"
    subscriber._ensure_subscriber_dir(shared)
    ledger = subscriber.ledger_path(shared)

    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    fresh_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    ledger.write_text(
        json.dumps(
            {
                "generator_id": "g1",
                "signal_id": "old",
                "signal_type": "cost_spike",
                "proposals_ingested": 0,
                "fired_at": old_ts,
            }
        )
        + "\n"
        + json.dumps(
            {
                "generator_id": "g1",
                "signal_id": "fresh",
                "signal_type": "cost_spike",
                "proposals_ingested": 1,
                "fired_at": fresh_ts,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    kept = subscriber.prune_ledger(shared)
    assert kept == 1
    # has_fired for the fresh entry survives the prune; the old one does not.
    assert subscriber.has_fired(shared, generator_id="g1", signal_id="fresh") is True
    assert subscriber.has_fired(shared, generator_id="g1", signal_id="old") is False


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_signal_invokes_runner_once(tmp_path: Path) -> None:
    """dispatch_signal calls run_one_generator with the Signal's bot_id and
    records the fire so a second call no-ops."""
    shared = tmp_path / "evolve"
    sig = _write_firing_signal(shared, signal_id="x", signal_type="cost_spike", bot_id="team-bot-a")

    calls: list[tuple] = []

    def fake_runner(shared_dir, network_config, gen_id, *, bot_id, log_fn=None):  # noqa: ANN001
        calls.append((gen_id, bot_id))
        return 1  # proposals_ingested

    results = subscriber.dispatch_signal(
        shared,
        signal_id=sig.id,
        subscription_map={"cost_spike": ["cost_root_cause_correlator"]},
        network_config={},
        run_one_generator=fake_runner,
        log_fn=lambda _msg: None,
    )

    assert calls == [("cost_root_cause_correlator", "team-bot-a")]
    assert len(results) == 1 and results[0].fired and results[0].proposals_ingested == 1

    # Re-dispatching the same Signal is a no-op (ledger hit).
    results2 = subscriber.dispatch_signal(
        shared,
        signal_id=sig.id,
        subscription_map={"cost_spike": ["cost_root_cause_correlator"]},
        network_config={},
        run_one_generator=fake_runner,
        log_fn=lambda _msg: None,
    )
    assert len(calls) == 1, "runner must not be re-invoked after ledger hit"
    assert results2[0].fired is False
    assert results2[0].skipped_reason == "already_fired"


def test_dispatch_signal_no_subscribers_is_noop(tmp_path: Path) -> None:
    """A Signal type with no subscribers must not invoke any runner."""
    shared = tmp_path / "evolve"
    sig = _write_firing_signal(shared, signal_id="y", signal_type="something_uninteresting")
    calls: list = []
    results = subscriber.dispatch_signal(
        shared,
        signal_id=sig.id,
        subscription_map={"cost_spike": ["some_gen"]},
        network_config={},
        run_one_generator=lambda *a, **kw: calls.append(a) or 0,
    )
    assert results == []
    assert calls == []


def test_dispatch_signal_missing_signal_is_noop(tmp_path: Path) -> None:
    """If the Signal has left firing/ by the time we dispatch, skip silently —
    not an error (operator might have resolved it manually, sweep might
    have cleared it)."""
    shared = tmp_path / "evolve"
    results = subscriber.dispatch_signal(
        shared,
        signal_id="never-existed",
        subscription_map={"cost_spike": ["cost_root_cause_correlator"]},
        network_config={},
        run_one_generator=lambda *a, **kw: 1,
    )
    assert results == []


def test_dispatch_signal_runner_exception_records_fire(tmp_path: Path) -> None:
    """A generator that raises must not crash the dispatcher; the ledger
    still gets written so a perpetually-broken generator does not get
    retried on every poll."""
    shared = tmp_path / "evolve"
    sig = _write_firing_signal(shared, signal_id="z", signal_type="cost_spike")

    def boom(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("synthetic crash")

    results = subscriber.dispatch_signal(
        shared,
        signal_id=sig.id,
        subscription_map={"cost_spike": ["broken_gen"]},
        network_config={},
        run_one_generator=boom,
        log_fn=lambda _msg: None,
    )
    assert len(results) == 1
    assert results[0].fired is False
    assert results[0].error and "synthetic crash" in results[0].error
    # Ledger entry exists so the same Signal won't retry next poll.
    assert subscriber.has_fired(shared, generator_id="broken_gen", signal_id=sig.id)


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end run_loop
# ─────────────────────────────────────────────────────────────────────────────


def test_run_loop_dispatches_new_signal(tmp_path: Path, monkeypatch) -> None:
    """Integration: a Signal landing in firing/ between bootstrap and the
    first poll lands a single dispatch within one iteration."""
    shared = tmp_path / "evolve"
    generators_code_dir = tmp_path / "fake-generators"  # unused (runner stubbed)
    records_dir = tmp_path / "fake-records"
    generators_code_dir.mkdir(parents=True, exist_ok=True)

    # Pre-build the subscription map by patching build_subscription_map so
    # we don't have to mount a real registry for this test.
    monkeypatch.setattr(
        subscriber,
        "build_subscription_map",
        lambda *, generators_code_dir, records_dir: {
            "cost_spike": ["cost_root_cause_correlator"],
        },
    )

    fired: list[str] = []

    def fake_runner(shared_dir, network_config, gen_id, *, bot_id, log_fn=None):  # noqa: ANN001
        fired.append(gen_id)
        return 1

    # Stub the late-bound run_one_generator import inside dispatch_signal.
    import generator_runner

    monkeypatch.setattr(generator_runner, "run_one_generator", fake_runner)

    # Drive the loop. We use a tiny fake clock so stop_after_seconds is hit
    # after exactly one tick (bootstrap → discover → dispatch → return).
    clock = {"t": 0.0}

    def fake_now():
        return clock["t"]

    def fake_sleep(secs):
        clock["t"] += secs

    # Write a Signal BEFORE run_loop bootstraps so the discovery pass
    # catches it. We need it to be considered "new" — bootstrap_seen_set
    # would mark it as already-seen if we land it there. Workaround: write
    # the Signal AFTER the bootstrap by monkeypatching bootstrap_seen_set
    # to return an empty set.
    monkeypatch.setattr(subscriber, "bootstrap_seen_set", lambda _sd: set())
    sig = _write_firing_signal(shared, signal_id="x", signal_type="cost_spike")

    total = subscriber.run_loop(
        shared,
        network_config={},
        generators_code_dir=generators_code_dir,
        records_dir=records_dir,
        poll_interval_seconds=1.0,
        stop_after_seconds=0.5,
        sleep_fn=fake_sleep,
        now_fn=fake_now,
        log_fn=lambda _msg: None,
    )

    assert total == 1
    assert fired == ["cost_root_cause_correlator"]
    # Ledger persists the dispatch.
    assert subscriber.has_fired(
        shared, generator_id="cost_root_cause_correlator", signal_id=sig.id
    )


def test_run_loop_skips_already_handled_signals(tmp_path: Path, monkeypatch) -> None:
    """If the ledger already records the (gen, signal) fire, run_loop must
    not re-invoke the runner — survives daemon restart."""
    shared = tmp_path / "evolve"
    sig = _write_firing_signal(shared, signal_id="x", signal_type="cost_spike")

    # Pre-populate the ledger as if a prior daemon instance had dispatched.
    subscriber.record_fire(
        shared,
        generator_id="cost_root_cause_correlator",
        signal_id=sig.id,
        signal_type="cost_spike",
        proposals_ingested=1,
    )

    monkeypatch.setattr(
        subscriber,
        "build_subscription_map",
        lambda *, generators_code_dir, records_dir: {
            "cost_spike": ["cost_root_cause_correlator"],
        },
    )
    monkeypatch.setattr(subscriber, "bootstrap_seen_set", lambda _sd: set())

    invocations: list = []
    import generator_runner

    monkeypatch.setattr(
        generator_runner,
        "run_one_generator",
        lambda *args, **kwargs: invocations.append(args) or 0,
    )

    clock = {"t": 0.0}

    total = subscriber.run_loop(
        shared,
        network_config={},
        generators_code_dir=tmp_path / "g",
        records_dir=tmp_path / "r",
        poll_interval_seconds=1.0,
        stop_after_seconds=0.5,
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        now_fn=lambda: clock["t"],
        log_fn=lambda _msg: None,
    )

    assert total == 0
    assert invocations == [], "ledger hit must short-circuit"
