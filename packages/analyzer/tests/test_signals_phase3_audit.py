"""tests/test_signals_phase3_audit.py — Phase 3 audit-cluster cutover.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 3).

Covers:
  - Each Finding (critical or warn) → one Signal, with the right
    flavor (always maintenance), severity (critical→alert, warn→warn),
    scope (per-bot when bot_id set, else pod), and producer ("audit")
  - Repeated audit runs dedup the same finding into a single rolling
    Signal (observation_count bumps)
  - When a finding clears on the next run, sweep_resolve archives the
    Signal — no manual cleanup
  - Telegram alert records as a Delivery on each critical Signal
    (single dispatch path; spec §10 calls this the "single delivery
    path, not two" requirement)
  - Page-on-transition (Phase 5b): a standing finding re-observed on a
    later run pages nothing and records nothing — the firing Signal on
    the Alerts page IS the standing record
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from audit import (  # noqa: E402
    Finding,
    _audit_signature,
    dispatch_findings,
)
from signals import store as signals_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    """Audit's _send_security_alert hits Telegram on real runs — stub it.

    True is the delivered outcome: the return value gates whether the page
    is recorded as a successful delivery on the Signal.
    """
    monkeypatch.setattr("audit._send_security_alert", lambda *a, **kw: True)
    monkeypatch.setattr("audit._send_telegram_direct", lambda *a, **kw: True)


def _critical(category: str, bot_id: str | None, message: str) -> Finding:
    return Finding(level="critical", finding_kind="event", category=category,
                   bot_id=bot_id, message=message, detail="")


def _warn(category: str, bot_id: str | None, message: str) -> Finding:
    return Finding(level="warn", category=category, bot_id=bot_id,
                   message=message, detail="")


# ─────────────────────────────────────────────────────────────────────────────
# Each finding → one Signal with the right shape
# ─────────────────────────────────────────────────────────────────────────────


def test_critical_finding_emits_alert_severity_signal(tmp_path):
    findings = [_critical("identity", "admin_bot", "ssh key 0644 — must be 0600")]
    dispatch_findings(findings, tmp_path, config={}, dry_run=False)

    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.flavor == "maintenance"
    assert sig.severity == "alert"
    assert sig.scope == "bot"
    assert sig.bot_id == "admin_bot"
    assert sig.type == "audit_identity"
    assert "ssh key" in sig.body


def test_signal_title_carries_distinguishing_message(tmp_path):
    """Distinct findings on the same (category, bot) must produce distinct
    titles. Earlier shape was "Config warn on security_bot" for every config
    finding — four legitimately different findings on one bot all looked
    like duplicates in the Alerts UI.
    """
    findings = [
        _warn("config", "security_bot",
              "security_bot: cron job 'security_bot-task-runner' uses sessionTarget=main"),
        _warn("config", "security_bot",
              "security_bot: new cron job not in baseline: 'usage-alert-dispatch'"),
    ]
    dispatch_findings(findings, tmp_path, config={}, dry_run=False)

    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    titles = {s.title for s in sigs}
    assert len(titles) == 2, f"expected distinct titles, got {titles!r}"
    # And neither title should be the old generic "Config warn on security_bot"
    for t in titles:
        assert "Config warn on security_bot" != t


def test_signal_title_strips_redundant_bot_prefix(tmp_path):
    """Audit messages typically start with the bot_id — strip it from the
    title so we don't get "security_bot: security_bot: ..." once the bot chip already
    shows the bot."""
    findings = [
        _warn("config", "security_bot", "security_bot: cron job uses sessionTarget=main"),
    ]
    dispatch_findings(findings, tmp_path, config={}, dry_run=False)
    [sig] = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert not sig.title.lower().startswith("security_bot:")


def test_signal_title_truncates_long_messages(tmp_path):
    long_msg = "x" * 200
    findings = [_warn("config", "team_bot_a", long_msg)]
    dispatch_findings(findings, tmp_path, config={}, dry_run=False)
    [sig] = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sig.title) <= 80
    assert sig.title.endswith("…")
    # Body still has the full message
    assert sig.body == long_msg


def test_warn_finding_emits_warn_severity_signal(tmp_path):
    findings = [_warn("config", "team_bot_a", "openclaw.json missing model field")]
    dispatch_findings(findings, tmp_path, config={}, dry_run=False)

    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    assert sigs[0].severity == "warn"
    assert sigs[0].flavor == "maintenance"


def test_machine_level_finding_is_pod_scoped(tmp_path):
    findings = [_critical("machine", None, "evolve user has no admin sudo grant")]
    dispatch_findings(findings, tmp_path, config={}, dry_run=False)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    assert sigs[0].scope == "pod"
    assert sigs[0].bot_id is None


def test_distinct_findings_get_distinct_signatures(tmp_path):
    findings = [
        _critical("identity", "admin_bot", "ssh key wrong perms"),
        _critical("identity", "admin_bot", "missing recovery key"),
        _critical("config", "admin_bot", "drift unexplained"),
    ]
    dispatch_findings(findings, tmp_path, config={}, dry_run=False)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 3
    signatures = {s.signature for s in sigs}
    assert len(signatures) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Dedup: same finding across runs → one rolling Signal
# ─────────────────────────────────────────────────────────────────────────────


def test_repeat_runs_dedup_same_finding(tmp_path):
    f = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    for _ in range(3):
        dispatch_findings([f], tmp_path, config={}, dry_run=False)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    assert sigs[0].observation_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-resolve: cleared finding auto-archives
# ─────────────────────────────────────────────────────────────────────────────


def test_cleared_finding_auto_resolves_on_next_run(tmp_path):
    f = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")

    # Run 1: finding present
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    actives_1 = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(actives_1) == 1
    sig_id = actives_1[0].id

    # Run 2: clean — fix applied
    dispatch_findings([], tmp_path, config={}, dry_run=False)

    # The previous Signal is archived (resolved), no active audit signals
    actives_2 = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert actives_2 == []
    located = signals_store.find_signal(tmp_path, sig_id)
    assert located is not None
    assert located[0].state == "resolved"


def test_sweep_resolve_only_touches_audit_signals(tmp_path):
    """A signal from a different producer must not be swept by audit's run."""
    signals_store.observe(
        tmp_path,
        signature="evolve_watchdog:calibration_drift:pod",
        producer="evolve_watchdog",
        type="calibration_drift",
        flavor="activity",
        severity="warn",
        scope="pod",
        title="Watchdog drift",
    )
    # Audit run with no findings
    dispatch_findings([], tmp_path, config={}, dry_run=False)

    actives = list(signals_store.iter_active(tmp_path))
    watchdog = [s for s in actives if s.producer == "evolve_watchdog"]
    assert len(watchdog) == 1


