"""board_listener.py — the board's tailnet-only second listener.

Design: ``internal/design-pa-mobile-board-2026-08-31.md`` D-MB2 ("reachable
on tailnet/LAN only, never an open port") and §5's F8 gate. Threat model and
findings: ``internal/review-board-web-surface-2026-09.md``.

The admin daemon's own listener binds ``127.0.0.1`` and must keep doing so —
it serves the whole admin plane. A phone cannot reach loopback, so the board
shipped in slices 1+2 has never been opened from the device it was designed
for. This module adds a **second bound socket**, the same shape as the
unix-socket binding in ``unix_socket_server`` (one Flask app, several
bindings), with three properties that are the whole point:

  1. **Tailnet address only.** The bind address comes from
     ``tailscale status --json``'s ``Self.TailscaleIPs``, or — when the CLI
     cannot answer the service user this daemon runs as — from the unique
     address in ``100.64.0.0/10`` on this host's own interfaces. Either way
     it is re-checked against ``100.64.0.0/10`` immediately before
     ``bind()``. Anything else — a LAN address, ``0.0.0.0``, ``::``, an
     operator-typed override — is refused. There is no configuration path to
     a wildcard bind, because the address is not configurable at all; the
     second source is a second way to learn the same fact, subject to the
     same test, and it refuses rather than guesses when it finds two.
  2. **Board paths only.** ``BoardOnlyMiddleware`` answers 404 to every path
     outside ``/board/`` and ``/api/board/`` *before* the Flask app sees it.
     The admin API is not merely unauthenticated-and-gated on this socket;
     it is not routed. A gate can be misconfigured; an unrouted path cannot.
  3. **Off unless asked.** No config key ⇒ no listener. The daemon logs one
     plain line saying why it did not start and carries on.

Traffic is plain HTTP. That is deliberate and safe *only* because of (1):
every byte crosses a WireGuard tunnel between two authenticated tailnet
nodes, so the transport is encrypted end to end without a certificate. It is
also why the board cookie is not marked ``Secure`` on this listener (F-6).

Config (``network.json``)::

    "board": {
      "tailnetListener": {
        "enabled": true,       // absent or false ⇒ no listener
        "port": 5050           // optional; defaults to the admin port
      }
    }
"""
from __future__ import annotations

import socketserver
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from ..telemetry import get_logger, scrub_secrets

_log = get_logger("web.board_listener")

#: The only path prefixes this listener will route. Kept in one place and
#: shared with server.py's auth/CSRF exemptions so the two can never drift
#: into disagreeing about what "the board surface" means.
BOARD_PATH_PREFIXES: tuple[str, ...] = ("/board/", "/api/board/")

_NOT_FOUND_BODY = b"Not found.\n"

def is_board_path(path: str) -> bool:
    """True for a path the board surface owns.

    The single definition of the board's path namespace: the tailnet
    listener routes exactly these, and ``server.py`` exempts exactly these
    from the admin device-cookie and CSRF gates.
    """
    return any(path.startswith(p) for p in BOARD_PATH_PREFIXES)


class BoardOnlyMiddleware:
    """WSGI wrapper that 404s every path outside the board namespace."""

    def __init__(self, app: Callable[..., Iterable[bytes]]) -> None:
        self._app = app

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        if not is_board_path(environ.get("PATH_INFO") or ""):
            start_response("404 Not Found", [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(_NOT_FOUND_BODY))),
                ("Cache-Control", "no-store"),
            ])
            return [_NOT_FOUND_BODY]
        # Marks the request as having arrived on the tailnet socket. Routes
        # do not branch on it today; it exists so a future policy ("this
        # action is loopback-only") has a fact to read rather than a guess.
        environ["EVOLVE_BOARD_LISTENER"] = "1"
        return self._app(environ, start_response)


