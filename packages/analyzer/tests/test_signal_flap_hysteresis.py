"""tests/test_signal_flap_hysteresis.py — self-healing flap hysteresis.

Falsifiable proof artifacts for internal/spec-transient-signal-suppression-2026-06-23.md
(mechanism 2). The bring-up settle half (mechanism 1) is proven separately in
test_pod_bringup_settle.py.

Proof matrix:
  * A transient-prone condition observed once then cleared produces NO alert.
  * A condition that persists across N≥2 consecutive cycles DOES alert.
  * Promotion hands off to signals.store.observe so dedup / reopen still work:
    re-observe bumps observation_count; resolve → re-persist reopens the SAME
    Signal id.
  * The paired ACL-restore Proposal fires in lock-step with its Signal
    (withheld while dwelling, fires once the Signal is active).
  * Non-transient conditions and fail-open paths page immediately.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from signals import flap_gate  # noqa: E402
from signals import store as signals_store  # noqa: E402

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
_SIG = "pod_perms_drift:pod_perms_drift:pod"


def _spec(**overrides) -> dict:
    base = dict(
        signature=_SIG,
        producer="pod_perms_drift",
        type="pod_perms_drift",
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title="pod perms drift",
    )
    base.update(overrides)
    return base


def _producer_cycle(shared_dir: Path, *, present: bool, now: datetime, spec: dict | None = None):
    """Mimic one cycle of a transient sweep-style monitor.

    PRESENT → consult flap_gate; emit via store.observe only when it pages.
    ABSENT  → reset the dwell counter and sweep-resolve any active Signal
    (exactly what pod_perms_drift_monitor does on a clean tick).
    Returns the Signal when it pages this cycle, else None.
    """
    spec = spec or _spec()
    signature = spec["signature"]
    if not present:
        flap_gate.note_cleared(shared_dir, signature)
        for s in list(signals_store.iter_active(shared_dir)):
            if s.signature == signature:
                signals_store.apply_transition(
                    s, "resolved", shared_dir, actor="test", reason="cleared"
                )
        return None
    verdict = flap_gate.note_observed(
        shared_dir, signature=signature, transient=True, type=spec["type"], now=now
    )
    if not verdict.page:
        return None
    return signals_store.observe(shared_dir, **spec)


def _active(shared_dir: Path) -> list:
    return list(signals_store.iter_active(shared_dir))


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


def test_transient_prone_registry():
    assert flap_gate.is_transient_prone("acl_drift") is True
    assert flap_gate.is_transient_prone("pod_perms_drift") is True
    assert flap_gate.is_transient_prone("workspace_unreadable") is True
    assert flap_gate.is_transient_prone("gateway_down") is False
    assert flap_gate.is_transient_prone(None) is False
    # Override wins over the registry both ways.
    assert flap_gate.is_transient_prone("gateway_down", override=True) is True
    assert flap_gate.is_transient_prone("acl_drift", override=False) is False


# ─────────────────────────────────────────────────────────────────────────────
# Core hysteresis
# ─────────────────────────────────────────────────────────────────────────────


def test_single_observation_then_clear_never_pages(tmp_path):
    """The headline flap case: observed once, gone next cycle → never paged."""
    # Cycle 1: condition present → dwelling, withheld.
    assert _producer_cycle(tmp_path, present=True, now=_NOW) is None
    assert _active(tmp_path) == []
    # Cycle 2: condition gone → reset; still no Signal ever written.
    assert _producer_cycle(tmp_path, present=False, now=_NOW) is None
    assert _active(tmp_path) == []
    # The pending dwell entry is gone (reset), not lingering toward a threshold.
    assert not flap_gate._pending_path(tmp_path, _SIG).exists()


def test_persist_two_cycles_pages(tmp_path):
    """Persisting across N=2 consecutive cycles promotes to a firing Signal."""
    assert _producer_cycle(tmp_path, present=True, now=_NOW) is None  # dwell 1/2
    sig = _producer_cycle(tmp_path, present=True, now=_NOW)           # dwell 2/2 → page
    assert sig is not None
    assert sig.state == "firing"
    assert sig.observation_count == 1
    assert sig.signature == _SIG
    # The ledger may linger after promotion (reaped on the next clear/stale
    # sweep); what matters is the already-firing short-circuit, proven next.


def test_already_firing_reobserve_bumps_count_via_store(tmp_path):
    """Once firing, re-observation short-circuits the dwell and bumps the store."""
    _producer_cycle(tmp_path, present=True, now=_NOW)
    first = _producer_cycle(tmp_path, present=True, now=_NOW)  # promote
    assert first is not None
    # Next cycle, still present: already-firing → page immediately, store dedups.
    second = _producer_cycle(tmp_path, present=True, now=_NOW)
    assert second is not None
    assert second.id == first.id
    assert second.observation_count == 2


def test_resolve_then_persist_reopens_same_signal(tmp_path):
    """Reopen semantics survive: resolve, then a fresh 2-cycle dwell reopens it."""
    _producer_cycle(tmp_path, present=True, now=_NOW)
    fired = _producer_cycle(tmp_path, present=True, now=_NOW)
    assert fired is not None
    # Condition clears → Signal resolves.
    _producer_cycle(tmp_path, present=False, now=_NOW)
    assert _active(tmp_path) == []
    # Reappears: must re-dwell (single re-appearance does NOT immediately reopen).
    assert _producer_cycle(tmp_path, present=True, now=_NOW) is None
    assert _active(tmp_path) == []
    # Persists a 2nd cycle → promotes → store.observe REOPENS the same id.
    reopened = _producer_cycle(tmp_path, present=True, now=_NOW)
    assert reopened is not None
    assert reopened.id == fired.id, "reopen within window must reuse the Signal id"
    assert reopened.state == "firing"


def test_note_cleared_resets_dwell(tmp_path):
    """A gap (clear) between observations resets the count — no silent carry-over."""
    flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=_NOW)
    flap_gate.note_cleared(tmp_path, _SIG)
    # After a reset, the next single observation is back at 1/2 → still dwelling.
    v = flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=_NOW)
    assert v.page is False
    assert v.count == 1


def test_note_cleared_absent_resets_tracked_but_absent(tmp_path):
    """The dynamic-signature-set analogue of note_cleared: a sweep-style
    producer enumerates the signatures PRESENT this run; every tracked entry
    NOT present is reset (the consecutive-cycle reset)."""
    present = "audit:config:bot_a:present"
    gone = "audit:config:bot_b:gone"
    for sig in (present, gone):
        flap_gate.note_observed(
            tmp_path, signature=sig, transient=True, type="audit_perm_flap", now=_NOW
        )
    cleared = flap_gate.note_cleared_absent(
        tmp_path, {present}, type_filter="audit_perm_flap"
    )
    assert cleared == [gone]
    assert flap_gate._pending_path(tmp_path, present).exists()
    assert not flap_gate._pending_path(tmp_path, gone).exists()


def test_note_cleared_absent_type_filter_isolates_producers(tmp_path):
    """The pending dir is shared — a producer's clear-sweep must not reset
    another producer's dwell entry (filtered by the stored type)."""
    audit_sig = "audit:config:bot_a:perm"
    other_sig = "pod_perms_drift:pod_perms_drift:pod"
    flap_gate.note_observed(
        tmp_path, signature=audit_sig, transient=True, type="audit_perm_flap", now=_NOW
    )
    flap_gate.note_observed(
        tmp_path, signature=other_sig, transient=True, type="pod_perms_drift", now=_NOW
    )
    # Audit's clear-sweep (empty present set) must leave pod_perms_drift alone.
    cleared = flap_gate.note_cleared_absent(
        tmp_path, set(), type_filter="audit_perm_flap"
    )
    assert cleared == [audit_sig]
    assert not flap_gate._pending_path(tmp_path, audit_sig).exists()
    assert flap_gate._pending_path(tmp_path, other_sig).exists()


