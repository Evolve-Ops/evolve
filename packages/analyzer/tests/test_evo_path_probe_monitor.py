"""Unit tests for evo_path_probe_monitor.

Cover the probe's HTTP/socket client faithfulness, its PASS/FAIL evaluation
for every transport outcome, and the pod-wide Signal emit/sweep/coalesce
behavior. These use hand-rolled canned TCP + unix-socket responders so the
probe is exercised over a REAL wire (the faithfulness that matters) without
standing up the whole admin app — the real-auth-gate end-to-end RED/GREEN
reproduction of the #3257 outage lives in
packages/admin/tests/test_evo_path_probe_integration.py.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import evo_path_probe_monitor as probe  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── A canned HTTP responder over TCP or AF_UNIX ───────────────────────────────


def _read_http_request(conn: socket.socket) -> bytes:
    """Read one HTTP request (headers + Content-Length body) off a socket."""
    conn.settimeout(2.0)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return data
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        content_length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        while len(rest) < content_length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest
    except OSError:
        return data


class CannedServer:
    """A one-route HTTP server returning a fixed status + body.

    family="tcp" binds 127.0.0.1:0; family="unix" binds the given socket_path.
    Records every received raw request in ``.captured``.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body_obj: object | None = None,
        raw_body: bytes | None = None,
        family: str = "tcp",
        socket_path: str | None = None,
    ):
        self.status = status
        if raw_body is not None:
            self.body = raw_body
        elif body_obj is not None:
            self.body = json.dumps(body_obj).encode("utf-8")
        else:
            self.body = b""
        self.family = family
        self.socket_path = socket_path
        self.captured: list[bytes] = []
        self._stop = threading.Event()
        if family == "tcp":
            self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._srv.bind(("127.0.0.1", 0))
            self.host, self.port = self._srv.getsockname()
        else:
            self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._srv.bind(socket_path)
            self.host, self.port = "", 0
        self._srv.listen(8)
        self._srv.settimeout(0.25)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                self.captured.append(_read_http_request(conn))
                resp = (
                    f"HTTP/1.0 {self.status} STATUS\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(self.body)}\r\n\r\n"
                ).encode("utf-8") + self.body
                try:
                    conn.sendall(resp)
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        if self.family == "unix" and self.socket_path:
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def __enter__(self) -> "CannedServer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


_GOOD_ENVELOPE = {
    "subcommand": "help",
    "role": "primary",
    "mode": "speak",
    "system_append": "Respond verbatim: available evo commands…",
    "direct_send_message": "Available evo commands:\n • evo help",
    "wizard_session_id": None,
    "subcommand_brief": "show available evo commands",
    "session_tier_override": None,
}


