"""Tests for the board's tailnet-only second listener (board_listener.py).

WHAT THESE PIN — the three properties the F8 review's binding decision rests
on, and which the design doc's §5 gate is about:

  * **Board paths only.** The listener routes ``/board/*`` and
    ``/api/board/*`` and 404s everything else — the admin API is not merely
    gated on this socket, it is not routed at all.
  * **Tailnet address only.** The bind address is resolved from Tailscale's
    own status and re-checked against 100.64.0.0/10 at the bind site. A LAN
    address, a wildcard, or a Tailscale that returns nonsense all mean "do
    not start" — never "bind something else".
  * **Off unless asked**, and silent-but-explained when it cannot start: no
    config ⇒ no socket, and exactly one plain log line saying why.
  * **One definition of the port**, shared with the operator-facing link,
    and a bounded read timeout for the new off-box exposure (§4's accepted
    risk and its mechanism).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import https_setup, telemetry  # noqa: E402
from evolve_admin.web import board_listener as bl  # noqa: E402


def _wsgi_probe(app, path: str):
    """Drive a WSGI app directly and return ``(status, body)``."""
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    environ = {
        "REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": "",
        "SERVER_NAME": "test", "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.url_scheme": "http",
        "wsgi.input": None, "wsgi.errors": sys.stderr,
    }
    body = b"".join(app(environ, start_response))
    return captured["status"], body


@pytest.fixture()
def inner_app():
    app = Flask(__name__)

    @app.get("/board/<bot_id>")
    def board(bot_id):
        return f"board:{bot_id}"

    @app.get("/api/board/<bot_id>")
    def api_board(bot_id):
        return f"api:{bot_id}"

    @app.get("/api/status")
    def status():
        return "the whole admin plane"

    @app.get("/")
    def home():
        return "admin spa"

    return app


# ── the path namespace ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/board/personal-bot",
    "/board/personal-bot/manifest.webmanifest",
    "/board/icon-192.png",
    "/api/board/personal-bot",
    "/api/board/personal-bot/cards",
])
def test_board_paths_are_routed(path):
    assert bl.is_board_path(path) is True


@pytest.mark.parametrize("path", [
    "/", "/api/status", "/api/network", "/pair", "/api/pair",
    "/static/icons/icon-192.png", "/manifest.json", "/api/deploy",
    "/board", "/boardX/personal-bot", "/api/boards/x",
])
def test_everything_else_is_not(path):
    assert bl.is_board_path(path) is False


def test_listener_serves_board_and_404s_the_admin_plane(inner_app):
    wrapped = bl.BoardOnlyMiddleware(inner_app.wsgi_app)
    status, body = _wsgi_probe(wrapped, "/board/personal-bot")
    assert status.startswith("200")
    assert body == b"board:personal-bot"

    status, body = _wsgi_probe(wrapped, "/api/board/personal-bot")
    assert status.startswith("200")

    for blocked in ("/api/status", "/", "/static/icons/icon-192.png"):
        status, body = _wsgi_probe(wrapped, blocked)
        assert status.startswith("404"), blocked
        assert b"admin" not in body


def test_listener_marks_the_transport(inner_app):
    seen = {}

    def spy(environ, start_response):
        seen.update(environ)
        return inner_app.wsgi_app(environ, start_response)

    _wsgi_probe(bl.BoardOnlyMiddleware(spy), "/board/personal-bot")
    assert seen.get("EVOLVE_BOARD_LISTENER") == "1"


# ── the bind address ───────────────────────────────────────────────────────

@pytest.mark.parametrize("addr", ["100.64.0.1", "100.101.102.103", "100.127.255.254"])
def test_tailnet_addresses_are_accepted(addr):
    assert https_setup.is_tailnet_ipv4(addr) is True


@pytest.mark.parametrize("addr", [
    "0.0.0.0", "::", "127.0.0.1", "192.168.1.20", "10.0.0.5",
    "100.63.255.255", "100.128.0.0", "fd7a:115c:a1e0::1", "", "not-an-ip",
])
def test_non_tailnet_addresses_are_refused(addr):
    assert https_setup.is_tailnet_ipv4(addr) is False


def test_resolver_picks_the_tailnet_ipv4_from_status():
    status = {"Self": {"TailscaleIPs": ["fd7a:115c:a1e0::1", "100.101.102.103"],
                       "DNSName": "pod.tailnet.ts.net."}}
    assert https_setup._resolve_tailnet_ipv4(status) == "100.101.102.103"


def test_resolver_raises_when_no_tailnet_ipv4_is_present():
    with pytest.raises(https_setup.TailscaleNotSignedIn):
        https_setup._resolve_tailnet_ipv4({"Self": {"TailscaleIPs": ["192.168.1.9"]}})


# ── config gating and the not-started paths ────────────────────────────────

@pytest.mark.parametrize("network,expected", [
    ({}, (False, None)),
    ({"board": {}}, (False, None)),
    ({"board": {"tailnetListener": {}}}, (False, None)),
    ({"board": {"tailnetListener": {"enabled": False}}}, (False, None)),
    ({"board": {"tailnetListener": {"enabled": True}}}, (True, None)),
    ({"board": {"tailnetListener": {"enabled": True, "port": 5061}}}, (True, 5061)),
    ({"board": {"tailnetListener": {"enabled": True, "port": "5061"}}}, (True, None)),
    ({"board": "nonsense"}, (False, None)),
])
def test_listener_config_reads(network, expected):
    assert bl.listener_config(network) == expected


def test_the_handler_bounds_how_long_a_silent_peer_holds_a_thread():
    """§4's accepted risk needs a mechanism, not a note.

    ``_ThreadingWSGIServer`` spends one thread per connection and
    ``wsgiref``'s handler default is ``timeout = None`` — block forever. So
    before this, a tailnet peer that connected and sent nothing pinned a
    thread indefinitely inside the process that also serves the whole admin
    plane on loopback. This is the number that bounds it.
    """
    assert bl._BoardRequestHandler.timeout == 30


@pytest.mark.parametrize("network,admin_port,expected", [
    # No override: the board rides the admin daemon's own port.
    ({"board": {"tailnetListener": {"enabled": True}}}, 5050, 5050),
    ({"board": {"tailnetListener": {"enabled": True}}}, 8080, 8080),
    # An explicit board port wins over it.
    ({"board": {"tailnetListener": {"enabled": True, "port": 5061}}}, 8080, 5061),
    # A non-int port is ignored by listener_config, so the admin port stands.
    ({"board": {"tailnetListener": {"enabled": True, "port": "5061"}}}, 8080, 8080),
])
def test_resolve_listener_port(network, admin_port, expected):
    assert bl.resolve_listener_port(network, admin_port=admin_port) == expected


@pytest.fixture()
def network_file(tmp_path):
    def _write(payload):
        p = tmp_path / "network.json"
        p.write_text(json.dumps(payload))
        return p
    return _write


def test_not_started_when_unconfigured(inner_app, network_file, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        assert bl.start_board_listener(
            inner_app, network_file({"sharedDir": "/tmp/x"}), default_port=5050
        ) is None
    assert any("board.tailnetListener.enabled" in r.getMessage()
               for r in caplog.records)


def test_not_started_when_tailscale_is_unavailable(
    inner_app, network_file, monkeypatch, caplog
):
    import logging
    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: (_ for _ in ()).throw(https_setup.TailscaleNotSignedIn("signed out")),
    )
    net = network_file({"board": {"tailnetListener": {"enabled": True}}})
    with caplog.at_level(logging.INFO):
        assert bl.start_board_listener(inner_app, net, default_port=5050) is None
    assert any("no tailnet address resolved" in r.getMessage()
               for r in caplog.records)


def test_refuses_to_bind_a_non_tailnet_address(
    inner_app, network_file, monkeypatch, caplog
):
    """The keystone: even if resolution somehow yields a wildcard, the check
    adjacent to the bind refuses it. There is no path to 0.0.0.0."""
    import logging
    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: bl.BindAddress("0.0.0.0", bl.CLI_SOURCE),
    )
    net = network_file({"board": {"tailnetListener": {"enabled": True}}})
    with caplog.at_level(logging.INFO):
        assert bl.start_board_listener(inner_app, net, default_port=5050) is None
    assert any("not a tailnet address" in r.getMessage() for r in caplog.records)


def test_bind_failure_is_survivable(inner_app, network_file, monkeypatch, caplog):
    """A tailnet address the host does not hold (node key lapsed, address
    reassigned) must not take the admin daemon down with it."""
    import logging
    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: bl.BindAddress("100.64.0.1", bl.CLI_SOURCE),
    )
    net = network_file({"board": {"tailnetListener": {"enabled": True}}})
    with caplog.at_level(logging.INFO):
        result = bl.start_board_listener(inner_app, net, default_port=5050)
    if result is None:
        assert any("cannot bind" in r.getMessage() for r in caplog.records)
    else:  # pragma: no cover — only if the host really holds 100.64.0.1
        thread, server = result
        server.shutdown()
        server.server_close()


def test_started_listener_serves_only_board_paths(
    inner_app, network_file, monkeypatch
):
    """End to end on a real socket: bind loopback (standing in for the
    tailnet address), then prove the admin plane is unreachable on it."""
    import urllib.error
    import urllib.request

    import socket

    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: bl.BindAddress("127.0.0.1", bl.CLI_SOURCE),
    )
    # board_listener imports is_tailnet_ipv4 from https_setup at call time,
    # so this is the binding the bind-site check actually reads.
    monkeypatch.setattr(https_setup, "is_tailnet_ipv4", lambda addr: True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    net = network_file(
        {"board": {"tailnetListener": {"enabled": True, "port": free_port}}})

    result = bl.start_board_listener(inner_app, net, default_port=free_port)
    assert result is not None
    thread, server = result
    try:
        base = f"http://127.0.0.1:{free_port}"
        with urllib.request.urlopen(f"{base}/board/personal-bot", timeout=5) as r:
            assert r.read() == b"board:personal-bot"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/status", timeout=5)
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


# ── F-1 on this listener's own error path ──────────────────────────────────

def test_a_malformed_request_line_does_not_leak_the_token(
    inner_app, network_file, monkeypatch, caplog
):
    """``BaseHTTPRequestHandler.parse_request`` answers a malformed request
    with ``send_error(400, "Bad request syntax (%r)" % requestline)`` — the
    request line, **query string included**, goes to ``log_error``, whose
    stock implementation prints it to stderr. On a LaunchDaemon pod that is
    the error log. So the handler's error path is a second, quieter copy of
    F-1, and this drives it: a deliberately broken request carrying a board
    token must be logged scrubbed, or not at all.

    It reaches the scrub through the BASE ``log_error``, which delegates to
    the overridden ``log_message`` — which is why ``_BoardRequestHandler``
    does not override ``log_error`` itself. An override there would be dead
    code shaped like a safeguard.

    MUTATION CHECKED: dropping the ``scrub_secrets`` call from
    ``_BoardRequestHandler.log_message`` makes this go red. The request line
    below is deliberately ONE word — that is the only ``parse_request``
    branch which echoes the whole request line (the others print just the
    method or the version), so a well-formed-looking bad request would
    exercise nothing."""
    import logging
    import socket

    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: bl.BindAddress("127.0.0.1", bl.CLI_SOURCE),
    )
    monkeypatch.setattr(https_setup, "is_tailnet_ipv4", lambda addr: True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    net = network_file({"board": {"tailnetListener": {"enabled": True, "port": port}}})

    result = bl.start_board_listener(inner_app, net, default_port=port)
    assert result is not None
    thread, server = result
    try:
        with caplog.at_level(logging.INFO):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                # One word: parse_request answers 400 "Bad request syntax
                # (%r)" with the request line itself — token and all.
                s.sendall(b"/board/personal-bot?t=SEKRITTOKEN\r\n\r\n")
                s.settimeout(5)
                try:
                    s.recv(4096)
                except OSError:
                    pass
            logged = "\n".join(r.getMessage() for r in caplog.records)
    finally:
        server.shutdown()
        server.server_close()

    assert "SEKRITTOKEN" not in logged
    # And it is not merely silent: the rejection IS recorded, redacted — so
    # this cannot pass by the error path simply never being reached.
    assert "Bad request syntax" in logged
    assert telemetry.REDACTED in logged


# ── Resolving the address without the Tailscale CLI ────────────────────────
#
# WHY: on the reference pod the admin daemon runs as the ``evolve`` service
# user, and the Mac App Store Tailscale CLI answers only the GUI session's
# user — it exits 0 with non-JSON, so ``_check_signed_in`` raised and the
# listener refused to bind (measured 2026-09-02). The refusal was correct
# and stays correct; what these pin is the SECOND way to learn the same
# fact, and that it is subject to exactly the same acceptance test.

_IFCONFIG_ONE_TAILNET = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 3c:22:fb:00:11:22
\tinet6 fe80::1cb%en0 prefixlen 64 secured scopeid 0xc
\tinet 192.168.1.24 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.74.228.85 --> 100.74.228.85 netmask 0xff000000
"""

