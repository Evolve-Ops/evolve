"""tests/test_launchctl_unload_wait.py — _wait_for_launchd_unload helper.

Regression: ``launchctl bootout system/<label>`` returns asynchronously;
an immediate follow-up ``bootstrap`` of the same label fails with
"Service is being unloaded". The helper bridges that gap by polling
``launchctl print system/<label>`` until stdout is empty (service gone).

Apple's CLI returns 0 for both present-and-absent services, so the helper
must key on stdout content, not returncode.

Also covers ``_wait_for_gateway_port`` — the symmetric helper that bridges
the gap on the *other* side of bootstrap: ``launchctl bootstrap`` returns
the moment launchd has accepted the spec, but the Node gateway process
still takes several seconds to fork, init and bind the listening port.
Without that wait the post-deploy smoke audit races the bind and fires
``gateway.probe_failed`` on every fresh-bot deploy (atlas, 2026-05-29).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from evolve_admin import deploy
from evolve_admin.runtime import LaunchdScheduler, set_scheduler


def _proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _inject(runner, tmp_path=None) -> None:
    """Install a LaunchdScheduler with an injected runner as the seam.

    The launchctl verbs in deploy.py flow through the Scheduler seam
    (4.3C S2); without injection the default adapter would spawn REAL
    ``sudo launchctl`` — never allowed from a test.
    """
    kw = {"plist_dir": tmp_path} if tmp_path is not None else {}
    set_scheduler(LaunchdScheduler(runner=runner, **kw))


@pytest.fixture(autouse=True)
def _reset_scheduler():
    yield
    set_scheduler(None)


def test_returns_immediately_when_service_absent():
    """Empty stdout from launchctl print → service gone → return without polling."""
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return (0, "", "")

    _inject(runner)
    deploy._wait_for_launchd_unload("ai.openclaw.test-gateway", timeout_seconds=2.0)

    assert len(calls) == 1, f"Expected 1 print call when service absent, got {len(calls)}"
    assert "print" in calls[0], f"Expected launchctl print, got {calls[0]!r}"


def test_polls_until_unload_completes():
    """Stdout non-empty for N polls, then empty → return after the empty one."""
    poll_count = {"n": 0}

    def runner(argv):
        poll_count["n"] += 1
        # First 3 polls: still loaded. 4th: gone.
        if poll_count["n"] < 4:
            return (0, "system/foo = { ... }", "")
        return (0, "", "")

    _inject(runner)
    with patch.object(deploy.time, "sleep") as fake_sleep:
        deploy._wait_for_launchd_unload("foo", timeout_seconds=2.0)

    assert poll_count["n"] == 4, f"Expected 4 polls, got {poll_count['n']}"
    assert fake_sleep.call_count == 3, (
        f"Expected sleep between the 3 non-final polls, got {fake_sleep.call_count}"
    )


def test_respects_timeout_when_service_never_unloads():
    """Stuck service: helper must return after timeout_seconds, not loop forever."""
    poll_count = {"n": 0}

    def runner(argv):
        poll_count["n"] += 1
        return (0, "system/stuck = { ... }", "")  # always loaded

    # Fake monotonic that advances 0.5s per call; pair with sleep(0.1) → 5 polls
    # before exceeding the 1s budget.
    fake_clock = {"t": 0.0}

    def fake_monotonic():
        fake_clock["t"] += 0.5
        return fake_clock["t"]

    _inject(runner)
    with patch.object(deploy.time, "sleep"):
        with patch.object(deploy.time, "monotonic", side_effect=fake_monotonic):
            deploy._wait_for_launchd_unload("stuck", timeout_seconds=1.0)

    # Bounded: must not poll forever. Anything < 100 means the timeout fired.
    assert poll_count["n"] < 100, (
        f"Helper did not respect timeout — polled {poll_count['n']} times"
    )
    assert poll_count["n"] >= 1, "Helper should poll at least once"


def test_install_bot_gateway_plist_calls_wait_between_bootout_and_bootstrap(tmp_path, monkeypatch):
    """install_bot_gateway_plist must wait between bootout and bootstrap.

    Without the wait, the bootstrap races against the still-unloading
    service. The ritual lives inside Scheduler.install() now; the runner
    records the verb order it issues.
    """
    call_log: list[str] = []

    def runner(argv):
        for verb in ("bootout", "bootstrap", "print"):
            if verb in argv:
                call_log.append(verb)
        # Everything succeeds; "print" returns empty stdout (service gone)
        # so the settle-wait exits its loop on the first poll.
        return (0, "", "")

    _inject(runner, tmp_path)
    # The log-dir mkdir/chown prep still runs through deploy.subprocess.
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: _proc())
    # Short-circuit the post-bootstrap port-bind wait — it would otherwise
    # spin for 20s trying to TCP-connect to a port nothing is listening on.
    monkeypatch.setattr(deploy, "_wait_for_gateway_port", lambda *a, **kw: True)

    ok, _detail = deploy.install_bot_gateway_plist("testbot", 18799, user="testbot")
    assert ok, "install_bot_gateway_plist should succeed when all subprocess calls succeed"

    # The critical ordering: bootout → print (the wait) → bootstrap
    boot_idx = call_log.index("bootout")
    print_idx = call_log.index("print")
    bootstrap_idx = call_log.index("bootstrap")
    assert boot_idx < print_idx < bootstrap_idx, (
        f"Expected bootout → print (wait) → bootstrap; got {call_log!r}"
    )


# ── _wait_for_gateway_port — port-bind wait after bootstrap ─────────────


def test_wait_for_gateway_port_returns_true_on_immediate_connect():
    """First TCP connect succeeds → return True without polling."""
    attempts = {"n": 0}

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def fake_create_connection(addr, timeout=None):
        attempts["n"] += 1
        return _FakeConn()

    import socket
    with patch.object(socket, "create_connection", side_effect=fake_create_connection):
        ok = deploy._wait_for_gateway_port("testbot", 18799, timeout_seconds=5.0)

    assert ok is True
    assert attempts["n"] == 1, f"Expected 1 connect attempt, got {attempts['n']}"


def test_wait_for_gateway_port_polls_until_port_opens():
    """Port refuses for N attempts, then opens → return True after the success."""
    attempts = {"n": 0}

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def fake_create_connection(addr, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise OSError("Connection refused")
        return _FakeConn()

    import socket
    with patch.object(socket, "create_connection", side_effect=fake_create_connection), \
         patch.object(deploy.time, "sleep"):
        ok = deploy._wait_for_gateway_port("testbot", 18799, timeout_seconds=10.0)

    assert ok is True
    assert attempts["n"] == 4, f"Expected 4 connect attempts, got {attempts['n']}"


def test_wait_for_gateway_port_returns_false_on_timeout():
    """Port never opens → return False after timeout, don't loop forever."""
    attempts = {"n": 0}

    def fake_create_connection(addr, timeout=None):
        attempts["n"] += 1
        raise OSError("Connection refused")

    # Fake clock advances 0.6s per monotonic() call so the 1.0s budget
    # is exhausted in a couple of polls.
    fake_clock = {"t": 0.0}

    def fake_monotonic():
        fake_clock["t"] += 0.6
        return fake_clock["t"]

    import socket
    with patch.object(socket, "create_connection", side_effect=fake_create_connection), \
         patch.object(deploy.time, "sleep"), \
         patch.object(deploy.time, "monotonic", side_effect=fake_monotonic):
        ok = deploy._wait_for_gateway_port("testbot", 18799, timeout_seconds=1.0)

    assert ok is False
    assert attempts["n"] < 100, f"Helper did not respect timeout — {attempts['n']} attempts"


def test_install_bot_gateway_plist_returns_false_when_port_never_opens(monkeypatch, tmp_path):
    """If bootstrap succeeds but the gateway never binds the port, the
    install must surface that as a hard failure (not silently report OK)."""

    # All seam launchctl ops succeed; "print" returns empty stdout so the
    # bootout-wait short-circuits.
    _inject(lambda argv: (0, "", ""), tmp_path)
    # Log-dir prep subprocesses succeed too.
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: _proc())
    # The port never accepts connections.
    monkeypatch.setattr(deploy, "_wait_for_gateway_port", lambda *a, **kw: False)

    ok, detail = deploy.install_bot_gateway_plist("testbot", 18799, user="testbot")
    assert ok is False, (
        "install_bot_gateway_plist must return False when the gateway never "
        "binds the port — otherwise the smoke audit races and fires "
        "gateway.probe_failed against a deploy that reported success."
    )
    # The new tuple return must explain WHY — without this the wizard
    # surfaces a generic "manual recovery: launchctl bootstrap" hint that
    # can't help with port-bind failures (the plist is already loaded;
    # bootstrap won't fix it).
    assert "port-bind" in detail
    assert "18799" in detail
