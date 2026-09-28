"""Tests for audit._check_filevault.

FileVault is macOS full-disk encryption. Security_bot treated "FileVault is off"
as a security finding worth flagging; porting that check into Evolve's
audit was gap #1 of the Security_bot retirement coverage map.

Pinned behavior:
  - "FileVault is On." (any case) → ok finding, no alert
  - "FileVault is Off." (any case) → critical finding with what_it_means
    and a concrete fix_steps playbook
  - Indeterminate output (encryption/decryption in progress) → warn
  - fdesetup missing / non-zero exit / timeout → skipped (not a finding
    against the operator; an audit-infrastructure gap)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import platform_profile  # noqa: E402


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def test_filevault_on_emits_ok(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout="FileVault is On.\n"),
    )
    findings = audit._check_filevault()
    assert len(findings) == 1
    assert findings[0].level == "ok"
    assert findings[0].category == "machine"
    assert findings[0].bot_id is None


def test_filevault_off_emits_critical_with_playbook(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout="FileVault is Off.\n"),
    )
    findings = audit._check_filevault()
    assert len(findings) == 1
    f = findings[0]
    assert f.level == "critical"
    assert f.category == "machine"
    assert "FileVault is off" in f.message
    # Operator-facing context must be populated — without it the alert
    # row in the UI shows just the headline, which doesn't tell the
    # operator why it matters or what to do.
    assert "physical access" in f.what_it_means.lower()
    assert "System Settings" in f.fix_steps
    assert "recovery key" in f.fix_steps.lower()
    # 2026-06-04: the playbook also documents the policy_acceptances
    # escape hatch for operators who have legitimately decided to leave
    # FileVault off. Without this the only path to silence the alert is
    # to dismiss it forever in the UI; the operator-declared form keeps
    # the audit trail visible at the config layer.
    assert "policy_acceptances" in f.fix_steps


def test_filevault_off_with_operator_acceptance_emits_ok(monkeypatch):
    """network.json::policy_acceptances['machine.filevault_off'] demotes
    the critical finding to an ok-level 'operator-accepted' line. The
    reason text from the acceptance lands in the message so a future
    operator can see why this was accepted."""
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout="FileVault is Off.\n"),
    )
    config = {
        "policy_acceptances": {
            "machine.filevault_off": {
                "reason": "single-tenant dev mini, locked room",
                "accepted_at": "2026-06-04",
                "accepted_by": "pod-admin",
            }
        }
    }
    findings = audit._check_filevault(config)
    assert len(findings) == 1
    f = findings[0]
    assert f.level == "ok"
    assert "operator-accepted" in f.message
    assert "single-tenant dev mini" in f.message


def test_filevault_off_with_malformed_acceptance_falls_back_to_critical(monkeypatch):
    """Tolerance for malformed network.json: a non-dict or empty-dict
    entry should NOT silently accept — the check falls back to the
    critical finding. Acceptance must be a non-empty dict (with at
    least the ``reason`` field by convention) so a placeholder
    ``"machine.filevault_off": {}`` doesn't quietly silence the
    finding."""
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout="FileVault is Off.\n"),
    )
    # Non-dict shapes + empty dict → no acceptance
    for bad in (None, "yes", [], {}):
        config = {"policy_acceptances": {"machine.filevault_off": bad}}
        findings = audit._check_filevault(config)
        assert findings[0].level == "critical", (
            f"empty/non-dict acceptance entry {bad!r} should NOT silence "
            f"the finding"
        )
    # Dict with the reason field present → acceptance, even if reason
    # is the empty string (the operator opted in; the audit message
    # falls back to 'no reason recorded' for rendering).
    for ok in ({"reason": ""}, {"reason": "x"}, {"accepted_by": "pod-admin"}):
        config = {"policy_acceptances": {"machine.filevault_off": ok}}
        findings = audit._check_filevault(config)
        assert findings[0].level == "ok"


def test_policy_acceptance_helper_handles_missing_block():
    """audit.policy_acceptance returns None when network.json has no
    policy_acceptances block at all, when the block is malformed, or
    when the specific check_id isn't listed."""
    assert audit.policy_acceptance("machine.filevault_off", None) is None
    assert audit.policy_acceptance("machine.filevault_off", {}) is None
    assert audit.policy_acceptance(
        "machine.filevault_off", {"policy_acceptances": []}
    ) is None
    assert audit.policy_acceptance(
        "machine.filevault_off", {"policy_acceptances": {"other_check": {}}}
    ) is None
    # Present check returns the entry dict
    entry = audit.policy_acceptance(
        "machine.filevault_off",
        {"policy_acceptances": {"machine.filevault_off": {"reason": "x"}}}
    )
    assert entry == {"reason": "x"}


def test_filevault_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout="FILEVAULT IS ON."),
    )
    findings = audit._check_filevault()
    assert findings[0].level == "ok"


def test_filevault_in_progress_emits_warn(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(
            stdout="Encryption in progress: Percent completed = 42.5\n",
        ),
    )
    findings = audit._check_filevault()
    assert findings[0].level == "warn"
    assert "indeterminate" in findings[0].message.lower()


def test_filevault_fdesetup_missing_is_skipped(monkeypatch):
    def _raise(*_a, **_k):
        raise FileNotFoundError("fdesetup")
    monkeypatch.setattr(audit.subprocess, "run", _raise)
    findings = audit._check_filevault()
    assert findings[0].level == "skipped"


def test_filevault_fdesetup_nonzero_is_skipped(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(
            stderr="fdesetup: permission denied", returncode=1,
        ),
    )
    findings = audit._check_filevault()
    assert findings[0].level == "skipped"
    assert "permission denied" in (findings[0].detail or "")


def test_filevault_timeout_is_skipped(monkeypatch):
    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="fdesetup", timeout=5)
    monkeypatch.setattr(audit.subprocess, "run", _timeout)
    findings = audit._check_filevault()
    assert findings[0].level == "skipped"


def test_filevault_wired_into_audit_machine(monkeypatch, tmp_path: Path):
    """The check must actually be invoked by audit_machine(), not just
    sit in the module unconnected. Mock all the other machine checks to
    return [], make fdesetup report "off", and verify the critical
    finding is in audit_machine's return."""
    # FileVault is a macOS-only check; pin the macOS profile so the
    # platform-applicability gate in audit_machine runs it on Linux CI.
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.MACOS)
    monkeypatch.setattr(audit, "_check_firewall", lambda: [])
    monkeypatch.setattr(audit, "_check_ssh_config", lambda: [])
    monkeypatch.setattr(audit, "_check_user_accounts", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_listening_ports", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_oc_binary_mtime", lambda *_a, **_k: [])
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout="FileVault is Off.\n"),
    )
    findings = audit.audit_machine(tmp_path, {})
    assert any(f.level == "critical" and "FileVault" in f.message for f in findings)