_IFCONFIG_NO_TAILNET = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.24 netmask 0xffffff00 broadcast 192.168.1.255
"""

_IFCONFIG_TWO_TAILNETS = _IFCONFIG_ONE_TAILNET + """\
utun6: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.115.92.7 --> 100.115.92.7 netmask 0xff000000
"""


def _fake_ifconfig(monkeypatch, text: str) -> None:
    """Point the interface resolver at fixture ``ifconfig`` output.

    Patches the binary probe as well as the run, so the test is the same on
    a host with ``ip`` installed (Linux CI) as on one without (macOS).
    """
    import subprocess

    monkeypatch.setattr(
        https_setup, "_first_executable",
        lambda candidates: ("/sbin/ifconfig"
                            if candidates is https_setup._IFCONFIG_FALLBACK_PATHS
                            else None),
    )
    real_run = subprocess.run

    def fake_run(argv, *a, **k):
        # Dispatch on the argv rather than swallowing every subprocess call:
        # ``subprocess.run`` is a module global, and a blanket replacement
        # would silently answer anything else the process happened to run.
        if argv and argv[0] == "/sbin/ifconfig":
            return subprocess.CompletedProcess(argv, 0, text, "")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(https_setup.subprocess, "run", fake_run)


def _cli_is_deaf(monkeypatch) -> None:
    """The reference pod's exact failure: status exits 0 with non-JSON."""
    monkeypatch.setattr(
        https_setup, "_check_signed_in",
        lambda: (_ for _ in ()).throw(https_setup.TailscaleNotSignedIn(
            "tailscale status returned non-JSON: Expecting value: "
            "line 1 column 1 (char 0)")),
    )


