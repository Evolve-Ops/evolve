"""Tests for the audit→remediation mapping introduced in Phase 4 PR-2.

Pins two contracts:

  1. Script-inventory drift findings (the coalesced one-per-bot finding
     emitted by audit_script_inventory after Phase 3) carry a
     reset_baseline Remediation when mirrored to Signals. The alerts UI
     reads this to render the "Reset baseline" button on the alert card.

  2. Other audit findings (sudoers hash change, .zshrc unreadable, etc.)
     do NOT auto-attach a remediation today — those need operator
     judgment first, and the mapping intentionally stays narrow.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from audit import Finding, _remediation_for_finding  # noqa: E402


# ── Script-drift gets reset_baseline ─────────────────────────────────────────


def test_script_drift_finding_gets_reset_baseline_remediation():
    """Coalesced drift finding (Phase 3 shape) → reset_baseline remediation."""
    f = Finding(
        level="warn",
        category="identity",
        bot_id="security_bot",
        message="security_bot: script inventory drift (+3 new, -2 missing)",
        detail="new: foo.py | missing: bar.py",
    )
    rem = _remediation_for_finding(f)
    assert rem is not None
    assert rem.kind == "reset_baseline"
    assert rem.params == {"bot_id": "security_bot", "kind": "scripts"}
    assert rem.label, "remediation needs a button label"
    assert "baseline" in rem.confirm.lower(), (
        "confirm text should mention 'baseline' so operator knows what's being reset"
    )


def test_script_drift_remediation_carries_correct_bot_id_per_finding():
    """The bot_id in params reflects the finding's bot, not a hardcode."""
    for bot in ("team_bot_a", "team_bot_b", "admin_bot"):
        f = Finding(
            level="warn",
            category="identity",
            bot_id=bot,
            message=f"{bot}: script inventory drift (+1 new)",
            detail="new: x.sh",
        )
        rem = _remediation_for_finding(f)
        assert rem is not None
        assert rem.params["bot_id"] == bot


# ── Non-drift identity findings stay un-remediated ──────────────────────────


def test_zshrc_hash_change_gets_reset_baseline_with_security_confirm():
    """Posture flip 2026-06-06: a .zshrc hash mismatch DOES carry a
    reset_baseline remediation now. The original implementation
    withheld the button on the rationale "operator should read before
    resetting", but a legitimate operator edit (evolve's one-line
    ``source openclaw completion`` line landed during initial account
    setup) then fires the audit forever with no targeted reset path.

    The remediation exists, but the security guardrail moves into the
    confirm string so operators have to acknowledge what they're
    asserting before clicking.
    """
    f = Finding(
        level="critical",
        category="identity",
        bot_id="admin_bot",
        message="🔴 CRITICAL: admin_bot .zshrc hash changed since baseline",
        detail="baseline=abc current=def",
    )
    rem = _remediation_for_finding(f)
    assert rem is not None
    assert rem.kind == "reset_baseline"
    assert rem.params == {"bot_id": "admin_bot", "kind": "shell"}
    # Label flags the operator-acknowledgment aspect, not just "Reset".
    assert "Accept current .zshrc" in rem.label
    assert "verifying" in rem.label or "admin_bot" in rem.label
    # Confirm string must spell out the security stance.
    assert "SECURITY" in rem.confirm
    assert "compromised" in rem.confirm.lower(), (
        "confirm should call out the bot-compromise check so the "
        "operator doesn't just hit Accept reflexively"
    )
    # Platform-resolved home (/Users on macOS, /home on Linux CI) — must
    # match audit's _bot_home() construction.
    from evolve_config import bot_home

    assert f"{bot_home('admin_bot')}/.zshrc" in rem.confirm


def test_zshrc_deleted_gets_reset_baseline_with_security_confirm():
    """The .zshrc deleted-since-baseline warn finding shares the same
    remediation path: operator confirms the deletion was intentional
    (the file was removed during account cleanup) and the baseline
    re-records the absent state on the next audit run."""
    f = Finding(
        level="warn",
        category="identity",
        bot_id="admin_bot",
        message="admin_bot: .zshrc deleted (baseline says present)",
        detail="baseline=abc",
    )
    rem = _remediation_for_finding(f)
    assert rem is not None
    assert rem.kind == "reset_baseline"
    assert rem.params == {"bot_id": "admin_bot", "kind": "shell"}


