"""PWA Phase 3 — embedded mini terminal route + PTY session contracts.

Pins the behaviour the admin UI's Terminal page depends on:

  GET  /api/terminal/info                 → JSON { enabled, pod_name, now }
  WS   /api/terminal/ws                   → JSON frame protocol over PTY

Most of these tests exercise the ``_Session`` PTY-wrapper directly
(spawn, write, resize, audit-log buffering, teardown) rather than going
through the WebSocket — the WS handler is a thin glue layer over those
primitives. One end-to-end test boots a real Flask + flask-sock server
in a background thread and connects with simple-websocket's ``Client``
so the WS-side wiring (auth gate, ready frame, JSON dispatch) gets
exercised too.

The PTY tests skip cleanly on environments without a working PTY
(very rare on macOS/Linux, but pytest collections on Windows would
fail otherwise).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# Skip the entire module on platforms without pty (Windows). macOS/Linux
# are unaffected — pty is stdlib and always importable.
pty = pytest.importorskip("pty", reason="terminal tests require POSIX pty")


# ── Shared app fixture ─────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "evolve"
    (shared / "signals").mkdir(parents=True)
    network = {
        "networkId": "test-pod",
        "members": [],
        "bots": {},
        "sharedDir": str(shared),
        "alerts": {"channel": "telegram", "chatId": "12345"},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return {"app": app, "shared": shared, "network_path": network_path}


# ── /api/terminal/info ─────────────────────────────────────────────────────


def test_info_reports_enabled_by_default(app):
    c = app["app"].test_client()
    r = c.get("/api/terminal/info")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is True
    assert body["pod_name"] == "test-pod"
    assert isinstance(body["now"], str) and body["now"].endswith("Z")


def test_info_reports_disabled_when_network_flag_set(app):
    # Flip the flag and confirm the JSON reads it back. The admin server
    # re-reads network.json on every request, so a write here is
    # observable immediately — no restart required.
    net = json.loads(app["network_path"].read_text())
    net["terminal"] = {"enabled": False}
    app["network_path"].write_text(json.dumps(net))
    body = app["app"].test_client().get("/api/terminal/info").get_json()
    assert body["enabled"] is False


# ── _Session PTY behaviours ────────────────────────────────────────────────


def _wait_for(predicate, *, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_session_spawn_pipes_input_to_pty(tmp_path):
    """Write ``echo hi\\n`` → read back ``hi`` on PTY stdout."""
    from evolve_admin.web.terminal_routes import _Session

    sess = _Session(shared_dir=tmp_path)
    chunks: list[bytes] = []
    sess.spawn()
    try:
        sess.start_reader(lambda c: chunks.append(c))
        # Give the shell a beat to print its prompt; the prompt itself
        # is not what we're asserting on — we want ``hi`` somewhere in
        # the post-newline output.
        time.sleep(0.4)
        sess.write(b"echo hello-from-test\n")
        assert _wait_for(
            lambda: b"hello-from-test" in b"".join(chunks),
            timeout=3.0,
        ), (
            "expected 'hello-from-test' in PTY stdout; got "
            + repr(b"".join(chunks)[:400])
        )
    finally:
        sess.close()


def test_session_resize_updates_winsize(tmp_path):
    """``resize(rows, cols)`` is reflected by ioctl(TIOCGWINSZ)."""
    from evolve_admin.web.terminal_routes import _Session, _read_winsize_for_test

    sess = _Session(shared_dir=tmp_path, cols=80, rows=24)
    sess.spawn()
    try:
        sess.resize(40, 120)
        rows, cols = _read_winsize_for_test(sess._pty_fd)
        assert (rows, cols) == (40, 120)
    finally:
        sess.close()


def test_session_close_terminates_child(tmp_path):
    """``close()`` SIGHUPs the shell and reaps it — no orphan process."""
    from evolve_admin.web.terminal_routes import _Session

    sess = _Session(shared_dir=tmp_path)
    sess.spawn()
    pid = sess._pid
    sess.close()
    # The child either exited (waitpid in close) or was reaped. Either
    # way `os.kill(pid, 0)` either raises or finds nothing — both are
    # the no-orphan signal.
    time.sleep(0.05)
    alive = True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        alive = False
    except OSError:
        alive = False
    assert not alive, f"PTY child pid={pid} is still alive after close()"


# ── Audit log: \n flushing, not per-keystroke ──────────────────────────────


def test_audit_log_only_writes_on_newline(tmp_path):
    """Per-keystroke input must NOT log; only ``\\n`` flushes a line."""
    from evolve_admin.web.terminal_routes import _Session, _audit_path

    sess = _Session(shared_dir=tmp_path)
    audit = _audit_path(tmp_path)

    # Bypass spawn so the audit path runs purely off ``write``'s
    # tracking logic (the PTY fd isn't needed for the audit assertion).
    sess._track_audit_input("e")
    sess._track_audit_input("c")
    sess._track_audit_input("ho")
    sess._track_audit_input(" hi")
    # No newline yet — audit log must be empty.
    if audit.exists():
        assert audit.read_text() == "", (
            "audit log wrote before newline — would log every keystroke"
        )

    sess._track_audit_input("\n")
    assert audit.is_file()
    lines = [
        json.loads(line)
        for line in audit.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["line"] == "echo hi"
    assert lines[0]["session_id"] == sess.session_id


def test_audit_log_strips_ansi_and_handles_backspace(tmp_path):
    """ANSI sequences from completion redraws + backspaces don't pollute."""
    from evolve_admin.web.terminal_routes import _Session, _audit_path

    sess = _Session(shared_dir=tmp_path)
    audit = _audit_path(tmp_path)

    sess._track_audit_input("ech")
    sess._track_audit_input("\x7f")           # backspace
    sess._track_audit_input("ho \x1b[31mred\x1b[0m hi")
    sess._track_audit_input("\r")             # zsh's Enter is CR

    line = json.loads(audit.read_text().splitlines()[0])["line"]
    assert line == "echo red hi"