def test_ifconfig_parse_takes_only_inet_lines_under_their_interface():
    """Strict by construction: ``inet6``, ``ether``, flags and status lines
    match nothing, and each address is attributed to the block it sits in."""
    assert https_setup._parse_ifconfig_ipv4(_IFCONFIG_ONE_TAILNET) == [
        ("lo0", "127.0.0.1"),
        ("en0", "192.168.1.24"),
        ("utun4", "100.74.228.85"),
    ]


def test_ip_json_parse_reads_inet_addresses():
    """The Linux path: ``ip -j addr`` is JSON, so it needs no text parsing —
    and a non-``inet`` family is dropped rather than coerced."""
    payload = json.dumps([
        {"ifname": "eth0", "addr_info": [
            {"family": "inet", "local": "10.0.0.5"},
            {"family": "inet6", "local": "fe80::1"},
        ]},
        {"ifname": "tailscale0", "addr_info": [
            {"family": "inet", "local": "100.74.228.85"},
        ]},
    ])
    assert https_setup._parse_ip_json_ipv4(payload) == [
        ("eth0", "10.0.0.5"),
        ("tailscale0", "100.74.228.85"),
    ]


def test_deaf_cli_falls_back_to_the_single_interface_address(monkeypatch):
    """The chip's whole point: the CLI cannot answer this user, the address
    is on a utun anyway, and the listener learns it without any privilege."""
    _cli_is_deaf(monkeypatch)
    _fake_ifconfig(monkeypatch, _IFCONFIG_ONE_TAILNET)

    resolved = bl.resolve_bind_address()
    assert resolved == bl.BindAddress("100.74.228.85", bl.INTERFACE_SOURCE)


