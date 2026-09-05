"""tests/test_audit_identity_flap_hysteresis.py — identity-hash flap dwell (R-2).

R-2 of internal/design-security-alert-fatigue-2026-08-31.md. The audit's
identity check (``audit_identity``) compares each bot's live
SOUL.md/AGENTS.md/HEARTBEAT.md against the last git-backup commit. A bot
legitimately editing its own AGENTS.md opens a mismatch window until the next
backup commit, so the CRITICAL "hash mismatch vs git backup" finding flaps
in/out — and because critical alert delivery is a whole-batch broadcast
fingerprinted over the entire sorted finding list, every flap re-broadcast all
~25 standing criticals to the operator (~2 full re-broadcasts/day observed).

This routes the identity-hash mismatch family through the existing
``flap_gate`` dwell (N≥2 consecutive runs) under its own ledger type
(``audit_identity_flap``), and excludes still-dwelling criticals from the
batched Telegram alert so a one-run blip can no longer churn the batch
fingerprint. Contract:

  * a mismatch persisting 2+ consecutive runs still fires CRITICAL;
  * a one-run blip (edit-then-backup race) is withheld and never pages;
  * healing resets the dwell counter (clear-sweep);
  * withheld findings never enter kept_signatures, so sweep_resolve leaves
    prior signals alone;
  * the perm/mode/acl flap family and every other finding family (including
    credential-exposure criticals) are untouched.
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

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    monkeypatch.setattr("audit._send_security_alert", lambda *a, **kw: True)
    monkeypatch.setattr("audit._send_telegram_direct", lambda *a, **kw: True)


def _capture_page(sent: list[str]):
    """Stub for ``_send_security_alert`` that records the page it would send.

    Returns True — the return value is the delivery outcome the paging gate
    records on the Signal, and these tests exercise the delivered path.
    """
    def _send(msg, *a, **kw):
        sent.append(msg)
        return True
    return _send


# ── Finding shapes ────────────────────────────────────────────────────────────


def _identity_mismatch(bot_id: str = "team_bot_a", fname: str = "AGENTS.md") -> Finding:
    """The headline flap: audit_identity's CRITICAL live-vs-backup mismatch."""
    return Finding(
        level="critical",
        # R-3 classification: identity-hash mismatch is posture.
        finding_kind="posture",
        category="identity",
        bot_id=bot_id,
        message=f"🔴 CRITICAL: {bot_id} {fname} hash mismatch vs git backup",
        detail="live=aaaaaaaaaaaa backup=bbbbbbbbbbbb",
    )


def _procedure_mismatch(bot_id: str = "admin_bot") -> Finding:
    """The primary-bot EVOLVE_PROCEDURE_FILES shape — same race, warn level."""
    return Finding(
        level="warn",
        category="identity",
        bot_id=bot_id,
        message=f"{bot_id}: procedures/security-cve-scan.md changed outside proposal pipeline",
        detail="live=aaaaaaaaaaaa backup=bbbbbbbbbbbb",
    )


def _zshrc_baseline_critical(bot_id: str = "admin_bot") -> Finding:
    """A DIFFERENT identity family (baseline drift, not the backup race) —
    must never dwell."""
    return Finding(
        level="critical",
        # R-3 classification: .zshrc baseline drift is posture.
        finding_kind="posture",
        category="identity",
        bot_id=bot_id,
        message=f"🔴 CRITICAL: {bot_id} .zshrc hash changed since baseline",
        detail="baseline=abc current=def",
    )


def _credential_exposure_critical(bot_id: str = "team_bot_a") -> Finding:
    """The must-page floor: genuine world-readable credential exposure."""
    return Finding(
        level="critical",
        # R-3 classification: credential exposure is the canonical event.
        finding_kind="event",
        category="config",
        bot_id=bot_id,
        message=f"🔴 CRITICAL: {bot_id} (fs.config.perms_world_readable): "
        f"openclaw.json is world-readable",
        detail="mode 0644; readable by all users on a multi-user host",
    )


def _benign_perm_warn(bot_id: str = "team_bot_a") -> Finding:
    """The existing perm/mode/acl flap family — must keep its own dwell."""
    return Finding(
        level="warn",
        category="config",
        bot_id=bot_id,
        message=f"{bot_id}: openclaw.json permissions corrected to 0600",
        detail="was 0640 (token-bearing; must be 0600), chmod 600 succeeded",
    )


