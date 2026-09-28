"""tests/test_pod_bringup_settle.py — fresh-pod bring-up settle window.

Falsifiable proof artifact for internal/spec-pod-bringup-settle-2026-06-23.md.

Simulates a bring-up: with no settled sentinel present, transient ACL /
"unreadable" / Watchdog-ACL-restore outputs are WITHHELD across all three
producers (security audit, sysadmin watchdog, infra audit); after the settled
sentinel is written, they fire normally. A critical (alert-severity) finding is
NEVER withheld during the unsettled window.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from signals import settle_gate  # noqa: E402

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Predicate
# ─────────────────────────────────────────────────────────────────────────────


def test_no_markers_is_settled(tmp_path):
    """Existing pods / non-fresh paths (no bring-up marker) fail OPEN to settled."""
    assert settle_gate.is_pod_settled(tmp_path) is True


def test_fresh_bringup_marker_is_unsettled(tmp_path):
    """A fresh bring-up marker with no settled marker → unsettled (in window)."""
    settle_gate.mark_bringup_started(tmp_path)
    assert settle_gate.is_pod_settled(tmp_path, now=_NOW) is False


def test_settled_marker_overrides(tmp_path):
    settle_gate.mark_bringup_started(tmp_path)
    settle_gate.mark_settled(tmp_path)
    assert settle_gate.is_pod_settled(tmp_path, now=_NOW) is True


def test_window_cap_expires(tmp_path):
    """Past the cap, a pod with only a (stale) bring-up marker is settled —
    a wizard that died before writing the settled marker can't suppress forever."""
    settle_gate.mark_bringup_started(tmp_path)
    # The marker is written at real-now; assert relative to its own start so the
    # test is clock-independent.
    started = settle_gate._read_started_at(tmp_path / "pod-bringup.json")
    assert started is not None
    past_cap = started + timedelta(seconds=settle_gate.SETTLE_WINDOW_SECONDS + 1)
    in_window = started + timedelta(seconds=1)
    assert settle_gate.is_pod_settled(tmp_path, now=past_cap) is True
    assert settle_gate.is_pod_settled(tmp_path, now=in_window) is False


def test_mark_bringup_started_is_write_once(tmp_path):
    settle_gate.mark_bringup_started(tmp_path)
    first = settle_gate._read_started_at(tmp_path / "pod-bringup.json")
    # A second call must not reset the clock.
    settle_gate.mark_bringup_started(tmp_path)
    second = settle_gate._read_started_at(tmp_path / "pod-bringup.json")
    assert first == second


# ─────────────────────────────────────────────────────────────────────────────
# Gate: severity + transient interaction
# ─────────────────────────────────────────────────────────────────────────────


def _unsettled(tmp_path):
    settle_gate.mark_bringup_started(tmp_path)
    return tmp_path


def test_transient_warn_withheld_while_unsettled(tmp_path):
    _unsettled(tmp_path)
    assert settle_gate.should_withhold(tmp_path, severity="warn", transient=True, now=_NOW) is True


def test_alert_never_withheld(tmp_path):
    """Genuinely critical findings always fire, even when transient + unsettled."""
    _unsettled(tmp_path)
    assert settle_gate.should_withhold(tmp_path, severity="alert", transient=True, now=_NOW) is False


def test_non_transient_never_withheld(tmp_path):
    _unsettled(tmp_path)
    assert settle_gate.should_withhold(tmp_path, severity="warn", transient=False, now=_NOW) is False


def test_transient_fires_after_settle(tmp_path):
    _unsettled(tmp_path)
    settle_gate.mark_settled(tmp_path)
    assert settle_gate.should_withhold(tmp_path, severity="warn", transient=True, now=_NOW) is False


# ─────────────────────────────────────────────────────────────────────────────
# Producer 1 — security audit (audit.py)
# ─────────────────────────────────────────────────────────────────────────────


def _audit_findings():
    import audit

    transient = audit.Finding(
        level="warn",
        category="identity",
        bot_id="team_bot_a",
        message="team_bot_a: .zshrc unreadable",
        detail="audit user lacks ACL/sudo read for the bot's .zshrc",
    )
    critical = audit.Finding(
        level="critical",
        finding_kind="posture",
        category="machine",
        bot_id=None,
        message="CRITICAL: macOS host firewall is off",
    )
    return audit, transient, critical