def test_zshrc_unreadable_does_not_auto_remediate():
    """Unreadable is a different problem — the audit can't compute a
    hash to compare. Resetting the baseline doesn't help; the operator
    needs to fix the ACL / sudoers grant first. No button."""
    f = Finding(
        level="warn",
        category="identity",
        bot_id="admin_bot",
        message="admin_bot: .zshrc unreadable",
    )
    assert _remediation_for_finding(f) is None


def test_zshrc_appeared_does_not_auto_remediate():
    """The "appeared" path already auto-rewrites the baseline inside
    audit_shell_config — the operator-visible finding is informational.
    No button needed."""
    f = Finding(
        level="warn",
        category="identity",
        bot_id="admin_bot",
        message="admin_bot: .zshrc appeared — new baseline established",
        detail="hash=abc",
    )
    assert _remediation_for_finding(f) is None


def test_zshrc_ok_does_not_auto_remediate():
    """The OK path obviously doesn't need a button — nothing to reset."""
    f = Finding(
        level="ok",
        category="identity",
        bot_id="admin_bot",
        message="admin_bot: .zshrc OK",
    )
    assert _remediation_for_finding(f) is None


def test_config_category_findings_do_not_auto_remediate():
    """Findings outside the script/cron drift patterns get no auto-remediation."""
    f = Finding(
        level="warn",
        category="config",
        bot_id="security_bot",
        message="security_bot: cron job uses sessionTarget=main with exec payload",
    )
    assert _remediation_for_finding(f) is None


# ── Cron-baseline drift gets reset_baseline ─────────────────────────────────


def test_cron_drift_finding_gets_reset_baseline_remediation():
    """audit_cron_health's "new cron job not in baseline" → cron-jobs reset."""
    f = Finding(
        level="warn",
        category="config",
        bot_id="security_bot",
        message="security_bot: new cron job not in baseline: 'usage-alert-dispatch'",
    )
    rem = _remediation_for_finding(f)
    assert rem is not None
    assert rem.kind == "reset_baseline"
    assert rem.params == {"bot_id": "security_bot", "kind": "cron-jobs"}
    assert rem.label
    assert "baseline" in rem.confirm.lower()


def test_cron_drift_remediation_carries_correct_bot_id_per_finding():
    for bot in ("team_bot_a", "team_bot_b", "admin_bot"):
        f = Finding(
            level="warn",
            category="config",
            bot_id=bot,
            message=f"{bot}: new cron job not in baseline: 'self-installed'",
        )
        rem = _remediation_for_finding(f)
        assert rem is not None
        assert rem.params == {"bot_id": bot, "kind": "cron-jobs"}


def test_cron_drift_pod_scope_without_bot_id_gets_no_remediation():
    """No bot_id → can't target a per-bot baseline. No button."""
    f = Finding(
        level="warn",
        category="config",
        bot_id=None,
        message="pod: new cron job not in baseline: 'phantom'",
    )
    assert _remediation_for_finding(f) is None


def test_machine_findings_do_not_auto_remediate():
    f = Finding(
        level="warn",
        category="machine",
        bot_id=None,
        message="machine: openclaw binary mtime changed",
    )
    assert _remediation_for_finding(f) is None


def test_skipped_capability_gap_findings_do_not_auto_remediate():
    """Capability gaps (the 'skipped' level from PR #1004) don't have a
    mechanical fix — they need the audit's own sudoers/ACL to be
    extended. No button on the alert."""
    f = Finding(
        level="skipped",
        category="machine",
        bot_id=None,
        message="machine: listening-ports check denied — sudo lsof failed",
    )
    assert _remediation_for_finding(f) is None


def test_pod_scope_finding_without_bot_id_does_not_get_per_bot_remediation():
    """reset_baseline needs a bot_id; a pod-wide finding has none, so no
    button (would have to be a different kind)."""
    f = Finding(
        level="warn",
        category="identity",
        bot_id=None,
        message="pod-wide: script inventory drift",
    )
    assert _remediation_for_finding(f) is None