def test_a_working_cli_still_wins(monkeypatch):
    """The CLI stays first — it has actually spoken to the Tailscale daemon,
    and it is what keeps the hostname available to the HTTPS wizard. The
    interface fixture below carries a DIFFERENT address, so a test that
    passed by accidentally taking the fallback would be red."""
    monkeypatch.setattr(
        https_setup, "_check_signed_in",
        lambda: {"BackendState": "Running",
                 "Self": {"TailscaleIPs": ["100.101.102.103"]}},
    )
    _fake_ifconfig(monkeypatch, _IFCONFIG_ONE_TAILNET)

    resolved = bl.resolve_bind_address()
    assert resolved == bl.BindAddress("100.101.102.103", bl.CLI_SOURCE)


def test_no_tailnet_address_anywhere_refuses_and_names_both_paths(monkeypatch):
    """Zero matches ⇒ refuse exactly as before. The message carries BOTH
    reasons, because either alone tells the operator the wrong story."""
    _cli_is_deaf(monkeypatch)
    _fake_ifconfig(monkeypatch, _IFCONFIG_NO_TAILNET)

    with pytest.raises(bl.BindAddressUnresolved) as exc:
        bl.resolve_bind_address()
    message = str(exc.value)
    assert "non-JSON" in message                      # the CLI's reason
    assert https_setup.TAILNET_V4_NETWORK in message   # the interface reason
    assert bl.CLI_SOURCE in message and bl.INTERFACE_SOURCE in message