def _standing_critical() -> Finding:
    """A persistent non-identity critical (the standing-batch stand-in)."""
    return Finding(
        # R-3 classification: sudoers baseline drift is posture.
        level="critical", finding_kind="posture", category="machine",
        bot_id=None,
        message="🔴 CRITICAL: /etc/sudoers.d/evolve changed since baseline",
        detail="",
    )


def _active(shared_dir: Path) -> list:
    return list(signals_store.iter_active(shared_dir, producer="audit"))


def _emit(shared_dir, criticals, warns, now):
    return audit._emit_signals_from_findings(criticals, warns, shared_dir, now=now)


def _pending(shared_dir: Path, finding: Finding) -> Path:
    return flap_gate._pending_path(shared_dir, audit._audit_signature(finding))


# ── Classifier ───────────────────────────────────────────────────────────────


def test_classifier_matches_both_backup_mismatch_shapes():
    assert audit._is_identity_mismatch_finding(_identity_mismatch()) is True
    assert audit._is_identity_mismatch_finding(_procedure_mismatch()) is True


def test_classifier_excludes_other_families():
    # Baseline-drift identity critical: different family, no dwell.
    assert audit._is_identity_mismatch_finding(_zshrc_baseline_critical()) is False
    # Credential exposure: wrong category AND wrong message — never dwells here.
    assert audit._is_identity_mismatch_finding(_credential_exposure_critical()) is False
    # Perm family stays with its own classifier; no cross-match either way.
    assert audit._is_identity_mismatch_finding(_benign_perm_warn()) is False
    assert audit._is_flap_prone_perm_finding(_identity_mismatch()) is False
    assert audit._is_flap_prone_perm_finding(_procedure_mismatch()) is False


# ── Core hysteresis ──────────────────────────────────────────────────────────


def test_first_run_mismatch_withheld_no_signal(tmp_path):
    settle_gate.mark_settled(tmp_path)
    crit = _identity_mismatch()

    kept, critical_ids, withheld = _emit(tmp_path, [crit], [], now=_NOW)

    assert _active(tmp_path) == []
    assert critical_ids == {}
    sig = audit._audit_signature(crit)
    # Withheld: not kept (sweep_resolve leaves prior signals alone), reported
    # back so the batched alert can exclude it, dwell ledger entry written.
    assert sig not in kept
    assert sig in withheld
    assert _pending(tmp_path, crit).exists()


def test_second_consecutive_run_fires_critical(tmp_path):
    settle_gate.mark_settled(tmp_path)
    crit = _identity_mismatch()

    _emit(tmp_path, [crit], [], now=_NOW)
    kept, critical_ids, withheld = _emit(
        tmp_path, [crit], [], now=_NOW + timedelta(minutes=15)
    )

    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "alert"
    assert sigs[0].type == "audit_identity"
    sig = audit._audit_signature(crit)
    assert sig in kept
    assert sig in critical_ids
    assert withheld == set()


def test_heal_resets_dwell(tmp_path):
    """fire → heal → fire (non-consecutive) never pages: the clear-sweep
    resets the counter on the healed run, so the count never reaches N=2."""
    settle_gate.mark_settled(tmp_path)
    crit = _identity_mismatch()

    _emit(tmp_path, [crit], [], now=_NOW)                            # dwell 1/2
    assert _active(tmp_path) == []
    # Backup commit landed; the mismatch healed → no identity finding this run.
    _emit(tmp_path, [], [], now=_NOW + timedelta(minutes=15))        # reset
    assert not _pending(tmp_path, crit).exists()
    # The bot edits again next week — back at 1/2, still withheld.
    _emit(tmp_path, [crit], [], now=_NOW + timedelta(minutes=30))    # dwell 1/2
    assert _active(tmp_path) == []


def test_sustained_mismatch_keeps_firing(tmp_path):
    """Once promoted, re-observation pages immediately (already-firing
    short-circuit) — a genuinely persistent mismatch is never re-dwelled."""
    settle_gate.mark_settled(tmp_path)
    crit = _identity_mismatch()

    for i in range(4):
        kept, _ids, withheld = _emit(
            tmp_path, [crit], [], now=_NOW + timedelta(minutes=15 * i)
        )
    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "alert"
    # Runs 2-4 all kept (paged); nothing withheld once promoted.
    assert audit._audit_signature(crit) in kept
    assert withheld == set()


