"""Tests for evolve_admin.skills._qr_pairing — the QR pairing session manager.

Covers:
- Session lifecycle (start / poll / cancel)
- One-session-per-bot-channel eviction
- PNG rendering roundtrip
- Spawn-command shape matches deploy.py's openclaw-as-bot-user pattern
- Failure paths (FileNotFoundError, PermissionError)

All subprocess interaction is mocked — no real ``openclaw`` invocation.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.skills import _qr_pairing as qp  # noqa: E402


# ── Fake subprocess for spawn ────────────────────────────────────────────────


class _FakeProc:
    """Minimal Popen-like fake. Tests can push QR/marker lines onto
    ``self._lines`` before start_session and the stdout reader picks
    them up."""

    def __init__(self, lines=None, exit_code=0, exit_delay_s=0.1):
        self._lines = list(lines or [])
        self.returncode = None
        self._exit_code = exit_code
        self._exit_delay = exit_delay_s
        self._started = time.monotonic()
        self.stdout = self._StdoutIter(self._lines, self)
        self.terminated = False
        self.killed = False

    class _StdoutIter:
        def __init__(self, lines, parent):
            self._lines = lines
            self._parent = parent
            self._idx = 0

        def readline(self):
            if self._idx >= len(self._lines):
                return ""  # eof
            line = self._lines[self._idx]
            self._idx += 1
            return line + "\n"

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if time.monotonic() - self._started >= self._exit_delay:
            self.returncode = self._exit_code
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


def _fake_spawn_factory(proc):
    def _spawn(cmd, **kwargs):
        return proc
    return _spawn


# ── PNG rendering ───────────────────────────────────────────────────────────


class TestRenderQrPng:
    def test_renders_a_base64_png_data_url(self):
        url = qp.render_qr_png_data_url("test-payload-string")
        assert url.startswith("data:image/png;base64,")
        # Base64-decodes to valid PNG bytes
        payload = url.split(",", 1)[1]
        decoded = base64.b64decode(payload)
        # PNG signature: 89 50 4E 47 0D 0A 1A 0A
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_different_payloads_produce_different_pngs(self):
        a = qp.render_qr_png_data_url("payload-A-foo")
        b = qp.render_qr_png_data_url("payload-B-bar")
        assert a != b

    def test_long_baileys_payload_renders(self):
        """Baileys QR payloads are ~150-300 chars; the qrcode lib must
        scale to fit without crashing."""
        baileys_qr = (
            "2@" + "abc123" * 30 + ",foo=,bar=,ref=" + "x" * 20
        )
        url = qp.render_qr_png_data_url(baileys_qr)
        assert url.startswith("data:image/png;base64,")


# ── Spawn command shape ─────────────────────────────────────────────────────


class TestBuildSpawnCommand:
    """The spawn argv must match deploy.py:2811's pattern for
    openclaw-as-bot-user so the existing sudoers grant covers it."""

    def test_includes_preserve_env_openclaw_config_path(self):
        cmd = qp._build_spawn_command(
            "bot-user-a", "team-bot-a", "whatsapp",
            Path("/Users/bot-user-a/.openclaw/openclaw.json"),
        )
        assert "--preserve-env=OPENCLAW_CONFIG_PATH" in cmd
        assert "-H" in cmd
        assert "-u" in cmd
        # -n = non-interactive (no password prompt)
        assert "-n" in cmd

    def test_includes_channel_and_account_args(self):
        cmd = qp._build_spawn_command(
            "bot-user-a", "team-bot-a", "whatsapp",
            Path("/x/openclaw.json"),
        )
        assert "channels" in cmd
        assert "login" in cmd
        assert "--channel" in cmd
        assert cmd[cmd.index("--channel") + 1] == "whatsapp"
        assert "--account" in cmd
        assert cmd[cmd.index("--account") + 1] == "team-bot-a"

    def test_default_openclaw_bin_is_homebrew_path(self):
        cmd = qp._build_spawn_command(
            "bot-user-a", "team-bot-a", "signal",
            Path("/x/openclaw.json"),
        )
        # /opt/homebrew/bin/openclaw is the canonical path on the mini —
        # matches the SETENV sudoers grant in setup_wizard.
        assert "/opt/homebrew/bin/openclaw" in cmd


# ── Session lifecycle ──────────────────────────────────────────────────────


class TestSessionLifecycle:

    def _start(self, tmp_path, proc, bot_id="team-bot-a", channel="whatsapp"):
        """Wrap start_session with a fake spawn + fixed config path."""
        cfg = tmp_path / "Users" / "bot-user-a" / ".openclaw" / "openclaw.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{}")
        return qp.start_session(
            bot_id, channel,
            bot_user="bot-user-a", config_path=cfg,
            spawn=_fake_spawn_factory(proc),
        )

    def test_start_returns_session_id_and_initial_state(self, tmp_path):
        proc = _FakeProc(lines=[], exit_delay_s=10)  # no QR, never exits
        snap = self._start(tmp_path, proc)
        assert snap["session_id"].startswith("qrp_")
        assert snap["state"] in ("starting", "waiting", "failed")
        # qp.cancel_session cleans up the worker threads
        qp.cancel_session(snap["session_id"])

    def test_qr_payload_transitions_state_to_waiting(self, tmp_path):
        proc = _FakeProc(
            lines=["2@" + "abc" * 50 + ",foo=,ref=xyz"],
            exit_delay_s=10,
        )
        snap = self._start(tmp_path, proc)
        # Wait a beat for the stdout-reader thread to consume the line
        for _ in range(20):
            cur = qp.poll_session(snap["session_id"])
            if cur and cur.get("state") == "waiting":
                break
            time.sleep(0.05)
        cur = qp.poll_session(snap["session_id"])
        assert cur is not None
        assert cur["state"] == "waiting"
        assert cur["qr_png_data_url"] is not None
        assert cur["qr_png_data_url"].startswith("data:image/png;base64,")
        qp.cancel_session(snap["session_id"])

    def test_paired_marker_transitions_state_to_paired(self, tmp_path):
        proc = _FakeProc(
            lines=[
                "2@" + "abc" * 50 + ",foo=,ref=xyz",
                "Connected as team-bot-a, ready to send messages.",
            ],
            exit_delay_s=10,
        )
        snap = self._start(tmp_path, proc)
        for _ in range(40):
            cur = qp.poll_session(snap["session_id"])
            if cur and cur["state"] == "paired":
                break
            time.sleep(0.05)
        cur = qp.poll_session(snap["session_id"])
        assert cur is not None
        assert cur["state"] == "paired"

    def test_clean_exit_zero_treated_as_paired(self, tmp_path):
        """Baileys sometimes exits silently after the handshake completes
        without emitting a clear marker line. Watchdog should treat that
        as paired."""
        proc = _FakeProc(
            lines=["2@" + "abc" * 50 + ",foo=,ref=xyz"],
            exit_delay_s=0.1, exit_code=0,
        )
        snap = self._start(tmp_path, proc)
        for _ in range(60):
            cur = qp.poll_session(snap["session_id"])
            if cur and cur["state"] in ("paired", "failed", "expired"):
                break
            time.sleep(0.1)
        cur = qp.poll_session(snap["session_id"])
        assert cur is not None
        assert cur["state"] == "paired"

    def test_non_zero_exit_treated_as_failed(self, tmp_path):
        proc = _FakeProc(lines=[], exit_delay_s=0.1, exit_code=2)
        snap = self._start(tmp_path, proc)
        for _ in range(60):
            cur = qp.poll_session(snap["session_id"])
            if cur and cur["state"] == "failed":
                break
            time.sleep(0.1)
        cur = qp.poll_session(snap["session_id"])
        assert cur is not None
        assert cur["state"] == "failed"

    def test_cancel_session_terminates_subprocess(self, tmp_path):
        proc = _FakeProc(lines=[], exit_delay_s=60)
        snap = self._start(tmp_path, proc)
        sid = snap["session_id"]
        assert qp.cancel_session(sid) is True
        # Snapshot immediately after cancel
        cur = qp.poll_session(sid)
        assert cur is not None
        assert cur["state"] == "cancelled"
        assert proc.terminated or proc.killed

    def test_cancel_unknown_session_returns_false(self):
        assert qp.cancel_session("qrp_does-not-exist") is False

    def test_poll_unknown_session_returns_none(self):
        assert qp.poll_session("qrp_does-not-exist") is None

    def test_starting_second_session_for_same_bot_evicts_first(self, tmp_path):
        proc_a = _FakeProc(lines=[], exit_delay_s=60)
        proc_b = _FakeProc(lines=[], exit_delay_s=60)
        snap_a = self._start(tmp_path, proc_a)
        snap_b = self._start(tmp_path, proc_b)
        assert snap_a["session_id"] != snap_b["session_id"]
        cur_a = qp.poll_session(snap_a["session_id"])
        assert cur_a is not None
        # The first session should be cancelled by the eviction
        assert cur_a["state"] == "cancelled"
        qp.cancel_session(snap_b["session_id"])


# ── Spawn failure modes ─────────────────────────────────────────────────────


class TestSpawnFailureModes:

    def _start_with_spawn(self, tmp_path, spawn_fn):
        cfg = tmp_path / "Users" / "bot-user-a" / ".openclaw" / "openclaw.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{}")
        return qp.start_session(
            "team-bot-a", "whatsapp",
            bot_user="bot-user-a", config_path=cfg,
            spawn=spawn_fn,
        )

    def test_file_not_found_returns_failed_with_clear_error(self, tmp_path):
        def _raise_fnf(*a, **kw):
            raise FileNotFoundError("/opt/homebrew/bin/openclaw not found")
        snap = self._start_with_spawn(tmp_path, _raise_fnf)
        assert snap["state"] == "failed"
        assert snap["error"] == "openclaw_cli_not_found"

    def test_permission_error_returns_sudoers_grant_missing(self, tmp_path):
        def _raise_perm(*a, **kw):
            raise PermissionError("sudo: a password is required")
        snap = self._start_with_spawn(tmp_path, _raise_perm)
        assert snap["state"] == "failed"
        assert "sudoers_grant_missing" in snap["error"]

    def test_other_exception_returns_failed_with_class_in_error(self, tmp_path):
        def _raise_other(*a, **kw):
            raise RuntimeError("something broke")
        snap = self._start_with_spawn(tmp_path, _raise_other)
        assert snap["state"] == "failed"
        assert "RuntimeError" in snap["error"]
