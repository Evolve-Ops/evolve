"""tests/test_audit_ssh_password_paths.py — is a password login path REACHABLE?

``_check_ssh_config`` used to read one directive, ``PasswordAuthentication``,
and conclude password login was closed. That is not sufficient. With
``UsePAM yes``, the keyboard-interactive method hands authentication to PAM,
which authenticates against the account password — so the server still
advertises a password-backed method.

Verified on the mini 2026-08-02, after the operator applied
``PasswordAuthentication no``::

    passwordauthentication no
    kbdinteractiveauthentication yes
    usepam yes

    $ ssh -o PreferredAuthentications=none -o PubkeyAuthentication=no <user>@<mini>
    Permission denied (publickey,keyboard-interactive)

The audit's "SSH PasswordAuthentication is enabled" CRITICAL **resolved** on
the next run while the server was still offering ``keyboard-interactive``.
(Not confirmed by attempting a login — that would mean handling a real
credential. The path is treated as open until the directive is closed.)

What is pinned here:

  * the exact config that slipped through must FIRE, not pass;
  * ``kbdinteractive`` WITHOUT ``UsePAM`` must NOT fire — PAM is what makes it
    a password path, and firing without it would be a false positive;
  * ``ChallengeResponseAuthentication``, the pre-8.7 alias, counts too;
  * the pre-existing ``PasswordAuthentication`` message is byte-stable —
    ``_audit_signature`` hashes it, so changing it would resolve every pod's
    live Signal and reopen a new one;
  * the two paths stay SEPARATE findings, each naming its own directive.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _sshd(**directives):
    """A ``sshd -T`` stub. Defaults are the hardened shape (all paths closed)."""
    cfg = {
        "port": "22",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "usepam": "no",
        "permitrootlogin": "no",
        "pubkeyauthentication": "yes",
    }
    cfg.update(directives)
    return _R(0, "\n".join(f"{k} {v}" for k, v in cfg.items()) + "\n")


def _ssh_findings(monkeypatch, **directives):
    monkeypatch.setattr(audit.subprocess, "run",
                        lambda *_a, **_k: _sshd(**directives))
    return audit._check_ssh_config()


def _levels(findings, level):
    return [f for f in findings if f.level == level]


# ── the regression ───────────────────────────────────────────────────────────


def test_kbdinteractive_plus_pam_fires_even_with_password_auth_off(monkeypatch):
    """THE regression: the exact config that reported compliant on the mini."""
    findings = _ssh_findings(
        monkeypatch,
        passwordauthentication="no",
        kbdinteractiveauthentication="yes",
        usepam="yes",
    )

    crits = _levels(findings, "critical")
    assert len(crits) == 1, "a reachable password path must fire, not pass"
    assert "keyboard-interactive" in crits[0].message
    # An operator who already set PasswordAuthentication no must be told this
    # is a SECOND path, or the alert reads as stale and gets dismissed.
    assert "not a stale alert" in crits[0].detail
    assert "KbdInteractiveAuthentication" in crits[0].detail
    assert not any("password login closed" in f.message
                   for f in _levels(findings, "ok"))


def test_kbdinteractive_without_pam_does_not_fire(monkeypatch):
    """Keyboard-interactive alone is not a password path — PAM is what makes
    it one. Firing without UsePAM would be a false positive that trains
    operators to dismiss the finding."""
    findings = _ssh_findings(
        monkeypatch, kbdinteractiveauthentication="yes", usepam="no",
    )

    assert not _levels(findings, "critical")
    assert any("password login closed" in f.message
               for f in _levels(findings, "ok"))


def test_challenge_response_alias_is_honoured(monkeypatch):
    """Pre-8.7 sshd spells the same mechanism ChallengeResponseAuthentication;
    a host that set only the alias would slip a kbdinteractive-only read."""
    findings = _ssh_findings(
        monkeypatch,
        kbdinteractiveauthentication="no",
        challengeresponseauthentication="yes",
        usepam="yes",
    )

    crits = _levels(findings, "critical")
    assert len(crits) == 1
    assert "ChallengeResponseAuthentication" in crits[0].detail


# ── signal continuity + finding shape ────────────────────────────────────────


def test_password_auth_finding_message_is_unchanged(monkeypatch):
    """``_audit_signature`` hashes the message, so this string is a contract:
    changing it resolves every pod's live Signal and opens a new one."""
    findings = _ssh_findings(monkeypatch, passwordauthentication="yes")

    assert any(
        c.message == "🔴 CRITICAL: SSH PasswordAuthentication is enabled"
        for c in _levels(findings, "critical")
    )


def test_both_paths_open_fire_as_two_independent_findings(monkeypatch):
    """Two mechanisms, two fixes. One merged finding would leave whichever
    directive the operator fixed second looking already-handled."""
    findings = _ssh_findings(
        monkeypatch,
        passwordauthentication="yes",
        kbdinteractiveauthentication="yes",
        usepam="yes",
    )

    crits = _levels(findings, "critical")
    assert len(crits) == 2
    assert len({audit._audit_signature(c) for c in crits}) == 2


def test_fully_closed_config_reports_ok(monkeypatch):
    findings = _ssh_findings(monkeypatch)

    assert not _levels(findings, "critical")
    assert any("password login closed" in f.message
               for f in _levels(findings, "ok"))


def test_permit_root_login_check_is_unaffected(monkeypatch):
    """The root-login half of this check is out of scope — pin that it still
    behaves exactly as before alongside the new password-path logic."""
    findings = _ssh_findings(monkeypatch, permitrootlogin="yes")

    assert any("PermitRootLogin is enabled" in c.message
               for c in _levels(findings, "critical"))


def test_denied_sshd_read_is_still_skipped(monkeypatch):
    """A capability gap must stay ``skipped`` — not ``ok`` (which would claim
    compliance the check never verified) and not ``warn``."""
    monkeypatch.setattr(audit.subprocess, "run",
                        lambda *_a, **_k: _R(1, "", "permission denied"))

    findings = audit._check_ssh_config()

    assert findings and findings[0].level == "skipped"
    assert "sshd" in findings[0].message.lower()


# ── parsing ──────────────────────────────────────────────────────────────────


def test_sshd_config_parsed_by_exact_key_not_substring():
    """Substring matching was safe for one directive and stops being safe the
    moment a second one matters — ``…authentication yes`` matches whichever
    option happens to share the suffix."""
    cfg = audit._sshd_effective_config(
        "PasswordAuthentication no\nKbdInteractiveAuthentication yes\nUsePAM yes\n"
    )

    assert cfg["passwordauthentication"] == "no"
    assert cfg["kbdinteractiveauthentication"] == "yes"
    assert cfg["usepam"] == "yes"


def test_sshd_config_parser_tolerates_blank_and_valueless_lines():
    cfg = audit._sshd_effective_config("\n  \npermitrootlogin no\nallowusers\n")

    assert cfg["permitrootlogin"] == "no"
    assert cfg["allowusers"] == ""