def test_procedure_warn_family_dwells_too(tmp_path):
    settle_gate.mark_settled(tmp_path)
    warn = _procedure_mismatch()

    _emit(tmp_path, [], [warn], now=_NOW)
    assert _active(tmp_path) == []
    _emit(tmp_path, [], [warn], now=_NOW + timedelta(minutes=15))
    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "warn"


# ── Must-page floor + other families unaffected ──────────────────────────────


def test_credential_exposure_still_pages_immediately(tmp_path):
    settle_gate.mark_settled(tmp_path)
    crit = _credential_exposure_critical()

    _kept, _ids, withheld = _emit(tmp_path, [crit], [], now=_NOW)
    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "alert"
    assert withheld == set()
    assert not _pending(tmp_path, crit).exists()


def test_zshrc_baseline_critical_still_pages_immediately(tmp_path):
    settle_gate.mark_settled(tmp_path)
    crit = _zshrc_baseline_critical()

    _emit(tmp_path, [crit], [], now=_NOW)
    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "alert"


def test_perm_flap_family_unaffected_by_identity_sweep(tmp_path):
    """The two families dwell under separate ledger types: an identity heal
    must not reset a perm dwell counter, and vice versa."""
    settle_gate.mark_settled(tmp_path)
    perm = _benign_perm_warn()
    ident = _identity_mismatch()

    # Both start dwelling (1/2 each).
    _emit(tmp_path, [ident], [perm], now=_NOW)
    assert _pending(tmp_path, perm).exists()
    assert _pending(tmp_path, ident).exists()

    # Identity heals; perm persists → perm promotes on its 2nd consecutive
    # run, identity's counter is swept, perm's was never touched by the
    # identity-family sweep.
    _emit(tmp_path, [], [perm], now=_NOW + timedelta(minutes=15))
    assert not _pending(tmp_path, ident).exists()
    sigs = _active(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].severity == "warn"

    # And the mirror: perm heals while identity dwells → identity's counter
    # survives the perm-family sweep and promotes on its 2nd consecutive run.
    _emit(tmp_path, [ident], [], now=_NOW + timedelta(minutes=30))   # 1/2
    _emit(tmp_path, [ident], [], now=_NOW + timedelta(minutes=45))   # 2/2 fires
    assert any(s.severity == "alert" for s in _active(tmp_path))


# ── End-to-end: the batched alert stops churning ─────────────────────────────


def test_one_run_blip_does_not_rebroadcast_standing_batch(tmp_path, monkeypatch):
    """The R-2 acceptance case: a standing critical batch was already sent;
    a one-run identity blip appears then heals. The batch fingerprint must
    not change, so the standing set is NOT re-broadcast."""
    settle_gate.mark_settled(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr("audit._send_security_alert", _capture_page(sent))
    standing = _standing_critical()
    blip = _identity_mismatch()

    # Run 1: standing critical pages; batch sent once.
    audit.dispatch_findings([standing], tmp_path, config={}, dry_run=False)
    assert len(sent) == 1
    # Run 2: the bot edits AGENTS.md — blip is withheld, batch unchanged.
    audit.dispatch_findings([standing, blip], tmp_path, config={}, dry_run=False)
    assert len(sent) == 1
    assert audit._audit_signature(blip) not in {
        s.signature for s in _active(tmp_path)
    }
    # Run 3: backup committed, blip healed — still no re-broadcast.
    audit.dispatch_findings([standing], tmp_path, config={}, dry_run=False)
    assert len(sent) == 1


def test_sustained_mismatch_joins_batch_on_second_run(tmp_path, monkeypatch):
    """A mismatch persisting 2 consecutive runs still reaches the operator:
    run 2 promotes it into the batch, which re-sends with the new bullet."""
    settle_gate.mark_settled(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr("audit._send_security_alert", _capture_page(sent))
    standing = _standing_critical()
    mismatch = _identity_mismatch()

    audit.dispatch_findings([standing, mismatch], tmp_path, config={}, dry_run=False)
    assert len(sent) == 1
    assert "hash mismatch" not in sent[0]          # run 1: withheld
    audit.dispatch_findings([standing, mismatch], tmp_path, config={}, dry_run=False)
    assert len(sent) == 2
    assert "hash mismatch" in sent[1]              # run 2: promoted, pages
