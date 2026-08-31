"""tests/test_signals_stale_ttl.py — stale-firing TTL backstop.

Track 1.2 of internal/review-alerts-findings-noise-2026-08-30.md: the
founding spec (spec-alerts-signal-store-2026-05-07.md §6) assumed
event-style monitors "rely on TTL or manual resolution", but the TTL was
never built — any producer that forgets its sweep_resolve leaks firing
signals forever. These tests pin the backstop:

- a firing signal older than the TTL window is resolved with the
  explicit backstop reason (through the state machine, not file edits);
- a recently-observed firing signal is untouched;
- snoozed signals are never touched (a snooze is an operator statement);
- TTL-exempt types (operator-acked-only reminders) are never touched;
- the leaking-producer meta-signal fires with per-producer counts and
  sweep-resolves once a retention run finds that producer clean;
- prune_retention() wires the sweep (and 0 disables it).

Clock-coupling: signals are backdated by rewriting ``last_observed_at``
through ``signals_store.write_signal`` (a store API, not a hand edit);
the margins (14d TTL vs 30d/1d backdates) dwarf any test-run wall-clock
drift, and ``sweep_stale_firing`` takes an injectable ``now`` used by
the disable/threshold tests.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from schema.signal import Signal  # noqa: E402
from signals import retention, stale_ttl, store as signals_store  # noqa: E402


def _observe(
    shared_dir: Path,
    *,
    producer: str = "leaky_prod",
    type_: str = "leaky_type",
    scope_key: str = "all",
) -> Signal:
    return signals_store.observe(
        shared_dir,
        signature=f"{producer}:{type_}:{scope_key}",
        producer=producer,
        type=type_,
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title=f"{producer} {type_}",
    )


def _backdate(shared_dir: Path, sig: Signal, *, days: int) -> None:
    """Rewrite last_observed_at N days into the past via the store API."""
    sig.last_observed_at = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    signals_store.write_signal(sig, shared_dir)


def _reload(shared_dir: Path, signal_id: str) -> tuple[Signal, str]:
    located = signals_store.find_signal(shared_dir, signal_id)
    assert located is not None
    sig, _path, subdir = located
    return sig, subdir


def test_stale_firing_signal_resolved_with_backstop_reason(tmp_path):
    sig = _observe(tmp_path)
    _backdate(tmp_path, sig, days=30)

    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result.stale_resolved == 1
    assert result.resolved_by_producer == {"leaky_prod": 1}

    fresh, subdir = _reload(tmp_path, sig.id)
    assert fresh.state == "resolved"
    assert subdir == "archived"
    assert fresh.resolved_at is not None
    last = fresh.state_history[-1]
    assert last.to_state == "resolved"
    assert last.actor == "signals_retention"
    assert "TTL backstop" in last.reason
    assert "not re-observed for 14d" in last.reason
    assert "sweep_resolve" in last.reason


def test_recent_firing_signal_untouched(tmp_path):
    sig = _observe(tmp_path)
    _backdate(tmp_path, sig, days=1)

    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result.stale_resolved == 0

    fresh, subdir = _reload(tmp_path, sig.id)
    assert fresh.state == "firing"
    assert subdir == "firing"


def test_snoozed_signal_untouched(tmp_path):
    """Snooze is an operator statement — the TTL never overrides it,
    however stale the last observation."""
    sig = _observe(tmp_path)
    until = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
    signals_store.apply_transition(
        sig, "snoozed", tmp_path, actor="user:test", snoozed_until=until,
    )
    _backdate(tmp_path, sig, days=60)

    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result.stale_resolved == 0

    fresh, subdir = _reload(tmp_path, sig.id)
    assert fresh.state == "snoozed"
    assert subdir == "snoozed"


def test_exempt_type_untouched(tmp_path):
    """Operator-acked-only types in TTL_EXEMPT_TYPES never age out."""
    sig = _observe(
        tmp_path,
        producer="oc_upgrade_runtime_notes_reminder",
        type_="runtime_notes_review_due",
        scope_key="2026.7.1-2",
    )
    _backdate(tmp_path, sig, days=120)

    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result.stale_resolved == 0
    assert result.exempt_skipped == 1

    fresh, subdir = _reload(tmp_path, sig.id)
    assert fresh.state == "firing"
    assert subdir == "firing"


def test_ttl_zero_disables_backstop(tmp_path):
    sig = _observe(tmp_path)
    _backdate(tmp_path, sig, days=365)

    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=0)
    assert result.stale_resolved == 0
    fresh, _ = _reload(tmp_path, sig.id)
    assert fresh.state == "firing"


def test_unparseable_last_observed_kept(tmp_path):
    """A read failure must fail toward keeping, never toward archiving."""
    sig = _observe(tmp_path)
    sig.last_observed_at = "not-a-timestamp"
    signals_store.write_signal(sig, tmp_path)

    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result.stale_resolved == 0
    fresh, _ = _reload(tmp_path, sig.id)
    assert fresh.state == "firing"


def test_leak_meta_signal_per_producer_counts_and_auto_resolve(tmp_path):
    # Two stale signals from one producer, one from another.
    a1 = _observe(tmp_path, producer="prod_a", type_="type_x", scope_key="1")
    a2 = _observe(tmp_path, producer="prod_a", type_="type_y", scope_key="2")
    b1 = _observe(tmp_path, producer="prod_b", type_="type_z", scope_key="3")
    for sig in (a1, a2, b1):
        _backdate(tmp_path, sig, days=30)

    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result.stale_resolved == 3
    assert result.resolved_by_producer == {"prod_a": 2, "prod_b": 1}
    assert result.leak_signals_emitted == 2
    assert result.leak_signals_resolved == 0

    leaks = {
        sig.details["leaking_producer"]: sig
        for sig in signals_store.iter_active(
            tmp_path, producer=stale_ttl.LEAK_PRODUCER,
        )
    }
    assert set(leaks) == {"prod_a", "prod_b"}
    leak_a = leaks["prod_a"]
    assert leak_a.type == stale_ttl.LEAK_TYPE
    assert leak_a.severity == "warn"
    assert leak_a.details["aged_out_count"] == 2
    assert leak_a.details["aged_out_types"] == ["type_x", "type_y"]
    # Value-free signature: per-producer only — no counts, no dates.
    assert leak_a.signature == "signals_retention:producer_signal_leak:prod_a"
    assert leaks["prod_b"].details["aged_out_count"] == 1

    # Next run: nothing new ages out, so both leak signals sweep-resolve.
    result2 = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result2.stale_resolved == 0
    assert result2.leak_signals_emitted == 0
    assert result2.leak_signals_resolved == 2
    assert (
        list(
            signals_store.iter_active(
                tmp_path, producer=stale_ttl.LEAK_PRODUCER,
            )
        )
        == []
    )


def test_leak_meta_signal_dedups_while_leak_persists(tmp_path):
    """A producer still leaking on the next run bumps ONE meta-signal
    (value-free signature) instead of minting siblings."""
    s1 = _observe(tmp_path, producer="prod_a", type_="type_x", scope_key="1")
    _backdate(tmp_path, s1, days=30)
    stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)

    s2 = _observe(tmp_path, producer="prod_a", type_="type_x", scope_key="2")
    _backdate(tmp_path, s2, days=30)
    result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=14)
    assert result.stale_resolved == 1

    leaks = list(
        signals_store.iter_active(tmp_path, producer=stale_ttl.LEAK_PRODUCER)
    )
    assert len(leaks) == 1
    assert leaks[0].observation_count == 2


def test_prune_retention_runs_ttl_backstop(tmp_path):
    sig = _observe(tmp_path)
    _backdate(tmp_path, sig, days=30)

    result = retention.prune_retention(tmp_path)
    assert result.stale_firing_resolved == 1
    assert result.leak_signals_emitted == 1
    fresh, subdir = _reload(tmp_path, sig.id)
    assert fresh.state == "resolved"
    assert subdir == "archived"


def test_prune_retention_ttl_disable(tmp_path):
    sig = _observe(tmp_path)
    _backdate(tmp_path, sig, days=365)

    result = retention.prune_retention(tmp_path, stale_firing_ttl_days=0)
    assert result.stale_firing_resolved == 0
    fresh, _ = _reload(tmp_path, sig.id)
    assert fresh.state == "firing"