def test_stale_pending_does_not_instant_promote(tmp_path):
    """A crash residue (old pending entry) must not promote a fresh incident
    on its first observation — it's treated as stale and re-dwells."""
    old = datetime(2026, 6, 23, 6, 0, tzinfo=timezone.utc)  # >3h before _NOW
    flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=old)
    # New incident hours later: the stale count=1 is ignored → back to 1/2.
    v = flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=_NOW)
    assert v.page is False
    assert v.count == 1


def test_promotion_leaves_ledger_so_failed_store_write_retries(tmp_path):
    """If the producer can't write the Signal on the promotion cycle, the next
    cycle re-promotes immediately rather than restarting the dwell."""
    flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=_NOW)
    v2 = flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=_NOW)
    assert v2.page is True  # promoted
    # Simulate the store.observe() having failed: no firing Signal was written.
    assert _active(tmp_path) == []
    assert flap_gate._pending_path(tmp_path, _SIG).exists()  # ledger retained
    # Next cycle: no active signal, ledger persisted → re-promote, not re-dwell.
    v3 = flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=_NOW)
    assert v3.page is True
    assert v3.count == 3


def test_non_transient_pages_immediately(tmp_path):
    v = flap_gate.note_observed(
        tmp_path, signature="sysadmin_watchdog:gateway_down:b", transient=False, type="gateway_down"
    )
    assert v.page is True
    assert v.reason == "not-transient"


