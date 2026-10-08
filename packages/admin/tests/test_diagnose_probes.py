"""Tests for evolve_admin.diagnose.probes — focused on platform handling.

The 2026-04-27 deploy uncovered that probe_network's sshd_listening check
silently returned False on macOS because both `lsof` and `ss` failed to
run for the `evolve` user. These tests pin the corrected behavior:
- True when a working tool reports LISTEN on :22
- False when a working tool reports no listener
- 'unknown' when no tool is available (do NOT silently return False)
"""
from __future__ import annotations

import pytest

from evolve_admin.diagnose import probes


# ── _check_sshd_listening ──────────────────────────────────────────────────


def test_sshd_listening_true_when_lsof_reports_listen(monkeypatch):
    """Standard happy path: lsof at /usr/sbin/lsof returns LISTEN."""
    def fake_run(cmd, timeout=5):
        if cmd[0] == "/usr/sbin/lsof":
            return 0, "sshd  123 root  3u  IPv4  TCP *:22 (LISTEN)\n", ""
        return -2, "", "FileNotFoundError"
    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._check_sshd_listening() is True


def test_sshd_listening_false_when_lsof_reports_no_listener(monkeypatch):
    """lsof runs successfully but reports nothing on :22 → confirmed False."""
    def fake_run(cmd, timeout=5):
        if cmd[0] == "/usr/sbin/lsof":
            return 0, "", ""   # rc=0, no output → no LISTEN
        return -2, "", "FileNotFoundError"
    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._check_sshd_listening() is False


def test_sshd_listening_falls_through_to_netstat_macos(monkeypatch):
    """lsof not available; netstat at /usr/sbin/netstat (macOS) reports BSD-style."""
    macos_netstat_out = """\
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
tcp4       0      0  *.22                   *.*                    LISTEN
tcp4       0      0  127.0.0.1.5050         *.*                    LISTEN
"""
    def fake_run(cmd, timeout=5):
        if cmd[0] == "/usr/sbin/lsof":
            return -2, "", "FileNotFoundError"
        if cmd[0] == "/usr/sbin/netstat" and cmd == ["/usr/sbin/netstat", "-an"]:
            return 0, macos_netstat_out, ""
        return -2, "", "FileNotFoundError"
    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._check_sshd_listening() is True


def test_sshd_listening_falls_through_to_netstat_linux(monkeypatch):
    """lsof not at macOS path; netstat at /bin/netstat (Linux) reports Linux-style."""
    linux_netstat_out = """\
Active Internet connections (servers and established)
Proto Recv-Q Send-Q Local Address           Foreign Address         State
tcp        0      0 0.0.0.0:22              0.0.0.0:*               LISTEN
tcp6       0      0 :::5050                 :::*                    LISTEN
"""
    def fake_run(cmd, timeout=5):
        if cmd[0] == "/usr/sbin/lsof":
            return -2, "", "FileNotFoundError"
        if cmd[0] == "/usr/sbin/netstat":
            return -2, "", "FileNotFoundError"
        if cmd[0] == "/bin/netstat" and cmd == ["/bin/netstat", "-an"]:
            return 0, linux_netstat_out, ""
        return -2, "", "FileNotFoundError"
    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._check_sshd_listening() is True


def test_sshd_listening_unknown_when_no_tool_available(monkeypatch):
    """Critical: when neither lsof nor netstat works, return 'unknown' —
    NOT False. The pre-fix code returned False here, masking real signal
    during 2026-04-27 Phase 1 deploy verification."""
    def fake_run(cmd, timeout=5):
        return -2, "", "FileNotFoundError"   # everything missing
    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._check_sshd_listening() == "unknown"


def test_sshd_listening_unknown_on_lsof_timeout(monkeypatch):
    """lsof timeout → unknown (not fall-through to netstat — a timeout
    suggests system stress, not platform absence)."""
    def fake_run(cmd, timeout=5):
        if cmd[0] == "/usr/sbin/lsof":
            return -1, "", "timeout after 5s"
        # Should not reach netstat
        raise AssertionError("should have returned unknown without trying netstat")
    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._check_sshd_listening() == "unknown"


def test_sshd_listening_distinguishes_port_22_from_22xx(monkeypatch):
    """Regex must not match port 220, 2222, etc. that happen to start with '22'."""
    no_22_listener_out = """\
Active Internet connections
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
tcp4       0      0  *.2222                 *.*                    LISTEN
tcp4       0      0  *.220                  *.*                    LISTEN
tcp4       0      0  127.0.0.1.5050         *.*                    LISTEN
"""
    def fake_run(cmd, timeout=5):
        if cmd[0] == "/usr/sbin/lsof":
            return -2, "", "FileNotFoundError"
        if cmd[0] == "/usr/sbin/netstat":
            return 0, no_22_listener_out, ""
        return -2, "", "FileNotFoundError"
    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._check_sshd_listening() is False


# ── probe_network aggregate behavior ───────────────────────────────────────


def test_probe_network_overall_unknown_when_sshd_unknown_and_others_good(monkeypatch):
    """If sshd check can't run but admin-ui + tailscale are healthy, the
    overall verdict should be 'unknown' (we don't know enough), not
    'healthy' (overclaim) or 'unhealthy' (false alarm)."""
    def fake_run(cmd, timeout=5):
        c = cmd[0] if isinstance(cmd, list) else cmd
        if c == "/usr/sbin/lsof" or c.startswith("/") and "netstat" in c:
            return -2, "", "FileNotFoundError"
        if c == "curl":
            return 0, "200|0.05", ""
        # tailscale
        if "Tailscale" in c or "tailscale" in c:
            return 0, '{"BackendState": "Running"}', ""
        return -2, "", "FileNotFoundError"
    monkeypatch.setattr(probes, "_run", fake_run)

    result = probes.probe_network()
    assert result["observed"]["sshd_listening"] == "unknown"
    assert result["observed"]["admin_ui_healthy"] is True
    assert result["observed"]["tailscale_healthy"] is True
    assert result["status"] == "unknown"


def test_probe_network_overall_unhealthy_when_sshd_confirmed_down(monkeypatch):
    """If sshd is confirmed not listening (a working tool reports nothing),
    the verdict is 'unhealthy' regardless of other layers — this is the
    signal we missed during the 04-27 deploy."""
    def fake_run(cmd, timeout=5):
        c = cmd[0] if isinstance(cmd, list) else cmd
        if c == "/usr/sbin/lsof":
            return 0, "", ""   # lsof works; no listener
        if c == "curl":
            return 0, "200|0.05", ""
        if "tailscale" in c.lower():
            return 0, '{"BackendState": "Running"}', ""
        return -2, "", "FileNotFoundError"
    monkeypatch.setattr(probes, "_run", fake_run)

    result = probes.probe_network()
    assert result["observed"]["sshd_listening"] is False
    assert result["status"] == "unhealthy"
    assert any("sshd not listening" in n for n in result["notes"])
