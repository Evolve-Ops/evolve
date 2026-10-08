"""tests/test_evo_admin_client_fd_hygiene.py — the unix-socket client
closes its socket on every failed connect().

``_UnixSocketHTTPConnection.connect()`` allocates an AF_UNIX socket and
then connects it. Before this guard, a failed ``connect()`` raised
``AdminDaemonUnavailable`` without closing the socket: the fd stayed
reachable only through the raised exception's traceback frame, so it
survived until the caller dropped that traceback — longer still for
callers that log with ``exc_info`` or stash the exception.

That is exactly the wrong behaviour during an fd-exhaustion storm. When
the admin daemon's listener is down, EVERY call takes the failure path,
so the client burns a fresh fd per retry while the box is already out of
them (the 2026-07-21..26 EMFILE incident, #3446).

These tests assert the close, not the reclaim — a refcount-driven
``__del__`` would mask the bug on CPython while still leaking under any
caller that retains the traceback.
"""

from __future__ import annotations

import socket

import pytest

from evolve_admin.evo.admin_client import (
    AdminDaemonUnavailable,
    _UnixSocketHTTPConnection,
)


class _FakeSocket:
    """Records close() and raises a caller-chosen error from connect()."""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def connect(self, _path):
        raise self._exc

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch):
    """Install a _FakeSocket factory; returns a setter for the raised error."""
    holder: dict = {}

    def _install(exc: BaseException) -> _FakeSocket:
        sock = _FakeSocket(exc)
        holder["sock"] = sock
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: sock)
        return sock

    return _install


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("no such socket"),
        ConnectionRefusedError("listener wedged"),
        PermissionError("socket not writable by this uid"),
    ],
)
def test_socket_closed_when_connect_fails_with_translated_error(fake_socket, exc):
    """The three translated errors close the fd before raising."""
    sock = fake_socket(exc)
    conn = _UnixSocketHTTPConnection("/tmp/does-not-exist.sock", timeout=1.0)

    with pytest.raises(AdminDaemonUnavailable):
        conn.connect()

    assert sock.closed, f"fd leaked on {type(exc).__name__}"


def test_socket_closed_when_connect_fails_with_untranslated_error(fake_socket):
    """A timeout is NOT one of the three translated types — it propagates
    unchanged, and must still not strand the fd. This is the case the
    original three-type ``except`` clause missed entirely."""
    sock = fake_socket(TimeoutError("connect timed out"))
    conn = _UnixSocketHTTPConnection("/tmp/does-not-exist.sock", timeout=1.0)

    with pytest.raises(TimeoutError):
        conn.connect()

    assert sock.closed, "fd leaked on an untranslated connect() error"


def test_successful_connect_keeps_the_socket_open(fake_socket, monkeypatch):
    """The guard must not over-close: a successful connect keeps the fd and
    publishes it as ``self.sock`` for HTTPConnection to use."""

    class _OkSocket(_FakeSocket):
        def connect(self, _path):
            return None

    sock = _OkSocket(RuntimeError("unused"))
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: sock)

    conn = _UnixSocketHTTPConnection("/tmp/whatever.sock", timeout=1.0)
    conn.connect()

    assert not sock.closed
    assert conn.sock is sock
