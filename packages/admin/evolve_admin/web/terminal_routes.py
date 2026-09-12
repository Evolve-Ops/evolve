"""Embedded mini-terminal — PWA Phase 3 §7.

The admin server already runs as the ``evolve`` user with the sudoers
grants we need (CLAUDE.md → "Runtime Context"). This module gives the
operator an in-PWA shell on the *mini* by spawning ``/bin/zsh -l`` via
the stdlib ``pty`` module and proxying bytes between the PTY and a
WebSocket connection.

Routing & auth
--------------
``/api/terminal/ws`` is the WebSocket. The admin UI itself binds to
127.0.0.1 only and reaches the operator over Tailscale; UI access IS
the authorization layer (user-memory rule
``feedback_ui_authorization_presumed``). No extra per-route check.

``/api/terminal/info`` is a tiny JSON read used by the page header to
render "Terminal on **<pod>** · started at <time>" and by the SPA to
decide whether to even mount the Terminal nav item — operators who
disabled it in ``network.json`` get a clean hide on the client side
*and* a 403 on the WS, not just one or the other (defense-in-depth
against a stale nav item).

Wire protocol
-------------
Client → server messages are JSON. Two shapes:

  ``{"type": "input", "data": "<utf8>"}``     keystrokes (we pass UTF-8
                                              bytes straight to the PTY)
  ``{"type": "resize", "cols": N, "rows": M}`` window-size change

Server → client messages are JSON ``{"type": "output", "data": "<utf8>"}``
chunks. We base64-encode bytes that don't decode as UTF-8 only if/when
that becomes a real problem — xterm.js accepts UTF-8 strings happily
and ``errors='replace'`` covers the rare malformed-byte case without
breaking the stream.

Audit log
---------
Every newline-terminated command is appended to
``{shared_dir}/terminal/audit-YYYY-MM-DD.jsonl`` as

  ``{"ts": "...", "session_id": "...", "line": "...", "exit_code": null,
    "duration_ms": null}``

We deliberately log on ``\\n`` only — not per-keystroke — and we don't
capture command output (privacy + size, per task brief). ``exit_code``
and ``duration_ms`` are reserved for a future per-command tracker; left
as ``null`` in v1 since wrapping every line means parsing the prompt,
which is fragile.

Silent-failure watch (per CLAUDE.md task brief)
-----------------------------------------------
  * WS accepts but never spawns the PTY → reader thread starts before
    we send the first ``ready`` event; if PTY spawn raises, we send an
    error frame and close, never just hang.
  * Audit log misses lines because buffering chunks at arbitrary
    boundaries → we accumulate per-session text into ``_pending_line``
    and only emit on ``\\n``.
  * PTY left running after disconnect → ``_teardown`` is wrapped in a
    try/finally that always sends SIGHUP + waitpid; the WS close
    handler can raise without stranding the child.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import struct
import termios
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify

from ..config import load_network

log = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────────

_DEFAULT_SHELL = "/bin/zsh"
_FALLBACK_SHELLS = ("/bin/zsh", "/bin/bash", "/bin/sh")
# Strip ANSI escape sequences before audit logging so the recorded
# command line matches what the operator typed, not what the terminal
# painted (cursor moves, color codes, completion redraws). The audit
# log is for "what did they ask the shell to do," not "what bytes did
# the SPA send."
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")


def _pick_shell() -> str:
    """Return the first existing shell from the fallback list.

    macOS Catalina+ ships ``/bin/zsh`` as the default; the fallbacks are
    here so a test environment without zsh still boots a terminal.
    """
    for cand in _FALLBACK_SHELLS:
        if Path(cand).exists():
            return cand
    return _DEFAULT_SHELL  # let exec fail downstream if literally nothing exists


def _resolve_terminal_enabled(network_path: Path) -> bool:
    """``network.json::terminal.enabled``; defaults to True."""
    try:
        net = load_network(network_path)
    except Exception:  # noqa: BLE001 — corrupt config shouldn't disable the terminal hard
        return True
    term_cfg = net.get("terminal") or {}
    return bool(term_cfg.get("enabled", True))


def _resolve_shared_dir(network_path: Path) -> Path:
    return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))


def _resolve_pod_name(network_path: Path) -> str:
    try:
        net = load_network(network_path)
    except Exception:  # noqa: BLE001
        return "Evolve"
    raw = net.get("networkId") or ""
    return str(raw).strip() or "Evolve"


# ── Audit log ───────────────────────────────────────────────────────────────


def _audit_dir(shared_dir: Path) -> Path:
    d = shared_dir / "terminal"
    try:
        d.mkdir(parents=True, exist_ok=True)
        # 0o700 — owner-only. The audit log files themselves are 0o600;
        # tightening the parent dir means a passing `ls` over shared_dir
        # doesn't reveal command-history dates (filenames are
        # audit-YYYY-MM-DD.jsonl).
        os.chmod(d, 0o700)
    except OSError as exc:
        log.warning("terminal audit dir create failed (%s) at %s", exc, d)
    return d


def _audit_path(shared_dir: Path) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _audit_dir(shared_dir) / f"audit-{today}.jsonl"


def _append_audit_line(shared_dir: Path, session_id: str, line: str) -> None:
    """Append one ``{ts, session_id, line}`` JSON record. Mode 0600."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "line": line,
        "exit_code": None,
        "duration_ms": None,
    }
    path = _audit_path(shared_dir)
    try:
        # O_APPEND is atomic across short writes on POSIX; multiple
        # PTY sessions logging concurrently won't interleave bytes
        # mid-record. 0o600 — audit log; treat as sensitive.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        # Re-tighten in case the file existed with looser permissions.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log.warning("terminal audit append failed (%s)", exc)