def test_audit_log_skips_empty_lines(tmp_path):
    """Pressing Enter at an empty prompt must not spam the audit log."""
    from evolve_admin.web.terminal_routes import _Session, _audit_path

    sess = _Session(shared_dir=tmp_path)
    sess._track_audit_input("\n")
    sess._track_audit_input("\n")
    sess._track_audit_input("   \n")          # whitespace only

    audit = _audit_path(tmp_path)
    if audit.exists():
        assert audit.read_text() == ""


def test_audit_log_mode_is_0600(tmp_path):
    """Audit file is mode 0600 — operator-only, treat as sensitive."""
    from evolve_admin.web.terminal_routes import _Session, _audit_path

    sess = _Session(shared_dir=tmp_path)
    sess._track_audit_input("ls\n")
    audit = _audit_path(tmp_path)
    assert audit.is_file()
    # The two-low-bits-clear check: group/other have no perms.
    mode = audit.stat().st_mode & 0o777
    assert mode == 0o600, f"audit file mode {oct(mode)}, expected 0o600"


def test_session_id_format(tmp_path):
    from evolve_admin.web.terminal_routes import _Session

    sess = _Session(shared_dir=tmp_path)
    assert sess.session_id.startswith("term_")
    assert len(sess.session_id) == len("term_") + 10


