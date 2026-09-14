"""Tests for audit._check_admin_user_gateway.

The pod admin user (typically ``pod_admin_user``) has sudo over every bot, the
evolve service, launchd, and the deploy checkout. An openclaw gateway
running under that account would let an LLM execute commands at that
privilege level — root over the whole pod. This was Security_bot's highest-
stakes CRITICAL invariant; porting it into Evolve's audit was gap #3
of the Security_bot retirement coverage map.

Pinned behavior:
  - admin_user missing from config        → skipped
  - ps fails / unavailable                → skipped
  - no admin-user gateway in ps output    → ok
  - admin-user gateway found              → critical with playbook
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


# Real-looking ps auxww lines — col layout is USER PID %CPU %MEM ... CMD.
_TEAM_BOT_A_GATEWAY = (
    "team_bot_a             12345   0.5  1.2 408392832  98765   ??  S    10:30AM "
    "0:42.10 /opt/homebrew/bin/node /Users/team_bot_a/.openclaw/node_modules/openclaw/dist/entry.js"
)
_SECURITY_BOT_GATEWAY = (
    "security_bot          54321   0.3  0.8 408392832  72345   ??  S    11:00AM "
    "0:21.05 /opt/homebrew/bin/node /Users/security_bot/.openclaw/node_modules/openclaw/dist/entry.js"
)
_POD_ADMIN_USER_GATEWAY = (
    "pod_admin_user         99999   0.7  1.5 408392832 123456   ??  S    11:45AM "
    "0:12.30 /opt/homebrew/bin/node /Users/pod_admin_user/.openclaw/node_modules/openclaw/dist/entry.js"
)
_POD_ADMIN_USER_SHELL = (
    "pod_admin_user         11111   0.0  0.1   4123456   2345   ??  S     9:00AM "
    "0:00.50 -zsh"
)


def test_admin_user_missing_skips(monkeypatch):
    findings = audit._check_admin_user_gateway({})
    assert findings[0].level == "skipped"
    assert "admin_user not set" in findings[0].message


def test_admin_user_none_skips():
    findings = audit._check_admin_user_gateway({"admin_user": None})
    assert findings[0].level == "skipped"


def test_ok_when_no_admin_gateway(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(
            stdout=f"USER PID ...\n{_TEAM_BOT_A_GATEWAY}\n{_SECURITY_BOT_GATEWAY}\n{_POD_ADMIN_USER_SHELL}\n",
        ),
    )
    findings = audit._check_admin_user_gateway({"admin_user": "pod_admin_user"})
    assert findings[0].level == "ok"
    assert "no openclaw gateway running as pod_admin_user" in findings[0].message


def test_critical_when_admin_runs_gateway(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(
            stdout=f"USER PID ...\n{_TEAM_BOT_A_GATEWAY}\n{_POD_ADMIN_USER_GATEWAY}\n",
        ),
    )
    findings = audit._check_admin_user_gateway({"admin_user": "pod_admin_user"})
    assert len(findings) == 1
    f = findings[0]
    assert f.level == "critical"
    assert f.category == "machine"
    assert "pod_admin_user" in f.message
    assert "admin user" in f.message
    assert _POD_ADMIN_USER_GATEWAY[:300] in f.detail
    # Operator playbook must be populated — this finding will page Pod_admin
    # immediately and he needs to know what to do.
    assert "sudo" in f.what_it_means.lower()
    assert "launchctl" in f.fix_steps.lower()
    assert "Rotate" in f.fix_steps


def test_critical_works_for_different_admin_user(monkeypatch):
    """The check must follow network.json's admin_user, not be hardcoded to pod_admin_user."""
    other_admin_gateway = _POD_ADMIN_USER_GATEWAY.replace("pod_admin_user", "podadmin")
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout=f"{other_admin_gateway}\n"),
    )
    findings = audit._check_admin_user_gateway({"admin_user": "podadmin"})
    assert findings[0].level == "critical"
    assert "podadmin" in findings[0].message


def test_ps_failure_skips(monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(returncode=1, stderr="ps: command not found"),
    )
    findings = audit._check_admin_user_gateway({"admin_user": "pod_admin_user"})
    assert findings[0].level == "skipped"


def test_ps_timeout_skips(monkeypatch):
    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=10)
    monkeypatch.setattr(audit.subprocess, "run", _timeout)
    findings = audit._check_admin_user_gateway({"admin_user": "pod_admin_user"})
    assert findings[0].level == "skipped"


def test_wired_into_audit_machine(tmp_path: Path, monkeypatch):
    # admin-user-gateway is platform-neutral (runs on macOS + Linux), but
    # pin the macOS profile so the test is deterministic regardless of the
    # CI host OS and exercises the full macOS check pipeline.
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.MACOS)
    monkeypatch.setattr(audit, "_check_firewall", lambda: [])
    monkeypatch.setattr(audit, "_check_filevault", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_ssh_config", lambda: [])
    monkeypatch.setattr(audit, "_check_macos_updates", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_user_accounts", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_listening_ports", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_oc_binary_mtime", lambda *_a, **_k: [])
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed(stdout=f"{_POD_ADMIN_USER_GATEWAY}\n"),
    )
    findings = audit.audit_machine(tmp_path, {"admin_user": "pod_admin_user"})
    assert any(
        f.level == "critical" and "admin user" in f.message
        for f in findings
    )