@pytest.fixture
def short_socket_path():
    """Short /tmp AF_UNIX path (macOS caps AF_UNIX paths at ~104 chars)."""
    import tempfile
    fd, name = tempfile.mkstemp(prefix="evop-", suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(name)
    yield name
    try:
        os.unlink(name)
    except OSError:
        pass


@pytest.fixture
def shared(tmp_path):
    s = tmp_path / "shared"
    signals_store.signals_root(s).mkdir(parents=True, exist_ok=True)
    return s


# ── TCP probe ─────────────────────────────────────────────────────────────────


def test_tcp_green_on_200_envelope():
    with CannedServer(status=200, body_obj=_GOOD_ENVELOPE) as srv:
        out = probe.probe_tcp(bot_id="evolve", host=srv.host, port=srv.port, timeout=2)
    assert out.ok is True
    assert out.http_status == 200
    assert out.envelope_ok is True
    assert out.mode == "speak"
    assert out.transport == "tcp"


def test_tcp_sends_cookieless_post_with_plugin_body():
    """Faithfulness invariant: the probe sends EvoDispatchClient's exact body
    over the wire and NO Cookie header. An in-process call would carry no wire
    request at all and bypass the auth gate — this is what catches #3257."""
    with CannedServer(status=200, body_obj=_GOOD_ENVELOPE) as srv:
        probe.probe_tcp(bot_id="team_bot_a", host=srv.host, port=srv.port, timeout=2)
        raw = srv.captured[0]
    head, _, body = raw.partition(b"\r\n\r\n")
    request_line = head.split(b"\r\n", 1)[0]
    assert request_line == b"POST /api/evo/dispatch HTTP/1.1"
    assert b"cookie" not in head.lower(), "probe must send NO device cookie"
    sent = json.loads(body)
    assert sent == {
        "bot_id": "team_bot_a",
        "channel": "telegram",
        "sender_external_id": "evo-path-probe",
        "raw_text": "evo help",
    }


def test_tcp_red_on_401_then_green_after_fix():
    """The #3257 both-directions reproduction at the probe layer: a 401 (auth
    gate, no exemption) is RED; once the path returns a 200 envelope (the fix),
    it is GREEN."""
    # Auth enforced, /api/evo/dispatch NOT exempt → cookieless TCP gets 401.
    with CannedServer(status=401, body_obj={"error": "device not paired"}) as srv:
        red = probe.probe_tcp(bot_id="evolve", host=srv.host, port=srv.port, timeout=2)
    assert red.ok is False
    assert red.http_status == 401
    assert red.error == "http:401"

    # Fix applied (path exempt / auth off) → 200 + well-formed envelope.
    with CannedServer(status=200, body_obj=_GOOD_ENVELOPE) as srv:
        green = probe.probe_tcp(bot_id="evolve", host=srv.host, port=srv.port, timeout=2)
    assert green.ok is True


def test_tcp_red_on_empty_envelope():
    with CannedServer(status=200, body_obj={"mode": "speak"}) as srv:
        out = probe.probe_tcp(bot_id="evolve", host=srv.host, port=srv.port, timeout=2)
    assert out.ok is False
    assert out.error == "bad-envelope"
    assert "empty envelope" in out.detail


def test_tcp_red_on_mode_missing():
    with CannedServer(status=200, body_obj={"system_append": "hi"}) as srv:
        out = probe.probe_tcp(bot_id="evolve", host=srv.host, port=srv.port, timeout=2)
    assert out.ok is False
    assert "mode" in out.detail


def test_tcp_red_on_non_json():
    with CannedServer(status=200, raw_body=b"<html>not json</html>") as srv:
        out = probe.probe_tcp(bot_id="evolve", host=srv.host, port=srv.port, timeout=2)
    assert out.ok is False
    assert out.error == "non-json"


def test_tcp_red_on_connection_refused():
    # Bind then immediately close to get a port nothing is listening on.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _, port = s.getsockname()
    s.close()
    out = probe.probe_tcp(bot_id="evolve", host="127.0.0.1", port=port, timeout=2)
    assert out.ok is False
    assert out.error.startswith("connect:")


# ── unix-socket probe ─────────────────────────────────────────────────────────


def test_socket_green_on_200(short_socket_path):
    with CannedServer(
        status=200, body_obj=_GOOD_ENVELOPE, family="unix",
        socket_path=short_socket_path,
    ):
        out = probe.probe_unix_socket(short_socket_path, bot_id="evolve", timeout=2)
    assert out.ok is True
    assert out.http_status == 200
    assert out.envelope_ok is True


def test_socket_green_on_401_reachable(short_socket_path):
    """401 over the socket = the daemon is serving but did not exempt this
    uid (expected when only the evo peer is trusted). The socket transport is
    healthy, so this is GREEN — auth over the socket is the TCP probe's job."""
    with CannedServer(
        status=401, body_obj={"error": "device not paired"}, family="unix",
        socket_path=short_socket_path,
    ):
        out = probe.probe_unix_socket(short_socket_path, bot_id="evolve", timeout=2)
    assert out.ok is True
    assert out.http_status == 401


def test_socket_red_on_500(short_socket_path):
    with CannedServer(
        status=500, body_obj={"error": "boom"}, family="unix",
        socket_path=short_socket_path,
    ):
        out = probe.probe_unix_socket(short_socket_path, bot_id="evolve", timeout=2)
    assert out.ok is False
    assert out.http_status == 500


def test_socket_red_on_missing_path_enoent(short_socket_path):
    # short_socket_path fixture deletes the placeholder; nothing is bound.
    out = probe.probe_unix_socket(short_socket_path, bot_id="evolve", timeout=2)
    assert out.ok is False
    assert out.error == "connect:ENOENT"
    assert "ENOENT" in out.detail


def test_socket_non_http_response_is_red(short_socket_path):
    """A peer that answers with bytes that are NOT an HTTP response → RED."""
    # Hand-roll a server that returns raw non-HTTP bytes.
    stop = threading.Event()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(short_socket_path)
    srv.listen(4)
    srv.settimeout(0.25)

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                _read_http_request(conn)
                try:
                    conn.sendall(b"\x00\x01 not an http reply")
                except OSError:
                    pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        out = probe.probe_unix_socket(short_socket_path, bot_id="evolve", timeout=2)
    finally:
        stop.set()
        srv.close()
        t.join(timeout=2)
    assert out.ok is False
    assert out.error == "non-http"


# ── Signal emit / sweep / coalesce ────────────────────────────────────────────


def _firing(shared):
    return [
        s for s in signals_store.iter_active(shared, producer=probe.PRODUCER)
        if s.type == probe.SIGNAL_TYPE
    ]


def test_emit_fires_pod_wide_alert_signal(shared):
    red = probe.ProbeOutcome(
        "tcp", False, http_status=401, error="http:401",
        detail="HTTP 401 on /api/evo/dispatch — device-auth gate",
    )
    probe.emit_signals(shared, [red], pod="mini")
    firing = _firing(shared)
    assert len(firing) == 1
    sig = firing[0]
    assert sig.scope == "pod"
    assert sig.severity == "alert"
    assert sig.bot_id is None
    assert sig.signature == "evo_path_probe:evo_path_down:tcp"
    assert "evo keyword path down" in sig.title
    assert "HTTP 401" in sig.title and "TCP" in sig.title and "mini" in sig.title
    assert sig.details["transport"] == "tcp"
    assert sig.details["http_status"] == 401


def test_emit_one_signal_per_transport_not_per_bot(shared):
    tcp_red = probe.ProbeOutcome("tcp", False, http_status=401, error="http:401")
    sock_red = probe.ProbeOutcome(
        "unix-socket", False, error="connect:ENOENT", detail="sock missing"
    )
    probe.emit_signals(shared, [tcp_red, sock_red], pod="mini")
    firing = _firing(shared)
    assert len(firing) == 2
    sigs = {s.signature for s in firing}
    assert sigs == {
        "evo_path_probe:evo_path_down:tcp",
        "evo_path_probe:evo_path_down:unix-socket",
    }


def test_emit_coalesces_across_runs(shared):
    red = probe.ProbeOutcome("tcp", False, http_status=401, error="http:401")
    probe.emit_signals(shared, [red], pod="mini")
    probe.emit_signals(shared, [red], pod="mini")
    probe.emit_signals(shared, [red], pod="mini")
    firing = _firing(shared)
    assert len(firing) == 1, "a stable signature must NOT spam per-run"
    assert firing[0].observation_count == 3


def test_sweep_resolves_on_recovery(shared):
    red = probe.ProbeOutcome("tcp", False, http_status=401, error="http:401")
    probe.emit_signals(shared, [red], pod="mini")
    assert len(_firing(shared)) == 1

    green = probe.ProbeOutcome("tcp", True, http_status=200, envelope_ok=True, mode="speak")
    probe.emit_signals(shared, [green], pod="mini")
    assert _firing(shared) == []


def test_dry_run_writes_no_signal(shared):
    red = probe.ProbeOutcome("tcp", False, http_status=401, error="http:401")
    kept = probe.emit_signals(shared, [red], pod="mini", dry_run=True)
    assert kept == {"evo_path_probe:evo_path_down:tcp"}
    assert _firing(shared) == []


# ── Title formatting ──────────────────────────────────────────────────────────


def test_human_title_examples():
    o401 = probe.ProbeOutcome("tcp", False, http_status=401, error="http:401")
    assert (
        probe._human_title(o401, "mini")
        == "evo keyword path down — HTTP 401 on /api/evo/dispatch (mini, TCP)"
    )
    oenoent = probe.ProbeOutcome("unix-socket", False, error="connect:ENOENT")
    assert (
        probe._human_title(oenoent, "evolve-vps-pod")
        == "evo keyword path down — admin-daemon.sock missing (ENOENT) "
        "(evolve-vps-pod, unix socket)"
    )


# ── run_probe orchestration ───────────────────────────────────────────────────


def test_run_probe_green_both_transports(shared, short_socket_path):
    with CannedServer(status=200, body_obj=_GOOD_ENVELOPE) as tcp_srv, CannedServer(
        status=200, body_obj=_GOOD_ENVELOPE, family="unix",
        socket_path=short_socket_path,
    ):
        summary = probe.run_probe(
            shared, bot_id="evolve", host=tcp_srv.host, port=tcp_srv.port,
            socket_path=short_socket_path, timeout=2, pod="mini",
        )
    assert summary["ok"] is True
    assert summary["red_count"] == 0
    assert summary["transports"]["tcp"]["ok"] is True
    assert summary["transports"]["unix-socket"]["ok"] is True
    assert _firing(shared) == []


def test_run_probe_red_tcp_fires_and_summarizes(shared, short_socket_path):
    with CannedServer(status=401, body_obj={"error": "x"}) as tcp_srv, CannedServer(
        status=200, body_obj=_GOOD_ENVELOPE, family="unix",
        socket_path=short_socket_path,
    ):
        summary = probe.run_probe(
            shared, bot_id="evolve", host=tcp_srv.host, port=tcp_srv.port,
            socket_path=short_socket_path, timeout=2, pod="mini",
        )
    assert summary["ok"] is False
    assert summary["red_count"] == 1
    assert summary["signals_firing"] == ["evo_path_probe:evo_path_down:tcp"]
    firing = _firing(shared)
    assert len(firing) == 1
