"""tests/test_audit_perm_flap_hysteresis.py — audit producer flap hysteresis (D2).

Workstream D2 of the subscription-digest-default spec. The audit producer
(audit.py) emits ``security.audit_finding`` Signals but historically never
consulted the flap hysteresis gate that the sysadmin-watchdog ACL detector and
the pod-perms drift monitor already use. So a benign perm/mode/acl finding that
oscillates fire↔clear (the canonical case: darwin/Linux ``auth-profiles.json
mode=640`` re-clamping ~8×/day on the evo-vps pod, as the OC gateway re-hardens
file modes / clamps the POSIX ACL mask and Evolve auto-restores them) paged the
operator every cycle.

This wires audit's benign perm/mode/acl finding family through
``flap_gate.note_observed`` (dwell N≥2 consecutive runs) while keeping the
must-page floor intact: a CRITICAL world-readable credential exposure
(internal/spec-drift-alert-taxonomy-2026-06-26.md) pages on cycle 1, never dwells.

The flap is a Linux-VPS phenomenon (the POSIX ACL mask only exists on Linux),
so the findings here are shaped like the OC ``fs.*.perms_*`` warnings and the
``permissions corrected to 0600`` self-heal that the Linux audit path emits.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import audit  # noqa: E402
from audit import Finding  # noqa: E402
from signals import flap_gate, settle_gate  # noqa: E402
from signals import store as signals_store  # noqa: E402

_NOW = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    monkeypatch.setattr("audit._send_security_alert", lambda *a, **kw: None)
    monkeypatch.setattr("audit._send_telegram_direct", lambda *a, **kw: None)


# ── Finding shapes (Linux-VPS perm/mode/acl family) ──────────────────────────


def _benign_mode_warn(bot_id: str = "team_bot_a") -> Finding:
    """The headline flap: a benign group-readable mode finding (warn). On Linux
    the OC gateway re-clamps the mode and Evolve restores it, so this fires then
    clears repeatedly."""
    return Finding(
        level="warn",
        category="config",
        bot_id=bot_id,
        message=f"{bot_id} (fs.auth_profiles.perms_readable): "
        f"auth-profiles.json is group-readable",
        detail="mode 0640 (group-class bit); OC re-clamp flap",
    )


def _self_heal_warn(bot_id: str = "team_bot_a") -> Finding:
    """The other live flap shape: the audit's own ``corrected to 0600`` warn."""
    return Finding(
        level="warn",
        category="config",
        bot_id=bot_id,
        message=f"{bot_id}: openclaw.json permissions corrected to 0600",
        detail="was 0640 (token-bearing; must be 0600), chmod 600 succeeded",
    )


def _critical_world_readable(bot_id: str = "team_bot_a") -> Finding:
    """The must-page floor: a GENUINE world-readable credential exposure. OC
    rates this CRITICAL because the OTHER class read bit can never be a mask
    artifact — it must page on cycle 1, never dwell."""
    return Finding(
        level="critical",
        category="config",
        bot_id=bot_id,
        message=f"🔴 CRITICAL: {bot_id} (fs.config.perms_world_readable): "
        f"openclaw.json is world-readable",
        detail="mode 0644; readable by all users on a multi-user host",
    )


def _non_perm_warn(bot_id: str = "team_bot_a") -> Finding:
    return Finding(
        level="warn", category="config", bot_id=bot_id,
        message=f"{bot_id}: openclaw.json missing model field", detail="",
    )


def _active(shared_dir: Path) -> list:
    return list(signals_store.iter_active(shared_dir, producer="audit"))


def _emit(shared_dir, criticals, warns, now):
    return audit._emit_signals_from_findings(criticals, warns, shared_dir, now=now)


# ── Classifier ───────────────────────────────────────────────────────────────


def test_classifier_benign_perm_warn_is_flap_prone():
    assert audit._is_flap_prone_perm_finding(_benign_mode_warn()) is True
    assert audit._is_flap_prone_perm_finding(_self_heal_warn()) is True


def test_classifier_critical_exposure_is_never_flap_prone():
    """The must-page floor: a CRITICAL world-readable exposure is NOT flap-prone
    even though its text matches the perm family — severity wins."""
    assert audit._is_flap_prone_perm_finding(_critical_world_readable()) is False