class _BoardRequestHandler(WSGIRequestHandler):
    """Request handler whose access lines are scrubbed and logged, not printed.

    ``wsgiref``'s default ``log_message`` writes the request line — query
    string and all — to stderr, which on a LaunchDaemon pod is the error
    log. That is precisely the leak ``telemetry.scrub_secrets`` exists to
    stop (F-1), so this routes through the admin logger, scrubbed.
    """

    #: Seconds a connection may sit without sending its next byte before the
    #: worker thread gives up on it. ``wsgiref``'s default is ``None`` —
    #: block forever — and ``_ThreadingWSGIServer`` spends one thread per
    #: connection, so before this a tailnet peer that opened sockets and
    #: sent nothing could pin threads indefinitely inside the process that
    #: also serves the whole admin plane on loopback. Tailnet peers are
    #: authenticated WireGuard nodes, so this is an accepted risk rather
    #: than a hole (§4 of the review) — 30s is its mechanism, and it is
    #: generous for a phone on a slow link, which sends its request line
    #: immediately after connecting.
    timeout = 30

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        try:
            line = scrub_secrets(format % args)
        except Exception:
            # A malformed format/args pair must not fall through to the base
            # class, which would print the UNSCRUBBED request line to stderr
            # — the exact leak this override exists to stop.
            line = "<unformattable access line suppressed>"
        _log.info("board-listener %s", line)

    # log_error is deliberately NOT overridden: BaseHTTPRequestHandler's own
    # implementation delegates to log_message, so the scrub above already
    # covers the error path — including the 400 whose message embeds the raw
    # request line. An override here would be dead code that looks like a
    # safeguard. Pinned by test_a_malformed_request_line_does_not_leak_the_token.


class _ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    """One worker thread per request, so a slow phone cannot block the accept
    loop. Daemon threads: the process exits without waiting on them."""

    daemon_threads = True
    allow_reuse_address = True


def listener_config(network: dict[str, Any]) -> tuple[bool, int | None]:
    """``(enabled, port_override)`` from ``network.json``.

    Absent config is a valid, expected state and means disabled — a pod that
    has never heard of the board must not grow a listening socket.
    """
    block = network.get("board")
    block = block.get("tailnetListener") if isinstance(block, dict) else None
    if not isinstance(block, dict):
        return False, None
    port = block.get("port")
    return bool(block.get("enabled")), port if isinstance(port, int) else None


def resolve_listener_port(network: dict[str, Any], *, admin_port: int) -> int:
    """The port the board listener binds — the single definition of it.

    ``start_board_listener`` binds this, and ``board_cli`` prints it into the
    shown-once phone link; they must not be two expressions of the same
    quantity, because the failure is silent (a link the phone cannot reach,
    printed once and unrecoverable).

    ``admin_port`` is the admin daemon's own TCP port: inside the daemon that
    is ``serve``'s live ``--port``, and from a separate process it is
    ``config.resolve_admin_port()``. The board reuses it on the tailnet
    address — same port, different interface, so the operator types one
    familiar URL with a different host — unless ``board.tailnetListener.port``
    overrides it.
    """
    _, port_override = listener_config(network)
    return port_override or admin_port


#: The two ways this node's tailnet address can be learned, named in the
#: one log line that says which one was used. Not a configuration choice —
#: the second is only ever tried when the first cannot answer.
CLI_SOURCE = "tailscale-cli"
INTERFACE_SOURCE = "interface"


class BindAddress(NamedTuple):
    """A resolved tailnet address plus which path produced it.

    The source rides along with the address because the log line that says
    the listener started must also say how it learned where to start — the
    two resolutions differ in what they prove (the CLI has spoken to the
    Tailscale daemon; the interface path has only seen an address the kernel
    holds), and an operator debugging "which pod is this even on" should not
    have to infer it.
    """

    address: str
    source: str


class BindAddressUnresolved(Exception):
    """Neither resolution path produced a tailnet address.

    Carries BOTH failures, because either one alone is misleading: "the
    Tailscale CLI is signed out" is the wrong story on the reference pod
    (the node is up; the service user cannot ask the app), and "no tailnet
    address on any interface" alone hides that the CLI was tried first.
    """

    def __init__(self, cli_error: BaseException, interface_error: BaseException) -> None:
        self.cli_error = cli_error
        self.interface_error = interface_error
        super().__init__(
            f"{CLI_SOURCE}: {type(cli_error).__name__}: {cli_error}; "
            f"{INTERFACE_SOURCE}: {type(interface_error).__name__}: {interface_error}"
        )


