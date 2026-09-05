"""tests/test_oc_log_rotate.py

Behavioral tests for evolve_admin.oc_log_rotate — specifically that
truncation failures are loud. Companion to test_sudoers_log_rotate.py,
which pins the §3.1 sudoers grants themselves: if the grants regress
again (the PR #1956 shape, restored in #2664), the daily
ai.evolve.evolve.oc-log-rotate cron must print the sudo error, count
the failure in its summary, and exit non-zero — not report success
while gateway.log / gateway.err.log grow unbounded.

The original bug: _main partitioned results into truncated
(``r.get("ok")``) and skipped (``"skipped" in r``), so a result where
the sudo truncate ran and FAILED (``ok: False``, no "skipped" key)
appeared in neither printed list, and _main returned 0 unconditionally.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import oc_log_rotate  # noqa: E402

_OVER = oc_log_rotate._THRESHOLD_BYTES + 1

_SUDO_DENIED = "sudo: a password is required"


@pytest.fixture
def one_bot_network(monkeypatch):
    """network.json with a single bot 'team-bot-c' whose account name is 'team-bot-c'."""
    from evolve_admin import config

    monkeypatch.setattr(config, "load_network", lambda: {"bots": {"team-bot-c": {}}})
    monkeypatch.setattr(config, "get_bot_user", lambda bot_id, network: bot_id)


def _set_sizes(monkeypatch, size):
    monkeypatch.setattr(oc_log_rotate, "_file_size_with_sudo", lambda path: size)


# ── rotate_one_bot result shape ─────────────────────────────────────


def test_rotate_one_bot_failed_truncate_result_shape(monkeypatch):
    _set_sizes(monkeypatch, _OVER)
    monkeypatch.setattr(oc_log_rotate, "_truncate", lambda path: (False, _SUDO_DENIED))

    results = oc_log_rotate.rotate_one_bot("team-bot-c", "team-bot-c")

    assert len(results) == len(oc_log_rotate._TARGETS)
    for r in results:
        assert r["ok"] is False
        assert r["message"] == _SUDO_DENIED
        assert "skipped" not in r  # failed ≠ skipped — tri-state, not bi-state


# ── _main: failed truncations are loud ──────────────────────────────


def test_failed_truncation_output(one_bot_network, monkeypatch, capsys):
    _set_sizes(monkeypatch, _OVER)
    monkeypatch.setattr(oc_log_rotate, "_truncate", lambda path: (False, _SUDO_DENIED))

    rc = oc_log_rotate._main([])
    captured = capsys.readouterr()

    assert rc == 1
    assert "FAILED to truncate" in captured.err
    assert _SUDO_DENIED in captured.err
    # sudo-denied is actionable — the hint names the recovery command
    assert "sudo evolve-admin refresh-sudoers" in captured.err
    assert "failed=2" in captured.out  # both gateway.log and gateway.err.log
    assert "truncated=0" in captured.out


def test_non_sudo_failure_has_no_refresh_sudoers_hint(
    one_bot_network, monkeypatch, capsys,
):
    msg = "truncate: /Users/team-bot-c/.openclaw/logs/gateway.log: Operation not permitted"
    _set_sizes(monkeypatch, _OVER)
    monkeypatch.setattr(oc_log_rotate, "_truncate", lambda path: (False, msg))

    rc = oc_log_rotate._main([])
    captured = capsys.readouterr()

    assert rc == 1
    assert "FAILED to truncate" in captured.err
    assert msg in captured.err
    assert "refresh-sudoers" not in captured.err


# ── _main: success and skip paths keep exit 0 ───────────────────────


def test_successful_truncation_exits_zero(one_bot_network, monkeypatch, capsys):
    _set_sizes(monkeypatch, _OVER)
    monkeypatch.setattr(oc_log_rotate, "_truncate", lambda path: (True, "truncated"))

    rc = oc_log_rotate._main([])
    captured = capsys.readouterr()

    assert rc == 0
    assert "truncated:" in captured.out
    assert "failed=0" in captured.out
    assert captured.err == ""


def test_under_threshold_is_noop_exit_zero(one_bot_network, monkeypatch, capsys):
    _set_sizes(monkeypatch, 1024)

    def boom(path):
        raise AssertionError("truncate must not run under threshold")

    monkeypatch.setattr(oc_log_rotate, "_truncate", boom)

    rc = oc_log_rotate._main([])
    captured = capsys.readouterr()

    assert rc == 0
    assert "truncated=0 failed=0 skipped=0" in captured.out


def test_stat_failed_skip_exits_zero(one_bot_network, monkeypatch, capsys):
    # stat_failed is normal for bots without gateway logs yet — not a failure
    _set_sizes(monkeypatch, None)

    rc = oc_log_rotate._main([])
    captured = capsys.readouterr()

    assert rc == 0
    assert "skipped=2" in captured.out
    assert "failed=0" in captured.out


# ── _truncate argv matches the pinned sudoers grant ─────────────────


def test_truncate_argv_uses_sudo_n_and_grant_pinned_command(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(oc_log_rotate, "subprocess", SimpleNamespace(run=fake_run))

    path = Path("/Users/team-bot-c/.openclaw/logs/gateway.log")
    ok, msg = oc_log_rotate._truncate(path)

    assert ok is True
    assert calls == [["sudo", "-n", "/usr/bin/truncate", "-s", "0", str(path)]]
    # tail after the sudo flags must match the sudoers needle in
    # test_sudoers_log_rotate.py: /usr/bin/truncate -s 0 <path>
    assert " ".join(calls[0][2:]) == f"/usr/bin/truncate -s 0 {path}"


def test_truncate_failure_returns_stderr(monkeypatch):
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=_SUDO_DENIED + "\n")

    monkeypatch.setattr(oc_log_rotate, "subprocess", SimpleNamespace(run=fake_run))

    ok, msg = oc_log_rotate._truncate(Path("/Users/team-bot-c/.openclaw/logs/gateway.log"))

    assert ok is False
    assert msg == _SUDO_DENIED