def test_dwell_cycles_one_pages_immediately(tmp_path):
    v = flap_gate.note_observed(
        tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", dwell_cycles=1
    )
    assert v.page is True


def test_ledger_write_failure_fails_open(tmp_path, monkeypatch):
    """If the dwell ledger can't be written, page rather than swallow."""
    monkeypatch.setattr(
        flap_gate, "atomic_write_json",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    v = flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift")
    assert v.page is True
    assert v.reason == "ledger-write-failed"


def test_sweep_stale_reaps_old_pending(tmp_path):
    """Pending entries not re-observed within the window are reaped as a backstop."""
    old = datetime(2026, 6, 23, 6, 0, tzinfo=timezone.utc)  # 6h before _NOW
    flap_gate.note_observed(tmp_path, signature=_SIG, transient=True, type="pod_perms_drift", now=old)
    assert flap_gate._pending_path(tmp_path, _SIG).exists()
    dropped = flap_gate.sweep_stale(tmp_path, now=_NOW)
    assert _SIG in dropped
    assert not flap_gate._pending_path(tmp_path, _SIG).exists()


def test_signal_is_active_tracks_store(tmp_path):
    assert flap_gate.signal_is_active(tmp_path, _SIG) is False
    signals_store.observe(tmp_path, **_spec())
    assert flap_gate.signal_is_active(tmp_path, _SIG) is True


# ─────────────────────────────────────────────────────────────────────────────
# Real producer — Sysadmin Watchdog ACL detector (Signal + autonomous Proposal)
# ─────────────────────────────────────────────────────────────────────────────


def _watchdog_ctx(tmp_path, *, drift: bool):
    from generators.sysadmin_watchdog.observe import DetectorContext
    from metrics.registry import MetricValue

    value = 0.0 if drift else 1.0

    def _resolve(name, bot_id, now):
        return MetricValue(value=value, confidence=1.0)

    return DetectorContext(
        bot_id="team_bot_a",
        now=_NOW,
        resolve=_resolve,
        shared_dir=tmp_path,
    )


def _settle(tmp_path):
    from signals import settle_gate

    settle_gate.mark_settled(tmp_path)


def test_watchdog_acl_dwells_one_cycle_then_fires_with_proposal_lockstep(tmp_path):
    from generators.sysadmin_watchdog.detectors.platform import (
        detect_acl,
        detect_acl_signal,
    )

    _settle(tmp_path)  # past bring-up; isolate the flap behavior
    ctx = _watchdog_ctx(tmp_path, drift=True)

    # Cycle 1: ACL miss is real but dwelling — neither Signal nor Proposal page.
    assert detect_acl_signal(ctx) is None
    assert detect_acl(ctx) == []

    # Cycle 2: persists → Signal spec emitted. Mirror the runner writing it
    # (observe_signals runs before observe), then the Proposal fires lock-step.
    spec = detect_acl_signal(ctx)
    assert spec is not None and spec["type"] == "acl_drift"
    signals_store.observe(tmp_path, **spec)
    proposals = detect_acl(ctx)
    assert len(proposals) == 1
    assert proposals[0].claim.metric == "acl.evolve_read"


def test_watchdog_acl_single_cycle_flap_resets(tmp_path):
    from generators.sysadmin_watchdog.detectors.platform import detect_acl_signal

    _settle(tmp_path)
    drift_ctx = _watchdog_ctx(tmp_path, drift=True)
    healthy_ctx = _watchdog_ctx(tmp_path, drift=False)

    assert detect_acl_signal(drift_ctx) is None       # dwell 1/2
    assert detect_acl_signal(healthy_ctx) is None      # healthy → note_cleared resets
    # A subsequent single drift cycle is back at 1/2 (reset), NOT promoted.
    assert detect_acl_signal(drift_ctx) is None
    actives = [s for s in _active(tmp_path) if s.type == "acl_drift"]
    assert actives == []
