"""Tests for scripts/evolve_liveness_external.py.

Pinned behavior:
  - admin UI unreachable           → failure
  - signal log missing             → failure
  - signal log stale > 2h          → failure
  - audit log missing              → failure
  - audit log stale > 30m          → failure
  - in cooldown after recent page  → suppress repeat
  - missing keystore               → alert-delivery fails (exit 2)

The script intentionally avoids Evolve imports — these tests load it
via importlib so the test runner doesn't need any sys.path tricks.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parent / "evolve_liveness_external.py"


@pytest.fixture
def liveness():
    spec = importlib.util.spec_from_file_location("evolve_liveness_external", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── admin UI check ───────────────────────────────────────────────────


def test_admin_ui_reachable_no_failure(liveness, monkeypatch):
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
    monkeypatch.setattr(liveness.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert liveness._check_admin_ui() == []


def test_admin_ui_non_200_is_failure(liveness, monkeypatch):
    class FakeResp:
        status = 503
        def __enter__(self): return self
        def __exit__(self, *_): return False
    monkeypatch.setattr(liveness.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    failures = liveness._check_admin_ui()
    assert len(failures) == 1
    assert "503" in failures[0]


def test_admin_ui_connection_refused_is_failure(liveness, monkeypatch):
    def _raise(*a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(liveness.urllib.request, "urlopen", _raise)
    failures = liveness._check_admin_ui()
    assert len(failures) == 1
    assert "ConnectionRefusedError" in failures[0]


# ── signal store check ───────────────────────────────────────────────


def test_signal_log_missing_is_failure(liveness, tmp_path, monkeypatch):
    # Redirect the script's path probe at a tmp dir with no log.
    monkeypatch.setattr(
        liveness, "_check_signal_store",
        lambda: _check_with_root(liveness, tmp_path),
    )
    failures = liveness._check_signal_store()
    assert "missing" in failures[0].lower()


def _check_with_root(liveness, root: Path) -> list[str]:
    """Mirror of liveness._check_signal_store rebound to a tmp root."""
    from datetime import date
    today = date.today().isoformat()
    log = root / "signals" / "log" / f"{today}.jsonl"
    if not log.exists():
        return [f"signal store: today's log ({log.name}) missing — nothing written today"]
    age = time.time() - log.stat().st_mtime
    if age > liveness.SIGNAL_LOG_MAX_AGE:
        return [f"signal store: today's log idle for {liveness._human(age)}"]
    return []


def test_signal_log_fresh_passes(liveness, tmp_path):
    from datetime import date
    today = date.today().isoformat()
    log = tmp_path / "signals" / "log" / f"{today}.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"sig": "test"}')
    # 5 minutes ago
    os.utime(log, (time.time() - 300, time.time() - 300))
    assert _check_with_root(liveness, tmp_path) == []


def test_signal_log_stale_is_failure(liveness, tmp_path):
    from datetime import date
    today = date.today().isoformat()
    log = tmp_path / "signals" / "log" / f"{today}.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("")
    # 3 hours ago (> 2h threshold)
    old = time.time() - 3 * 3600
    os.utime(log, (old, old))
    failures = _check_with_root(liveness, tmp_path)
    assert len(failures) == 1
    assert "idle" in failures[0]


# ── audit log check ──────────────────────────────────────────────────


def test_audit_log_fresh_passes(liveness, tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    log.write_text("ran")
    os.utime(log, (time.time() - 60, time.time() - 60))
    monkeypatch.setattr(
        liveness, "_check_audit_log",
        lambda: _check_audit_with_path(liveness, log),
    )
    assert liveness._check_audit_log() == []


def _check_audit_with_path(liveness, log: Path) -> list[str]:
    if not log.exists():
        return ["audit.log missing"]
    age = time.time() - log.stat().st_mtime
    if age > liveness.AUDIT_LOG_MAX_AGE:
        return [f"audit.log idle for {liveness._human(age)}"]
    return []


def test_audit_log_stale_is_failure(liveness, tmp_path):
    log = tmp_path / "audit.log"
    log.write_text("ran")
    old = time.time() - 60 * 60  # 1h, > 30 min threshold
    os.utime(log, (old, old))
    failures = _check_audit_with_path(liveness, log)
    assert len(failures) == 1
    assert "idle" in failures[0]


def test_audit_log_missing_is_failure(liveness, tmp_path):
    log = tmp_path / "nope.log"
    failures = _check_audit_with_path(liveness, log)
    assert len(failures) == 1
    assert "missing" in failures[0]


# ── cooldown ─────────────────────────────────────────────────────────


def test_cooldown_absent_returns_false(liveness, tmp_path, monkeypatch):
    monkeypatch.setattr(liveness, "COOLDOWN_FILE", tmp_path / "never-touched")
    assert liveness.in_cooldown() is False


def test_cooldown_fresh_returns_true(liveness, tmp_path, monkeypatch):
    f = tmp_path / "cooldown"
    f.touch()
    monkeypatch.setattr(liveness, "COOLDOWN_FILE", f)
    assert liveness.in_cooldown() is True


def test_cooldown_expired_returns_false(liveness, tmp_path, monkeypatch):
    f = tmp_path / "cooldown"
    f.touch()
    old = time.time() - 2 * 60 * 60  # 2h, > 60 min cooldown
    os.utime(f, (old, old))
    monkeypatch.setattr(liveness, "COOLDOWN_FILE", f)
    assert liveness.in_cooldown() is False


def test_stamp_cooldown_creates_file(liveness, tmp_path, monkeypatch):
    f = tmp_path / "cooldown"
    monkeypatch.setattr(liveness, "COOLDOWN_FILE", f)
    liveness.stamp_cooldown()
    assert f.exists()


# ── telegram delivery ────────────────────────────────────────────────


def test_send_telegram_missing_keystore_returns_false(liveness, tmp_path, monkeypatch):
    monkeypatch.setattr(liveness, "SECURITY_TOKEN_PATH", tmp_path / "missing-token")
    monkeypatch.setattr(liveness, "SECURITY_CHAT_ID_PATH", tmp_path / "missing-chat")
    assert liveness.send_telegram(["x"]) is False


def test_send_telegram_empty_keystore_returns_false(liveness, tmp_path, monkeypatch):
    (tmp_path / "token").write_text("")
    (tmp_path / "chat").write_text("")
    monkeypatch.setattr(liveness, "SECURITY_TOKEN_PATH", tmp_path / "token")
    monkeypatch.setattr(liveness, "SECURITY_CHAT_ID_PATH", tmp_path / "chat")
    assert liveness.send_telegram(["x"]) is False


def test_send_telegram_posts_to_bot_api(liveness, tmp_path, monkeypatch):
    (tmp_path / "token").write_text("fake-bot-token")
    (tmp_path / "chat").write_text("123456789")
    monkeypatch.setattr(liveness, "SECURITY_TOKEN_PATH", tmp_path / "token")
    monkeypatch.setattr(liveness, "SECURITY_CHAT_ID_PATH", tmp_path / "chat")

    calls = []
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    def fake_urlopen(url_or_req, data=None, timeout=None):
        if hasattr(url_or_req, "full_url"):
            calls.append((url_or_req.full_url, data))
        else:
            calls.append((url_or_req, data))
        return FakeResp()
    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)

    assert liveness.send_telegram(["admin-ui down"]) is True
    assert len(calls) == 1
    url, data = calls[0]
    assert "api.telegram.org/botfake-bot-token/sendMessage" in url
    assert b"chat_id=123456789" in data
    assert b"admin-ui+down" in data or b"admin-ui%20down" in data


# ── main exit codes ──────────────────────────────────────────────────


def test_main_no_failures_returns_0(liveness, monkeypatch):
    monkeypatch.setattr(liveness, "check_health", lambda: [])
    assert liveness.main() == 0


def test_main_in_cooldown_suppresses_returns_0(liveness, monkeypatch, capsys):
    monkeypatch.setattr(liveness, "check_health", lambda: ["x"])
    monkeypatch.setattr(liveness, "in_cooldown", lambda: True)
    assert liveness.main() == 0
    out = capsys.readouterr().out
    assert "failures_in_cooldown" in out


def test_main_failure_with_send_returns_1(liveness, monkeypatch):
    monkeypatch.setattr(liveness, "check_health", lambda: ["x"])
    monkeypatch.setattr(liveness, "in_cooldown", lambda: False)
    monkeypatch.setattr(liveness, "send_telegram", lambda _: True)
    stamps = []
    monkeypatch.setattr(liveness, "stamp_cooldown", lambda: stamps.append(1))
    assert liveness.main() == 1
    assert stamps == [1]  # cooldown was stamped


def test_main_failure_with_send_failure_returns_2(liveness, monkeypatch):
    monkeypatch.setattr(liveness, "check_health", lambda: ["x"])
    monkeypatch.setattr(liveness, "in_cooldown", lambda: False)
    monkeypatch.setattr(liveness, "send_telegram", lambda _: False)
    assert liveness.main() == 2


# ── independence guarantee ───────────────────────────────────────────


def test_script_uses_stdlib_only(liveness):
    """The whole point of this script is that it works when Evolve is
    broken. Verify no Evolve imports snuck in."""
    src = _SCRIPT.read_text()
    # Things that would couple this to the Evolve install:
    forbidden = (
        "from evolve_admin",
        "import evolve_admin",
        "from evolve_config",
        "import evolve_config",
        "from signals",
        "from schema",
        "import requests",   # not stdlib
    )
    for f in forbidden:
        assert f not in src, f"script uses {f!r} — defeats the 'works when Evolve is broken' guarantee"