def resolve_bind_address() -> BindAddress:
    """This node's tailnet IPv4 and how it was learned, or raise.

    Two paths, tried in that order, both landing on the SAME acceptance
    test (``https_setup.is_tailnet_ipv4``) and both re-checked again by
    ``start_board_listener`` immediately before ``bind()``. This is a
    second way to learn one fact, not a second kind of address that may be
    bound — the bind rules are exactly what they were.

    1. ``tailscale status --json`` via ``https_setup`` — the same code path
       the admin UI's HTTPS wizard uses, so the CLI stays the one definition
       of "our tailnet identity" and keeps supplying the hostname the wizard
       needs. First, so a working CLI always wins.
    2. This host's own network interfaces. The daemon runs as the ``evolve``
       service user, and the Mac App Store Tailscale CLI answers only the
       GUI session's user — on the reference pod it exits 0 with non-JSON,
       which is why the listener never started there (measured 2026-09-02).
       The address is on a ``utun`` either way, and enumerating interfaces
       needs no privilege.

    Raises :class:`BindAddressUnresolved`, naming both reasons, when
    neither path can answer.
    """
    from .. import https_setup
    try:
        return BindAddress(
            https_setup._resolve_tailnet_ipv4(https_setup._check_signed_in()),
            CLI_SOURCE,
        )
    except Exception as exc:  # noqa: BLE001
        cli_error = exc

    try:
        return BindAddress(
            https_setup._resolve_tailnet_ipv4_from_interfaces(), INTERFACE_SOURCE
        )
    except Exception as exc:  # noqa: BLE001
        raise BindAddressUnresolved(cli_error, exc) from exc


def start_board_listener(
    app: Any,
    network_path: Path,
    *,
    default_port: int,
) -> tuple[threading.Thread, _ThreadingWSGIServer] | None:
    """Bind the board-only listener on the tailnet address, or explain why not.

    Returns ``(thread, server)`` on success and ``None`` otherwise, having
    logged exactly one plain, operator-legible line for the reason. Never
    raises: a pod whose Tailscale is signed out must still serve its admin
    UI on loopback.
    """
    from ..config import load_network
    try:
        network = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        _log.warning("board listener not started: cannot read %s (%s)",
                     network_path, exc)
        return None

    enabled, _ = listener_config(network)
    if not enabled:
        _log.info("board listener not started: board.tailnetListener.enabled "
                  "is not set in %s", network_path)
        return None

    try:
        resolved = resolve_bind_address()
    except Exception as exc:  # noqa: BLE001
        _log.warning("board listener not started: no tailnet address resolved "
                     "(%s: %s)", type(exc).__name__, exc)
        return None
    address = resolved.address

    # Re-check at the bind site, not only at the resolve site. The check that
    # matters is the one adjacent to the syscall.
    from ..https_setup import is_tailnet_ipv4
    if not is_tailnet_ipv4(address):
        _log.error("board listener not started: refusing to bind %r — not a "
                   "tailnet address", address)
        return None

    port = resolve_listener_port(network, admin_port=default_port)
    try:
        server = _ThreadingWSGIServer((address, port), _BoardRequestHandler)
        server.set_app(BoardOnlyMiddleware(app))
    except OSError as exc:
        _log.warning("board listener not started: cannot bind %s:%s (%s)",
                     address, port, exc)
        return None

    thread = threading.Thread(
        target=server.serve_forever,
        name="board-tailnet-listener",
        daemon=True,
    )
    thread.start()
    # One line, and it names the source: "which address, and how did it
    # decide that" are the same question when the two resolvers can disagree
    # about whether this node is on a tailnet at all.
    _log.info("board listener bound %s:%s (source: %s) — "
              "http://%s:%s/board/<bot_id>, board paths only",
              address, port, resolved.source, address, port)
    return thread, server