def test_two_tailnet_addresses_refuse_and_list_both(monkeypatch):
    """Never guess between tailnets. Two candidates is a refusal that names
    them, not a coin flip the operator finds out about on their phone."""
    _cli_is_deaf(monkeypatch)
    _fake_ifconfig(monkeypatch, _IFCONFIG_TWO_TAILNETS)

    with pytest.raises(bl.BindAddressUnresolved) as exc:
        bl.resolve_bind_address()
    message = str(exc.value)
    assert "utun4=100.74.228.85" in message
    assert "utun6=100.115.92.7" in message
    assert "refusing to guess" in message


def test_interface_resolution_binds_nothing_it_would_not_have_bound(monkeypatch):
    """The fallback cannot widen the bind rules: it applies the SAME
    ``is_tailnet_ipv4`` test, so a LAN-only host yields nothing at all.

    MUTATION CHECKED: dropping the ``is_tailnet_ipv4`` filter in
    ``_resolve_tailnet_ipv4_from_interfaces`` makes this go red — it would
    return 127.0.0.1 (or refuse for having three candidates) instead."""
    _fake_ifconfig(monkeypatch, _IFCONFIG_NO_TAILNET)
    with pytest.raises(https_setup.TailnetInterfaceUnavailable):
        https_setup._resolve_tailnet_ipv4_from_interfaces()


def test_a_started_listener_logs_which_source_it_used(
    inner_app, network_file, monkeypatch, caplog
):
    """§4's guarantees are about WHERE it bound; this is about the operator
    being able to see it. One line, naming address, port and source."""
    import logging
    import socket

    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: bl.BindAddress("127.0.0.1", bl.INTERFACE_SOURCE),
    )
    monkeypatch.setattr(https_setup, "is_tailnet_ipv4", lambda addr: True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    net = network_file({"board": {"tailnetListener": {"enabled": True, "port": port}}})

    with caplog.at_level(logging.INFO):
        result = bl.start_board_listener(inner_app, net, default_port=port)
    assert result is not None
    thread, server = result
    try:
        logged = "\n".join(r.getMessage() for r in caplog.records)
    finally:
        server.shutdown()
        server.server_close()
    assert f"board listener bound 127.0.0.1:{port} (source: interface)" in logged


def test_the_refusal_says_it_could_not_look_when_it_could_not(monkeypatch):
    """"No tailnet address on any interface" and "could not enumerate any
    interface" are different facts, and the refusal line is the only place
    either is ever seen. Losing the second reads as "definitely not on a
    tailnet" when the truth is "did not find out"."""
    monkeypatch.setattr(https_setup, "_first_executable", lambda candidates: None)
    with pytest.raises(https_setup.TailnetInterfaceUnavailable) as exc:
        https_setup._resolve_tailnet_ipv4_from_interfaces()
    assert "neither `ip` nor `ifconfig` found" in str(exc.value)