def test_classifier_excludes_bringup_unreadable_and_non_perm():
    # Bring-up transient (.zshrc unreadable) belongs to the settle gate, not flap.
    bringup = Finding(
        level="warn", category="identity", bot_id="team_bot_a",
        message="team_bot_a: .zshrc unreadable",
        detail="audit user lacks ACL/sudo read for the bot's .zshrc",
    )
    assert audit._is_bringup_transient_finding(bringup) is True
    assert audit._is_flap_prone_perm_finding(bringup) is False
    # A plain non-perm warn is unaffected (emits immediately, no dwell).
    assert audit._is_flap_prone_perm_finding(_non_perm_warn()) is False


# ── Core hysteresis: benign perm finding dwells N≥2 ──────────────────────────


def test_benign_mode_warn_dwells_two_cycles_then_pages(tmp_path):
    settle_gate.mark_settled(tmp_path)  # past bring-up; isolate the flap behavior
    warn = _benign_mode_warn()

    # Cycle 1: real this cycle but dwelling — NO Signal written.
    _emit(tmp_path, [], [warn], now=_NOW)
    assert _active(tmp_path) == []
    assert flap_gate._pending_path(tmp_path, audit._audit_signature(warn)).exists()

    # Cycle 2: persists a 2nd consecutive cycle → promotes to a firing Signal.
    _emit(tmp_path, [], [warn], now=_NOW + timedelta(hours=1))
    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "warn"
    assert sigs[0].type == "audit_config"


def test_single_cycle_flap_never_pages(tmp_path):
    """fire → clear → fire (non-consecutive) must never page: note_cleared_absent
    resets the dwell on the clear cycle so the count never reaches N=2."""
    settle_gate.mark_settled(tmp_path)
    warn = _benign_mode_warn()

    _emit(tmp_path, [], [warn], now=_NOW)                       # dwell 1/2
    assert _active(tmp_path) == []
    # Condition gone this cycle → the audit run carries no perm finding.
    _emit(tmp_path, [], [], now=_NOW + timedelta(hours=1))      # reset
    assert not flap_gate._pending_path(
        tmp_path, audit._audit_signature(warn)
    ).exists()
    # Reappears once more — back at 1/2, still withheld.
    _emit(tmp_path, [], [warn], now=_NOW + timedelta(hours=2))  # dwell 1/2 again
    assert _active(tmp_path) == []


# ── Must-page floor: CRITICAL exposure pages on cycle 1 ──────────────────────


def test_critical_world_readable_pages_immediately(tmp_path):
    """The CRITICAL credential-exposure floor: a single cycle pages, no dwell."""
    settle_gate.mark_settled(tmp_path)
    crit = _critical_world_readable()

    _emit(tmp_path, [crit], [], now=_NOW)
    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "alert"
    # No flap-dwell ledger entry was ever written for the critical finding.
    assert not flap_gate._pending_path(
        tmp_path, audit._audit_signature(crit)
    ).exists()


def test_critical_exposure_pages_even_while_benign_sibling_dwells(tmp_path):
    """A real exposure must not be held hostage by a benign sibling's dwell."""
    settle_gate.mark_settled(tmp_path)
    crit = _critical_world_readable("admin_bot")
    benign = _benign_mode_warn("team_bot_a")

    _emit(tmp_path, [crit], [benign], now=_NOW)
    sigs = _active(tmp_path)
    # Critical fired immediately; benign is still dwelling (not yet present).
    assert len(sigs) == 1
    assert sigs[0].bot_id == "admin_bot"
    assert sigs[0].severity == "alert"


# ── Regression: non-perm warns still emit immediately ────────────────────────


def test_non_perm_warn_still_emits_immediately(tmp_path):
    settle_gate.mark_settled(tmp_path)
    _emit(tmp_path, [], [_non_perm_warn()], now=_NOW)
    assert len(_active(tmp_path)) == 1


# ── End-to-end via dispatch_findings (live wall-clock, no now injection) ──────


def test_dispatch_findings_dwells_benign_then_pages(tmp_path):
    settle_gate.mark_settled(tmp_path)
    warn = _benign_mode_warn()
    # Two back-to-back runs (wall clock ~identical → not stale): 1st dwells, 2nd pages.
    audit.dispatch_findings([warn], tmp_path, config={}, dry_run=False)
    assert _active(tmp_path) == []
    audit.dispatch_findings([warn], tmp_path, config={}, dry_run=False)
    assert len(_active(tmp_path)) == 1