# ── End-to-end: real WS through a Flask thread ─────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    """Boot the create_app() Flask app on a real socket in a thread.

    flask-sock can't be exercised via Flask's test_client (it doesn't
    do WebSocket upgrades). We bind to 127.0.0.1 on a random port and
    let simple-websocket's Client connect to it.
    """
    pytest.importorskip("flask_sock")
    pytest.importorskip("simple_websocket")
    from evolve_admin.web.server import create_app
    from werkzeug.serving import make_server

    shared = tmp_path / "evolve"
    (shared / "signals").mkdir(parents=True)
    network = {
        "networkId": "live-pod",
        "members": [],
        "bots": {},
        "sharedDir": str(shared),
        "alerts": {"channel": "telegram", "chatId": "12345"},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)

    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Wait for socket to accept connections.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    yield {"port": port, "shared": shared, "network_path": network_path}
    server.shutdown()
    thread.join(timeout=2.0)


def test_ws_roundtrips_echo_command(live_server):
    """End-to-end: connect, type ``echo``, read back the output."""
    from simple_websocket import Client

    url = f"ws://127.0.0.1:{live_server['port']}/api/terminal/ws"
    ws = Client.connect(url)
    try:
        # ready frame
        ready_seen = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            msg = ws.receive(timeout=2.0)
            if not msg:
                continue
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            if payload.get("type") == "ready":
                ready_seen = True
                assert payload["pod_name"] == "live-pod"
                assert payload["session_id"].startswith("term_")
                break
        assert ready_seen, "server never sent {type:'ready'} frame"

        # Send a command and collect output until we see our token.
        ws.send(json.dumps({
            "type": "input",
            "data": "echo TERMINAL_ROUNDTRIP_OK\n",
        }))
        collected = ""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            msg = ws.receive(timeout=2.0)
            if not msg:
                continue
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            if payload.get("type") == "output":
                collected += payload.get("data", "")
                if "TERMINAL_ROUNDTRIP_OK" in collected:
                    break
        assert "TERMINAL_ROUNDTRIP_OK" in collected, (
            f"echo output not received; collected={collected!r}"
        )
    finally:
        try:
            ws.close()
        except Exception:
            pass


def test_ws_closes_when_terminal_disabled(live_server):
    """``terminal.enabled = false`` → WS sends an error frame and closes."""
    from simple_websocket import Client, ConnectionClosed

    # Flip the flag at runtime — the handler re-reads on connect.
    net = json.loads(live_server["network_path"].read_text())
    net["terminal"] = {"enabled": False}
    live_server["network_path"].write_text(json.dumps(net))

    url = f"ws://127.0.0.1:{live_server['port']}/api/terminal/ws"
    ws = Client.connect(url)
    try:
        # Expect one error frame, then a close.
        got_error = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                msg = ws.receive(timeout=1.0)
            except ConnectionClosed:
                break
            if not msg:
                continue
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            if payload.get("type") == "error" and payload.get("code") == "disabled":
                got_error = True
                break
        assert got_error, "WS did not send an error frame when disabled"
    finally:
        try:
            ws.close()
        except Exception:
            pass


def test_ws_sends_error_frame_when_spawn_fails(live_server, monkeypatch):
    """Silent-failure guard: PTY spawn errors send an explicit error frame.

    Without this, a failing ``pty.fork`` would close the WS with no
    explanation and the client would render a blank terminal forever.
    """
    from simple_websocket import Client, ConnectionClosed
    from evolve_admin.web import terminal_routes as _tr

    def _boom(self):  # noqa: ARG001
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(_tr._Session, "spawn", _boom)

    url = f"ws://127.0.0.1:{live_server['port']}/api/terminal/ws"
    ws = Client.connect(url)
    try:
        got_error = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                msg = ws.receive(timeout=1.0)
            except ConnectionClosed:
                break
            if not msg:
                continue
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            if payload.get("type") == "error" and payload.get("code") == "spawn_failed":
                got_error = True
                break
        assert got_error, (
            "WS did not send {type:'error', code:'spawn_failed'} when "
            "_Session.spawn raised — client would see a blank terminal"
        )
    finally:
        try:
            ws.close()
        except Exception:
            pass


def test_session_close_is_idempotent(tmp_path):
    """``close()`` called twice must not raise or double-reap.

    The WS handler's ``finally: sess.close()`` runs after the reader
    thread's ``finally: self.close()``. If close() weren't idempotent,
    the second call would either raise on a dead pid (and strand the
    fd) or silently re-waitpid on an unrelated process recycled into
    the same pid.
    """
    from evolve_admin.web.terminal_routes import _Session

    sess = _Session(shared_dir=tmp_path)
    sess.spawn()
    sess.close()
    # Second close must be a no-op — no exception, no double-kill.
    sess.close()
    sess.close()


def test_ws_resize_message_updates_pty(live_server):
    """resize() message takes effect — verify the audit-log includes it on a follow-up command."""
    # Indirect proof: we can't peek into the live server's PTY winsize
    # from outside, but we can confirm the shell received the size by
    # running ``stty size`` (which prints rows + cols). If the resize
    # never reached the PTY, ``stty size`` would print the default 24
    # 80, not our requested values.
    from simple_websocket import Client

    url = f"ws://127.0.0.1:{live_server['port']}/api/terminal/ws"
    ws = Client.connect(url)
    try:
        # Drain ready frame.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = ws.receive(timeout=1.0)
            if not msg:
                continue
            try:
                if json.loads(msg).get("type") == "ready":
                    break
            except Exception:
                continue

        ws.send(json.dumps({"type": "resize", "rows": 33, "cols": 137}))
        # Give the resize a beat to propagate, then ask the shell.
        time.sleep(0.2)
        ws.send(json.dumps({"type": "input", "data": "stty size\n"}))

        collected = ""
        deadline = time.monotonic() + 5.0
        seen = False
        while time.monotonic() < deadline:
            msg = ws.receive(timeout=1.0)
            if not msg:
                continue
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            if payload.get("type") == "output":
                collected += payload.get("data", "")
                if "33 137" in collected:
                    seen = True
                    break
        assert seen, f"stty size did not report 33 137; got {collected!r}"
    finally:
        try:
            ws.close()
        except Exception:
            pass