def test_partial_clearance_resolves_only_fixed_findings(tmp_path):
    """One critical fixed, another still firing — only the fixed one resolves."""
    f_a = _critical("identity", "admin_bot", "ssh key wrong perms")
    f_b = _critical("config", "team_bot_a", "openclaw.json drift unexplained")

    dispatch_findings([f_a, f_b], tmp_path, config={}, dry_run=False)
    assert len(list(signals_store.iter_active(tmp_path, producer="audit"))) == 2

    # Run with only f_b still present
    dispatch_findings([f_b], tmp_path, config={}, dry_run=False)
    actives = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(actives) == 1
    assert "openclaw.json drift" in actives[0].body


# ─────────────────────────────────────────────────────────────────────────────
# Telegram delivery becomes an audit on the Signal (single delivery path)
# ─────────────────────────────────────────────────────────────────────────────


def test_critical_alert_records_telegram_delivery_on_signal(tmp_path):
    """When the audit alert fires, each critical Signal gets a Delivery
    entry — single audited dispatch path."""
    f = _critical("identity", "admin_bot", "ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)

    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    deliveries = sigs[0].deliveries
    assert len(deliveries) == 1
    assert deliveries[0].channel == "telegram"
    assert deliveries[0].suppressed_reason is None


def test_standing_finding_repeat_run_is_silent(tmp_path):
    """Page-on-transition: a standing finding re-observed on a later run
    pages nothing and records no extra Delivery — the single entry from
    the original page is the whole audit trail for the episode."""
    f = _critical("identity", "admin_bot", "ssh key wrong perms")

    # First run pages and records
    dispatch_findings([f], tmp_path, config={}, dry_run=False)

    # Second run — same finding still true: silent, no bookkeeping
    dispatch_findings([f], tmp_path, config={}, dry_run=False)

    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    deliveries = sigs[0].deliveries
    assert len(deliveries) == 1
    assert deliveries[0].suppressed_reason is None


def test_warn_findings_do_not_trigger_telegram_delivery(tmp_path):
    """Warns don't fire Telegram — but they DO get a Signal."""
    f = _warn("config", "team_bot_a", "minor drift")
    dispatch_findings([f], tmp_path, config={}, dry_run=False)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    assert sigs[0].deliveries == []


# ─────────────────────────────────────────────────────────────────────────────
# Signature stability (the dedup key is the load-bearing invariant)
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_signature_is_stable_across_calls():
    f1 = _critical("identity", "admin_bot", "ssh key wrong perms")
    f2 = _critical("identity", "admin_bot", "ssh key wrong perms")
    assert _audit_signature(f1) == _audit_signature(f2)


def test_audit_signature_differs_on_distinct_findings():
    a = _audit_signature(_critical("identity", "admin_bot", "msg A"))
    b = _audit_signature(_critical("identity", "admin_bot", "msg B"))
    c = _audit_signature(_critical("config", "admin_bot", "msg A"))
    d = _audit_signature(_critical("identity", "team_bot_a", "msg A"))
    assert len({a, b, c, d}) == 4


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run does NOT emit signals (caller is debugging, not deciding)
# ─────────────────────────────────────────────────────────────────────────────


def test_dry_run_does_not_emit_signals(tmp_path):
    f = _critical("identity", "admin_bot", "ssh key wrong perms")
    dispatch_findings([f], tmp_path, config={}, dry_run=True)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert sigs == []