def test_audit_classifier_recognizes_transient():
    audit, transient, critical = _audit_findings()
    assert audit._is_bringup_transient_finding(transient) is True
    # Critical-level findings are never transient.
    assert audit._is_bringup_transient_finding(critical) is False


def _run_audit_emit(audit, criticals, warns, shared_dir, monkeypatch):
    """Run _emit_signals_from_findings with observe()/sweep_resolve() recorded."""
    from signals import store as signals_store

    observed: list[dict] = []

    class _Sig:
        id = "sig-test"

    def _fake_observe(_shared, **kwargs):
        observed.append(kwargs)
        return _Sig(), "created"

    monkeypatch.setattr(signals_store, "observe_with_outcome", _fake_observe)
    monkeypatch.setattr(signals_store, "sweep_resolve", lambda *a, **k: [])
    audit._emit_signals_from_findings(criticals, warns, shared_dir)
    return observed


def test_audit_withholds_transient_unreadable_during_bringup(tmp_path, monkeypatch):
    audit, transient, critical = _audit_findings()
    _unsettled(tmp_path)
    observed = _run_audit_emit(audit, [critical], [transient], tmp_path, monkeypatch)
    types = {o["type"] for o in observed}
    # The transient "unreadable" warn is WITHHELD; the critical firewall alert fires.
    assert "audit_identity" not in types
    assert "audit_machine" in types


def test_audit_emits_transient_after_settle(tmp_path, monkeypatch):
    audit, transient, critical = _audit_findings()
    _unsettled(tmp_path)
    settle_gate.mark_settled(tmp_path)
    observed = _run_audit_emit(audit, [critical], [transient], tmp_path, monkeypatch)
    types = {o["type"] for o in observed}
    assert "audit_identity" in types
    assert "audit_machine" in types


# ─────────────────────────────────────────────────────────────────────────────
# Producer 2 — sysadmin watchdog ACL detector (signal + autonomous proposal)
# ─────────────────────────────────────────────────────────────────────────────


def _watchdog_ctx(shared_dir):
    from generators.sysadmin_watchdog.observe import DetectorContext
    from metrics.registry import MetricValue

    def _resolve(name, bot_id, t):
        if name == "acl.evolve_read":
            return MetricValue(value=0.0, confidence=1.0)  # drift present
        return MetricValue(value=1.0, confidence=1.0)

    return DetectorContext(
        bot_id="team_bot_a",
        now=_NOW,
        resolve=_resolve,
        shared_dir=shared_dir,
    )


def test_watchdog_acl_signal_and_proposal_withheld_during_bringup(tmp_path):
    from generators.sysadmin_watchdog.detectors.platform import (
        detect_acl,
        detect_acl_signal,
    )

    _unsettled(tmp_path)
    ctx = _watchdog_ctx(tmp_path)
    # Drift is real (acl.evolve_read == 0.0) but withheld during bring-up.
    assert detect_acl_signal(ctx) is None
    assert detect_acl(ctx) == []


def test_watchdog_acl_fires_after_settle(tmp_path):
    from generators.sysadmin_watchdog.detectors.platform import (
        detect_acl,
        detect_acl_signal,
    )
    from signals import store as signals_store

    _unsettled(tmp_path)
    settle_gate.mark_settled(tmp_path)
    ctx = _watchdog_ctx(tmp_path)
    # Settle no longer withholds — but flap hysteresis (mechanism 2,
    # internal/spec-transient-signal-suppression-2026-06-23.md) now requires the
    # ACL miss to persist across 2 consecutive cycles before it pages. Cycle 1
    # dwells; cycle 2 fires the Signal, and the Proposal follows in lock-step.
    assert detect_acl_signal(ctx) is None
    spec = detect_acl_signal(ctx)
    assert spec is not None and spec["type"] == "acl_drift"
    signals_store.observe(tmp_path, **spec)
    proposals = detect_acl(ctx)
    assert len(proposals) == 1
    assert proposals[0].claim.metric == "acl.evolve_read"


# ─────────────────────────────────────────────────────────────────────────────
# Producer 3 — infra audit ACL finding
# ─────────────────────────────────────────────────────────────────────────────


def test_infra_audit_acl_finding_withheld_then_fires(tmp_path):
    from evolve_admin.applications.infra_audit import (
        _withhold_bot_openclaw_unreadable,
    )

    _unsettled(tmp_path)
    assert _withhold_bot_openclaw_unreadable(tmp_path) is True
    settle_gate.mark_settled(tmp_path)
    assert _withhold_bot_openclaw_unreadable(tmp_path) is False
