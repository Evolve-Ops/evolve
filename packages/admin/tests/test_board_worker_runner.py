"""Tests for board_worker_runner.py — the daemon entry point.

Not wired into any LaunchDaemon yet (board_worker.py deviation 6); this pins
the CLI shim itself: argument parsing reaches ``run_loop`` with the right
values, the default model client refuses rather than spending against an
unreviewed provider, and SIGTERM/SIGINT exit cleanly instead of leaving the
poll loop running.
"""
from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_worker_runner as runner  # noqa: E402


def test_refusing_model_client_raises_instead_of_spending():
    with pytest.raises(RuntimeError, match="no model client wired"):
        runner._refusing_model_client(prompt="x", context={})


def test_main_parses_args_and_calls_run_loop_with_the_refusing_client(tmp_path, monkeypatch):
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}}))
    calls = []

    def fake_run_loop(shared_dir, network, *, model_client, poll_interval_seconds, stop_after_seconds):
        calls.append({
            "shared_dir": shared_dir, "network": network, "model_client": model_client,
            "poll_interval_seconds": poll_interval_seconds, "stop_after_seconds": stop_after_seconds,
        })
        return 0

    monkeypatch.setattr("evolve_admin.board_worker.run_loop", fake_run_loop)
    rc = runner.main([
        "--shared-dir", str(tmp_path), "--network", str(network_path),
        "--poll-interval", "2.0", "--stop-after", "5.0",
    ])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["shared_dir"] == tmp_path
    assert calls[0]["model_client"] is runner._refusing_model_client
    assert calls[0]["poll_interval_seconds"] == 2.0
    assert calls[0]["stop_after_seconds"] == 5.0


def test_main_refuses_to_start_when_the_rung_gate_self_check_fails(tmp_path, monkeypatch):
    # hold-fix-4279-approval-rung-and-busy-expiry, item 4: the daemon must
    # never install against an inverted approval gate.
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}}))

    def failing_self_check():
        raise AssertionError("board_worker rung gate inverted at 'act_with_approval'")

    monkeypatch.setattr("evolve_admin.board_worker._self_check_rung_gate", failing_self_check)
    rc = runner.main(["--shared-dir", str(tmp_path), "--network", str(network_path)])
    assert rc == 2


def test_main_returns_1_when_run_loop_crashes(tmp_path, monkeypatch):
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}}))

    def crashing_run_loop(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("evolve_admin.board_worker.run_loop", crashing_run_loop)
    rc = runner.main(["--shared-dir", str(tmp_path), "--network", str(network_path)])
    assert rc == 1


def test_sigterm_handler_exits_cleanly_instead_of_continuing_the_loop(tmp_path, monkeypatch):
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}}))

    def run_loop_delivers_sigterm_mid_poll(*a, **k):
        # Simulate the signal arriving while run_loop is polling: invoke
        # whatever handler main() just installed, the way the OS would.
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        raise AssertionError("unreachable — the handler above must exit first")

    monkeypatch.setattr("evolve_admin.board_worker.run_loop", run_loop_delivers_sigterm_mid_poll)
    with pytest.raises(SystemExit) as exc:
        runner.main(["--shared-dir", str(tmp_path), "--network", str(network_path)])
    assert exc.value.code == 0