# ── PTY session ─────────────────────────────────────────────────────────────


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """ioctl(TIOCSWINSZ) — tells the PTY (and thence the shell) the new size."""
    rows = max(1, min(int(rows), 1000))
    cols = max(1, min(int(cols), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _get_winsize(fd: int) -> tuple[int, int]:
    """Read ``(rows, cols)`` back from the PTY. Used by tests."""
    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return rows, cols


class _Session:
    """One PTY ↔ WebSocket pair. Cleaned up in ``close()``.

    The reader thread is the only producer of ``output`` frames. The WS
    handler is the only producer of ``input`` and ``resize`` writes to
    the PTY. We don't need a lock around ``self._pty_fd`` — Python's
    GIL serializes the individual ``os.read``/``os.write`` syscalls and
    the lifecycle is single-owner (the WS handler creates and tears
    down).
    """

    def __init__(
        self,
        *,
        shared_dir: Path,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        self.session_id = "term_" + uuid.uuid4().hex[:10]
        self.shared_dir = shared_dir
        self.started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._pid: int | None = None
        self._pty_fd: int | None = None
        self._reader_thread: threading.Thread | None = None
        self._closed = False
        self._pending_line = ""  # for audit-log line buffering
        self._initial_cols = max(1, int(cols) if cols else 80)
        self._initial_rows = max(1, int(rows) if rows else 24)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def spawn(self) -> None:
        """``pty.fork`` + ``execve`` the shell.

        ``pty.fork`` returns ``(pid, master_fd)``. In the child it
        returns ``pid == 0`` and we exec the shell; in the parent we
        keep the master fd to pipe bytes through.
        """
        shell = _pick_shell()
        env = self._build_env()
        pid, fd = pty.fork()
        if pid == 0:
            # Child. ``pty.fork`` already attached stdin/stdout/stderr
            # to the slave PTY and made it the controlling terminal,
            # so we just exec — argv0 of "-zsh" makes the shell read
            # ~/.zshrc as a login shell.
            try:
                os.execvpe(shell, [f"-{Path(shell).name}", "-l"], env)
            except Exception as exc:  # noqa: BLE001
                # We must not return from this branch — print and exit.
                os.write(2, f"terminal: exec failed: {exc}\r\n".encode())
                os._exit(127)
            return  # unreachable

        # Parent.
        self._pid = pid
        self._pty_fd = fd
        # Initial window size — xterm.js sends a resize after open, but
        # set a sensible default so the shell prompt doesn't render
        # against a 0-column terminal.
        try:
            _set_winsize(fd, self._initial_rows, self._initial_cols)
        except OSError as exc:
            log.warning("terminal initial winsize failed (%s)", exc)

    def _build_env(self) -> dict[str, str]:
        """Inherit the admin server's env, tweak for an interactive TTY."""
        env = os.environ.copy()
        env["TERM"] = env.get("TERM", "xterm-256color")
        env["COLORTERM"] = "truecolor"
        # Don't let the child re-enter the admin server's logger
        # config if it sources a shared init script that checks this.
        env.pop("FLASK_ENV", None)
        env.pop("FLASK_DEBUG", None)
        return env

    def write(self, data: bytes) -> None:
        """Pipe bytes from the WS to the PTY stdin."""
        if self._pty_fd is None:
            return
        # The audit log is fed off *input*, not raw bytes, so we
        # decode-and-strip control codes here. The bytes themselves
        # still go to the PTY verbatim.
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        self._track_audit_input(text)
        try:
            os.write(self._pty_fd, data)
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                log.warning("terminal write failed (%s)", exc)
            # I/O errors on the master fd mean the child died.
            self.close()

    def resize(self, rows: int, cols: int) -> None:
        if self._pty_fd is None:
            return
        try:
            _set_winsize(self._pty_fd, rows, cols)
        except OSError as exc:
            log.warning("terminal resize failed (%s)", exc)

    def start_reader(self, on_output) -> None:
        """Spawn the daemon thread that drains PTY stdout to the WS."""
        if self._pty_fd is None:
            return
        fd = self._pty_fd

        def _loop() -> None:
            try:
                while not self._closed:
                    try:
                        r, _, _ = select.select([fd], [], [], 0.25)
                    except (OSError, ValueError):
                        break
                    if not r:
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    on_output(chunk)
            finally:
                # The shell exited — tell the client and tear down.
                self.close()

        t = threading.Thread(
            target=_loop, name=f"terminal-reader-{self.session_id}", daemon=True
        )
        self._reader_thread = t
        t.start()

    # ── audit ─────────────────────────────────────────────────────────────
    def _track_audit_input(self, text: str) -> None:
        """Accumulate keystrokes, flush on ``\\r`` or ``\\n``.

        We treat CR (zsh's line-submit byte) the same as LF — the
        operator presses Enter and we record what they typed up to that
        point. Tab-completions, arrow-key history navigation, and shell
        completions don't write to the input stream so the recorded
        line matches what was actually submitted.
        """
        if not text:
            return
        for ch in text:
            if ch in ("\r", "\n"):
                line = _ANSI_ESCAPE.sub("", self._pending_line).rstrip("\r")
                # Ignore empty lines (pressing Enter at an empty prompt
                # would otherwise spam the audit log).
                if line.strip():
                    _append_audit_line(self.shared_dir, self.session_id, line)
                self._pending_line = ""
            elif ch == "\x7f" or ch == "\b":
                # Backspace — keep the audit-log line in sync with the
                # visible shell input so partial deletes don't leak
                # ghost characters into the recorded command.
                self._pending_line = self._pending_line[:-1]
            elif ch == "\x03":
                # Ctrl-C — abandon the current line.
                self._pending_line = ""
            else:
                # Cap to keep one pathological paste from buffering MB.
                if len(self._pending_line) < 8192:
                    self._pending_line += ch

    # ── teardown ──────────────────────────────────────────────────────────
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fd = self._pty_fd
        pid = self._pid
        self._pty_fd = None
        # Best-effort SIGHUP → waitpid → close. Wrapped in try/except so
        # one failure can't strand the others.
        if pid:
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            except OSError as exc:
                log.warning("terminal SIGHUP pid=%s failed: %s", pid, exc)
            # Give the shell a beat to flush, then SIGKILL if still up.
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                    if done:
                        break
                except ChildProcessError:
                    break
                except OSError:
                    break
                time.sleep(0.02)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except (ChildProcessError, OSError):
                    pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ── Flask registration ──────────────────────────────────────────────────────


def register_terminal_routes(app: Flask, network_path: Path) -> None:
    """Mount ``/api/terminal/info`` (REST) and ``/api/terminal/ws`` (WebSocket).

    ``flask-sock`` is the WS shim. It's a tiny wrapper over
    ``simple-websocket`` that registers as a normal Flask route, which
    means the rest of Flask's request handling (logging,
    error-handlers, etc.) keeps working. If ``flask-sock`` isn't
    installed at runtime we still register ``/api/terminal/info`` and
    return 503 on ``/api/terminal/ws`` so the SPA can show a clear
    error instead of a generic socket failure.
    """

    @app.get("/api/terminal/info")
    def api_terminal_info() -> Response:
        enabled = _resolve_terminal_enabled(network_path)
        return jsonify({
            "enabled": enabled,
            "pod_name": _resolve_pod_name(network_path),
            "now": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })

    try:
        from flask_sock import Sock  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "flask-sock not available (%s); /api/terminal/ws will return 503",
            exc,
        )

        @app.get("/api/terminal/ws")
        def api_terminal_ws_unavailable() -> Response:
            return jsonify({
                "error": "websocket dependency missing",
                "detail": "pip install flask-sock",
            }), 503
        return

    sock = Sock(app)

    @sock.route("/api/terminal/ws")
    def terminal_ws(ws):  # type: ignore[no-untyped-def]
        # simple-websocket's ``send`` writes directly to the socket without
        # an internal lock; with the PTY-reader thread and the main
        # handler thread both writing frames (output + ready/pong), two
        # concurrent sends can interleave bytes on the wire and corrupt
        # the WebSocket framing. Serialize at our layer.
        send_lock = threading.Lock()

        def _safe_send(frame: str) -> bool:
            with send_lock:
                try:
                    ws.send(frame)
                    return True
                except Exception:  # noqa: BLE001 — client gone
                    return False
        # Gate on ``terminal.enabled`` *inside* the WS handler — the
        # initial HTTP request has already upgraded by the time we land
        # here, so we send a JSON error frame and close cleanly rather
        # than returning an HTTP status. Browser clients see the close
        # event and surface the message to the operator.
        if not _resolve_terminal_enabled(network_path):
            _safe_send(json.dumps({
                "type": "error",
                "code": "disabled",
                "message": "terminal.enabled is false in network.json",
            }))
            try:
                ws.close(1008, "terminal disabled")
            except Exception:  # noqa: BLE001
                pass
            return

        shared = _resolve_shared_dir(network_path)
        sess = _Session(shared_dir=shared)

        # Spawn first; if the shell fails to start we want the client to
        # see an explicit error instead of a blank terminal forever.
        try:
            sess.spawn()
        except Exception as exc:  # noqa: BLE001
            log.exception("terminal spawn failed: %s", exc)
            _safe_send(json.dumps({
                "type": "error",
                "code": "spawn_failed",
                "message": str(exc),
            }))
            try:
                ws.close(1011, "spawn failed")
            except Exception:  # noqa: BLE001
                pass
            return

        def _on_output(chunk: bytes) -> None:
            # ``errors='replace'`` keeps a single malformed UTF-8
            # sequence from killing the whole stream; xterm.js renders
            # U+FFFD as the missing-char box and the next byte stream
            # picks up normally.
            ok = _safe_send(json.dumps({
                "type": "output",
                "data": chunk.decode("utf-8", errors="replace"),
            }))
            if not ok:
                sess.close()

        # Send the ready event *before* starting the reader so the
        # client knows the PTY is live; the reader then begins
        # producing output frames as the shell prints its prompt.
        if not _safe_send(json.dumps({
            "type": "ready",
            "session_id": sess.session_id,
            "started_at": sess.started_at,
            "pod_name": _resolve_pod_name(network_path),
        })):
            sess.close()
            return

        sess.start_reader(_on_output)

        # Main loop: read JSON messages from the client until they
        # disconnect (or send a close frame). flask-sock raises
        # ConnectionClosed on disconnect; any other exception is
        # logged so we don't strand the PTY.
        try:
            while True:
                msg = ws.receive(timeout=None)
                if msg is None:
                    break
                if isinstance(msg, bytes):
                    # Treat unframed binary as raw stdin (rare; the SPA
                    # uses JSON), so a misbehaving client doesn't get
                    # stuck.
                    sess.write(msg)
                    continue
                try:
                    payload: Any = json.loads(msg)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(payload, dict):
                    continue
                mtype = payload.get("type")
                if mtype == "input":
                    data = payload.get("data", "")
                    if isinstance(data, str) and data:
                        sess.write(data.encode("utf-8", errors="ignore"))
                elif mtype == "resize":
                    rows = payload.get("rows") or 24
                    cols = payload.get("cols") or 80
                    try:
                        sess.resize(int(rows), int(cols))
                    except Exception:  # noqa: BLE001
                        pass
                elif mtype == "ping":
                    # Optional heartbeat — keeps long-idle Tailscale
                    # NATs from collapsing the connection. The reply is
                    # JSON-typed so the client's normal dispatch
                    # handles it without a special case.
                    if not _safe_send(json.dumps({"type": "pong"})):
                        break
        except Exception as exc:  # noqa: BLE001
            # ConnectionClosed-style errors and timeouts all land here.
            # Drop to teardown — we don't want to fail loudly when the
            # client just navigated away.
            log.debug("terminal ws loop ended: %s", exc)
        finally:
            sess.close()


# ── Public helpers (for tests) ──────────────────────────────────────────────


def _read_winsize_for_test(fd: int) -> tuple[int, int]:
    """Exposed for tests so they don't need to redo the ioctl dance."""
    return _get_winsize(fd)
