"""evolve_admin.web.unix_socket_server — second WSGI binding for evo↔admin.

Spec: internal/spec-evo-account-separation-2026-05-25.md §3, Phase E.3.

The admin daemon's existing Flask app binds to TCP loopback for the admin
UI. This module adds a **second binding** over a unix socket at
``{shared_dir}/admin-daemon.sock`` (``/Users/Shared/evolve/admin-daemon.sock``
on macOS, ``/var/lib/evolve/admin-daemon.sock`` on Linux — the path is
platform-keyed via ``evolve_config.CANONICAL_SHARED_DIR`` and overridable
per-pod via ``network.json::sharedDir``). The same Flask app serves
both — request routing is identical — but the unix socket carries peer
credentials (via ``getpeereid`` on macOS, ``SO_PEERCRED`` on Linux —
glibc lacks ``getpeereid``) into ``request.environ`` so the admin daemon
can authenticate the calling process by its kernel-reported uid.

Used by Phase E to let evo's tool runtime (running as the future ``evo``
macOS user) call admin daemon endpoints. Routes that the admin UI
shouldn't expose (e.g. cross-bot reads triggered by evo's MCP tools)
gate on the peer uid via the ``@require_trusted_peer`` decorator
in ``peer_auth``.

The socket file is mode ``0660`` so its access scope is enforced at two
layers:
  1. Filesystem perms — only the owning user + group can connect at all
  2. Peer-cred extraction — the route handler inspects the connecting
     process's effective uid against an explicit trusted-user list

Either layer alone is sufficient defense; combining them is belt-and-
suspenders. We use the explicit decorator so the trust list lives in
code (reviewable, testable) rather than spread across filesystem state.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from evolve_config import CANONICAL_SHARED_DIR
from platform_profile import get_profile

log = logging.getLogger(__name__)


# Default socket location, derived from the platform-keyed shared dir
# (``/Users/Shared/evolve`` on macOS, ``/var/lib/evolve`` on Linux). The
# shared dir is operator-evolve-owned and always exists, so binding the
# socket there is safe and makes it discoverable to clients without
# further configuration. A literal macOS path here silently failed to
# bind on Linux pods (parent dir absent → FileNotFoundError), leaving
# the socket uncreated and every gateway plugin call (e.g. the Evolve
# directory digest) hitting connect ENOENT. The serve path passes an
# explicit ``socket_path`` resolved from ``network.json::sharedDir``;
# this default is the fallback when no path is supplied.
DEFAULT_SOCKET_PATH = Path(CANONICAL_SHARED_DIR) / "admin-daemon.sock"

# Default socket mode. 0660 means owner + group can connect; world cannot.
# Owner is whoever the admin daemon runs as (``evolve``). The group is the
# shared bot group (see DEFAULT_SOCKET_GROUP) so a bot in that group can
# connect. The explicit peer-uid check in ``peer_auth`` narrows the actual
# trust set; on Linux a named group ACE (evo_socket_acl) is the real reach
# mechanism because the chgrp below can't move group ownership there.
DEFAULT_SOCKET_MODE = 0o660

# Default group ownership for the socket — the platform-keyed shared bot group
# (``platform_profile.bot_shared_group``): macOS ``staff`` (every account's
# primary group, gid 20), Linux ``evolve-bots`` (the secondary group all bots
# join). Setting group ownership explicitly matters because the daemon's
# effective group isn't the bot group — on macOS launchd pods it is ``wheel``
# (the bind without chgrp leaves ``evolve:wheel`` 0660, locking out bots in
# ``staff``). macOS: this chgrp IS the reach mechanism (evolve can chgrp to a
# group it belongs to). Linux: ``evolve`` is NOT a member of ``evolve-bots``,
# so the chgrp EPERMs (fail-soft) and the named group ACE in evo_socket_acl —
# applied owner-direct at bind — is what actually grants bot connect access.
DEFAULT_SOCKET_GROUP = get_profile().bot_shared_group


def resolve_admin_socket_path(network_path: "str | Path | None" = None) -> Path:
    """Resolve the admin-daemon socket path from ``network.json::sharedDir``.

    The gateway plugin connects to ``{sharedDir}/admin-daemon.sock``, so the
    server must bind the same path. Honors an operator override of the shared
    dir while keeping the cross-platform default correct — a hardcoded macOS
    path never bound on Linux (parent dir absent → FileNotFoundError) and left
    every plugin call hitting connect ENOENT. Falls back to the platform-keyed
    :data:`DEFAULT_SOCKET_PATH` when no network path is given, the file lacks
    ``sharedDir``, or it can't be read.
    """
    if network_path is None:
        return DEFAULT_SOCKET_PATH
    try:
        from ..config import load_network
        shared = load_network(Path(network_path)).get("sharedDir")
    except Exception as exc:  # noqa: BLE001 — any read failure → platform default
        log.warning(
            "unix_socket_server: could not resolve sharedDir from %s (%s); "
            "using default %s", network_path, exc, DEFAULT_SOCKET_PATH,
        )
        return DEFAULT_SOCKET_PATH
    return Path(shared) / "admin-daemon.sock" if shared else DEFAULT_SOCKET_PATH


def ensure_bot_socket_acl(socket_path: "str | Path") -> bool:
    """Grant the shared bot group a write(connect) ACE on the admin-daemon
    socket. Called at every socket (re)bind from
    ``UnixSocketWSGIServer.server_bind`` — the primary enforcer of the grant.

    The Linux bot gateways run as users in the shared ``evolve-bots`` group but
    NOT in the socket-owning ``evolve`` group, so mode-0660 leaves them on the
    ``other`` class and a unix-socket ``connect()`` (a WRITE on the inode) hits
    EACCES — the pod-wide cause of the failing per-turn directory digest /
    RosterTools and the ``gateway_slow`` band. An owner-direct GROUP ACE fixes
    every bot at once (evo included) without widening ``other``; the in-process
    ``@require_trusted_peer`` uid-allowlist stays the second gate.

    The grant + platform gate live in ``evo_socket_acl._ensure_bot_socket_acl``
    (Linux only; macOS reaches the socket by its ``staff`` group ownership and
    is a no-op there). Imported lazily so this WSGI module carries no
    import-time dependency on the deploy layer.

    Best-effort and fail-soft: any error (import unavailable, setfacl failure)
    returns ``False`` and is logged — the socket still serves; only the bot
    connect channel degrades, exactly as before this grant existed. A ``False``
    on macOS is the expected group-ownership path, not an error; real Linux
    drift is surfaced operator-visibly by the ensure_pod_perms self-heal check.
    """
    try:
        from ..evo_socket_acl import _ensure_bot_socket_acl
    except Exception as exc:  # noqa: BLE001 — module not importable here
        log.debug("unix_socket_server: bot-socket-ACL import failed: %s", exc)
        return False
    try:
        ok = _ensure_bot_socket_acl(Path(socket_path))
        if not ok:
            log.debug(
                "unix_socket_server: bot-group connect ACL on %s not applied "
                "(macOS group-ownership path, or setfacl failed)", socket_path,
            )
        return ok
    except Exception as exc:  # noqa: BLE001 — never let an ACL hiccup break bind
        log.warning(
            "unix_socket_server: bot-socket-ACL grant on %s raised: %s",
            socket_path, exc,
        )
        return False


# ── Peer-credential extraction (kernel-sourced, never client-supplied) ────────

# ``struct ucred { pid_t pid; uid_t uid; gid_t gid; }`` — three 32-bit fields.
# ``pid_t`` is signed; ``uid_t``/``gid_t`` are UNSIGNED — so decode as ``i`` +
# ``I`` + ``I`` (NOT ``3i``) to match the macOS ``c_uint32`` path and keep a uid
# ≥ 2**31 (e.g. a systemd DynamicUser) from decoding negative. Returned by
# ``getsockopt(SOL_SOCKET, SO_PEERCRED)`` on Linux; the uid is the SECOND field.
# The kernel records these at ``connect()`` time, so a client cannot forge them.
_UCRED_FMT = "iII"

# ``SO_PEERCRED`` is a Linux-only socket option, so the constant is absent on a
# non-Linux Python build (e.g. a macOS dev box running the Linux-branch unit
# test against a mocked socket). Fall back to its canonical Linux value so the
# symbol always resolves; the fallback is never exercised on a real pod — a real
# macOS pod takes the ``getpeereid`` branch, and a real Linux pod always defines
# the constant.
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)


_libc: ctypes.CDLL | None = None
try:
    _libname = ctypes.util.find_library("c")
    if _libname:
        _libc = ctypes.CDLL(_libname)
except OSError:  # pragma: no cover — should be unreachable on macOS/Linux
    _libc = None


def _peer_eid_getpeereid(sock: socket.socket) -> tuple[int | None, int | None]:
    """macOS/BSD peer-cred extraction via ``getpeereid(3)`` from libc.

    Fills in the EUID/EGID of the process on the OTHER end of the connection.
    glibc does NOT ship ``getpeereid`` (it is a BSD function), so on Linux the
    ``_libc.getpeereid`` attribute lookup raises ``AttributeError`` and this
    degrades to ``(None, None)`` — which is why Linux must use the SO_PEERCRED
    branch instead. Returns ``(None, None)`` on any failure (libc not loadable,
    syscall error) so the caller degrades gracefully rather than crashing.
    """
    if _libc is None:
        log.warning("unix_socket_server: libc not available for getpeereid")
        return None, None
    uid = ctypes.c_uint32()
    gid = ctypes.c_uint32()
    try:
        rc = _libc.getpeereid(
            ctypes.c_int(sock.fileno()),
            ctypes.byref(uid),
            ctypes.byref(gid),
        )
    except (AttributeError, OSError) as exc:
        log.warning("unix_socket_server: getpeereid failed: %s", exc)
        return None, None
    if rc != 0:
        return None, None
    return uid.value, gid.value


def _peer_eid_so_peercred(sock: socket.socket) -> tuple[int | None, int | None]:
    """Linux peer-cred extraction via ``getsockopt(SOL_SOCKET, SO_PEERCRED)``.

    The kernel populates a ``struct ucred {pid, uid, gid}`` with the credentials
    the connecting process held at ``connect()`` time — kernel-sourced and
    unspoofable by the client, the same trust property ``getpeereid`` gives on
    macOS. This is the only working Linux mechanism, since glibc lacks
    ``getpeereid``. Returns ``(None, None)`` on any failure (unsupported option,
    short read) so the caller degrades to an unknown peer rather than crashing.
    """
    try:
        raw = sock.getsockopt(
            socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize(_UCRED_FMT)
        )
        _pid, uid, gid = struct.unpack(_UCRED_FMT, raw)
    except (OSError, struct.error) as exc:
        log.warning("unix_socket_server: SO_PEERCRED failed: %s", exc)
        return None, None
    return uid, gid


def _is_linux() -> bool:
    """True on a real Linux host. Keyed off ``sys.platform`` (the ACTUAL OS),
    deliberately NOT ``platform_profile.get_profile()``.

    The peer-cred MECHANISM is a hard libc/kernel-capability question — glibc
    does not export ``getpeereid``, and ``SO_PEERCRED`` is the real Linux
    syscall — so it must follow the running OS, not the pinnable platform
    profile. The admin test suite pins the MACOS profile for path-shape
    assertions (tests/conftest.py autouse), even on Linux CI runners; keying
    this off ``get_profile()`` would route a Linux runner into the
    ``getpeereid`` branch (undefined symbol → 401), which is exactly the
    regression this guard avoids. Tests override THIS function, not the profile.
    """
    return sys.platform.startswith("linux")


def _get_peer_eid(sock: socket.socket) -> tuple[int | None, int | None]:
    """Extract the peer process's effective uid + gid from a unix-socket conn.

    OS-dispatched on :func:`_is_linux` (the real ``sys.platform``): Linux uses
    ``getsockopt(SOL_SOCKET, SO_PEERCRED)``; macOS/BSD uses ``getpeereid(3)``
    via libc ctypes. glibc does not ship ``getpeereid``, so the macOS path
    returns ``(None, None)`` on Linux — porting this branch is what lets the
    admin daemon resolve member-bot uids (and clears the 401 on
    ``/api/directory/digest``). Both mechanisms report the credentials the
    KERNEL recorded for the connecting process — never client-supplied — so the
    uid matches ``pwd.getpwnam(...).pw_uid`` for the same account, the parity
    ``peer_auth``'s trusted-user list relies on. Returns ``(None, None)`` on any
    failure so the caller sets uid -1 and ``peer_auth`` 403s an unknown peer
    rather than crashing the request.
    """
    if _is_linux():
        return _peer_eid_so_peercred(sock)
    return _peer_eid_getpeereid(sock)


# ── WSGI server bound to a unix socket ───────────────────────────────────────


class _UnixWSGIRequestHandler(WSGIRequestHandler):
    """Request handler that injects peer credentials into the WSGI environ.

    Sets three environ keys per request:
      - ``REMOTE_TRANSPORT``: always ``"unix-socket"`` for this server.
        Decorators check this to distinguish unix-socket requests from
        TCP requests; the latter never carry trusted peer credentials.
      - ``REMOTE_PEER_UID``: the connecting process's effective uid,
        or ``-1`` if extraction failed.
      - ``REMOTE_PEER_GID``: the connecting process's effective gid,
        or ``-1`` if extraction failed.

    The standard WSGI environ keys (``REMOTE_ADDR``, ``REMOTE_HOST``)
    aren't meaningful for unix sockets — we fill them with a stable
    sentinel so downstream code that inspects them sees a coherent
    value rather than empty strings or AF_UNIX raw addresses.
    """

    # Quiet down the default request logger — gets noisy for the high-
    # frequency tool-call traffic over this socket. Per-route logging
    # happens at the Flask layer.
    def log_request(self, code: str | int = "-", size: str | int = "-") -> None:  # noqa: ARG002
        return

    def address_string(self) -> str:
        return "<unix-socket-peer>"

    def setup(self) -> None:
        # AF_UNIX accept returns "" for client_address, but
        # WSGIRequestHandler.get_environ expects a (host, port) tuple
        # (it indexes ``self.client_address[0]``). Coerce to a tuple
        # before super().setup() bridges request streams.
        if not isinstance(self.client_address, tuple):
            self.client_address = ("<unix-socket-peer>", 0)
        super().setup()

    def get_environ(self) -> dict[str, Any]:
        env = super().get_environ()
        env["REMOTE_TRANSPORT"] = "unix-socket"
        env["REMOTE_ADDR"] = "<unix-socket-peer>"
        env["REMOTE_HOST"] = "<unix-socket-peer>"
        uid, gid = _get_peer_eid(self.connection)
        env["REMOTE_PEER_UID"] = uid if uid is not None else -1
        env["REMOTE_PEER_GID"] = gid if gid is not None else -1
        return env


class _UnixWSGIServer(
    socketserver.ThreadingMixIn, socketserver.UnixStreamServer, WSGIServer,
):
    """WSGIServer subclass that listens on a unix socket instead of TCP.

    Replaces the TCP socket family in the base ``WSGIServer`` /
    ``HTTPServer`` chain with ``AF_UNIX``. ``socketserver.UnixStreamServer``
    handles the AF_UNIX bind; we layer ``WSGIServer.set_app`` on top so
    Flask works through it unchanged.

    ``ThreadingMixIn`` spawns a worker thread per accepted connection
    instead of processing requests inline in the accept loop. This
    matters because the base ``wsgiref.simple_server.WSGIServer`` is
    single-threaded synchronous — one slow request would block ALL new
    connections from being accepted. Observed in production 2026-05-26
    post-Phase-E cutover: an unknown request wedged the daemon thread
    into an unresponsive state where the accept-loop kept sitting in
    ``select()`` but never drained queued connections. The recovery
    wrapper added in PR #1586 didn't help because the thread wasn't
    crashing — it was just hung. Threading the request handling
    eliminates the failure mode at its root.

    ``daemon_threads = True`` so the request workers are daemon threads
    — process shutdown doesn't get blocked by an in-flight request that
    refuses to finish. Matches the daemon=True on the parent serve
    thread.
    """

    address_family = socket.AF_UNIX
    # See class docstring — threaded request handling prevents one
    # bad request from wedging the accept loop.
    daemon_threads = True

    def get_request(self):
        """accept() with EMFILE/OSError survival (the 2026-07-28 incident).

        During an fd-exhaustion storm, ``accept()`` raises ``OSError``
        (EMFILE) on every ready connection. ``socketserver``'s
        ``_handle_request_noblock`` swallows ``OSError`` from
        ``get_request`` *silently* and keeps polling — the loop survives,
        but (a) nothing is logged, and (b) with the backlog perpetually
        ready, the swallow-and-retry spins hot and keeps competing for
        the very fds the storm exhausted. Log at ERROR with the errno and
        back off briefly (exponential, capped) before re-raising into the
        stdlib's swallow path so the accept loop always continues.
        """
        try:
            result = super().get_request()
        except OSError as exc:
            self._accept_error_streak = getattr(self, "_accept_error_streak", 0) + 1
            backoff = min(
                _ACCEPT_BACKOFF_MIN_SECONDS * (2 ** (self._accept_error_streak - 1)),
                _ACCEPT_BACKOFF_MAX_SECONDS,
            )
            log.error(
                "unix_socket_server: accept() failed (errno=%s: %s; streak=%d) "
                "— backing off %.2fs and continuing to accept",
                exc.errno, exc, self._accept_error_streak, backoff,
            )
            time.sleep(backoff)
            raise  # socketserver._handle_request_noblock swallows OSError
        self._accept_error_streak = 0
        return result

    def process_request(self, request, client_address):
        """ThreadingMixIn.process_request with resource-storm survival.

        ``ThreadingMixIn`` starts a worker thread per accepted connection;
        under fd/thread exhaustion ``Thread.start()`` raises
        (``RuntimeError: can't start new thread``, or ``OSError``) and
        would propagate out of ``serve_forever`` — tearing down a healthy
        bound listener (and its backlog) for a transient storm. Drop the
        one connection instead: log, close it, keep the listener bound.
        """
        try:
            super().process_request(request, client_address)
        except (OSError, RuntimeError) as exc:
            log.error(
                "unix_socket_server: could not service accepted connection "
                "(%s: %s) — dropping it; listener stays up", type(exc).__name__, exc,
            )
            try:
                self.shutdown_request(request)
            except Exception:  # noqa: BLE001 — request teardown is best-effort
                log.debug("unix_socket_server: shutdown_request raised; ignoring")

    def __init__(
        self,
        socket_path: str,
        app: Any,
        socket_mode: int = DEFAULT_SOCKET_MODE,
        socket_group: str | None = DEFAULT_SOCKET_GROUP,
    ):
        # Set socket_mode + socket_group BEFORE __init__ — server_bind() runs
        # inside __init__ and reads both fields to apply chmod + chown.
        self._socket_mode = socket_mode
        self._socket_group = socket_group

        # Clean up any leftover socket file from a previous server crash.
        # macOS unix domain sockets aren't auto-removed on process exit.
        try:
            if os.path.exists(socket_path):
                os.unlink(socket_path)
        except OSError as exc:
            log.warning(
                "unix_socket_server: failed to unlink stale %s: %s",
                socket_path, exc,
            )

        # Initialize the WSGI bits (sets handler class, the application
        # slot, server_name, server_port). server_bind runs during init.
        WSGIServer.__init__(
            self,
            (socket_path, 0),  # placeholder; UnixStreamServer ignores port
            _UnixWSGIRequestHandler,
        )
        self.set_app(app)

    def server_bind(self) -> None:
        """Bind the AF_UNIX socket and set its file mode.

        We override server_bind because ``HTTPServer.server_bind`` calls
        ``socket.getfqdn`` on the address, which doesn't make sense for
        ``AF_UNIX``. Skip that step and just bind + chmod, then call
        ``WSGIServer.setup_environ()`` ourselves so the request handler's
        ``get_environ()`` has the WSGI base environ it expects.
        """
        # ``server_address`` is the (path, 0) tuple we passed; for AF_UNIX
        # we actually bind on the path string only.
        if isinstance(self.server_address, tuple):
            sock_path = self.server_address[0]
        else:
            sock_path = self.server_address
        self.socket.bind(sock_path)
        self.server_address = sock_path
        # Set socket file permissions explicitly. macOS doesn't honor
        # umask consistently across versions on AF_UNIX binds.
        try:
            os.chmod(sock_path, self._socket_mode)
        except OSError as exc:
            log.warning(
                "unix_socket_server: chmod %o %s failed: %s",
                self._socket_mode, sock_path, exc,
            )
        # Set group ownership to the shared bot group so bot users can connect
        # under mode 0660. On macOS (group ``staff``) this IS the reach
        # mechanism. On Linux (group ``evolve-bots``) ``evolve`` is not a member
        # so this EPERMs and fails soft — the named group ACE below is what
        # actually grants connect there. Without any chgrp the socket inherits
        # the daemon's effective group (``wheel`` on macOS launchd pods),
        # locking bots out. Best-effort: log + continue (matches chmod).
        if self._socket_group:
            try:
                import grp
                gid = grp.getgrnam(self._socket_group).gr_gid
                os.chown(sock_path, -1, gid)
            except (KeyError, OSError) as exc:
                log.warning(
                    "unix_socket_server: chgrp %s %s failed: %s",
                    self._socket_group, sock_path, exc,
                )
        # Linux: every bot gateway runs as a user in the shared ``evolve-bots``
        # group but NOT in the socket-owning ``evolve`` group, so each lands on
        # the socket's ``other`` class = ``r--`` under mode 0660 and a unix
        # connect() — a WRITE on the inode — returns EACCES, breaking the
        # per-turn digest / RosterTools pod-wide (gateway_slow band). Grant the
        # shared bot group a NAMED write(connect) ACE, owner-direct (no sudo),
        # never widening ``other``. The socket is recreated on every (re)bind,
        # so the grant MUST re-apply here; ensure_pod_perms re-asserts it as the
        # backstop. No-op on macOS (bots reach via ``staff`` group ownership).
        # ``str(...)`` pins the arg to the ``str | Path`` signature — bind()
        # accepts the broader AF_UNIX address type, but the ACL helper only
        # ever needs the filesystem path string (it re-wraps in ``Path``).
        ensure_bot_socket_acl(str(sock_path))
        # ``setup_environ`` (called next) reads ``server_name`` and
        # ``server_port`` from the instance. HTTPServer.server_bind
        # normally sets those, but we skipped it. For AF_UNIX neither
        # value is meaningful; use stable sentinels so the WSGI environ
        # gets populated cleanly.
        self.server_name = "<unix-socket>"
        self.server_port = 0
        # WSGIServer.server_bind normally calls setup_environ() after
        # HTTPServer.server_bind. Since we skipped HTTPServer.server_bind,
        # call setup_environ explicitly here so base_environ is
        # populated for the request handler's get_environ.
        self.setup_environ()

    def server_close(self) -> None:
        """Close the socket and remove the file."""
        super().server_close()
        try:
            sock_path = (
                self.server_address[0]
                if isinstance(self.server_address, tuple)
                else self.server_address
            )
            if os.path.exists(sock_path):
                os.unlink(sock_path)
        except OSError:
            pass


# ── Public entrypoint ────────────────────────────────────────────────────────


# Bug A defense — when ``serve_forever`` raises, wait this long before
# attempting to re-bind the socket and resume serving. Hard-coded
# small constant; not worth a settings knob until we know more about
# the failure mode. The cap on rebind attempts prevents a wedged
# socket from spinning the CPU.
_REBIND_BACKOFF_SECONDS = 5.0
_REBIND_MAX_ATTEMPTS = 12  # ~1 minute of attempts before giving up

# A crash after this much healthy uptime starts a NEW failure streak
# instead of extending the old one. Pre-2026-07-28 the counter never
# reset, so crash bursts *days apart* silently consumed the 12-attempt
# budget and the recovery wrapper eventually gave up for good — one
# ingredient of the 10-day silent unix-socket outage. "Consecutive"
# now means consecutive in time, not cumulative over the process life.
_FAILURE_STREAK_RESET_SECONDS = 60.0

# Accept-path backoff after an OSError from accept() (EMFILE storm) —
# exponential from MIN, capped at MAX, reset on the next good accept.
_ACCEPT_BACKOFF_MIN_SECONDS = 0.5
_ACCEPT_BACKOFF_MAX_SECONDS = 2.0

# Liveness watchdog — period and timeout for the periodic self-probe.
# Cheap to run every 30s (single tiny connect+request). The 3s timeout
# is generous; the loopback unix-socket should answer in <1ms when
# healthy. Two consecutive failures trigger a teardown + rebind to
# limit false positives from one-off transient stalls.
_LIVENESS_PROBE_PERIOD_SECONDS = 30.0
_LIVENESS_PROBE_TIMEOUT_SECONDS = 3.0
_LIVENESS_FAILURE_THRESHOLD = 2


def _probe_socket_alive(socket_path: str, timeout: float) -> bool:
    """Connect to the socket and verify a request gets a response.

    Returns True iff we can connect, send a minimal HTTP request, and
    receive at least one byte back within ``timeout`` seconds. Used by
    the liveness watchdog to catch the "thread is in accept() but
    nothing drains" hang state observed in production 2026-05-26.

    Caller's responsibility to size the timeout — a healthy loopback
    unix-socket answers in sub-millisecond time; anything beyond a few
    seconds is hung.

    Socket *creation* lives inside the try: under fd exhaustion
    ``socket.socket()`` itself raises OSError(EMFILE). Pre-2026-07-28
    that escaped this function and silently killed the watchdog thread —
    exactly when the daemon most needed its watchdog.
    """
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(socket_path)
        # /api/health is unconditional and cheap — the TCP side responds
        # to it constantly. If the unix-socket path is healthy, same.
        s.sendall(b"GET /api/health HTTP/1.0\r\nHost: localhost\r\n\r\n")
        data = s.recv(64)
        return bool(data)
    except (socket.timeout, OSError):
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _serve_with_recovery(
    server_factory,
    *,
    initial_server: "_UnixWSGIServer | None" = None,
    rebind_request: "threading.Event | None" = None,
) -> None:
    """Run ``serve_forever`` in a loop, rebinding on crash.

    Bug A (observed 2026-05-26): production admin-ui's daemon thread
    silently exited some time after a successful startup, leaving the
    socket file on disk with no listener. ``connect()`` returned
    ECONNREFUSED for every client (including the daemon's own
    ``evolve`` user). Nothing in the err log — pre-fix
    ``serve_forever`` ran as a bare daemon-thread target, so any
    exception propagated up and silently killed the thread.

    Defense:
      1. Wrap ``serve_forever`` in try/except. ANY exception is logged
         via ``log.exception`` (full traceback). No more silent death.
      2. On exception, sleep ``_REBIND_BACKOFF_SECONDS`` and try to
         re-bind a fresh server via ``server_factory()``. The factory
         re-creates ``_UnixWSGIServer``, which unlinks any stale
         socket file and re-binds + re-listens.
      3. Bail after ``_REBIND_MAX_ATTEMPTS`` consecutive failures —
         a permanent error (e.g. bind permission denied) shouldn't
         spin forever.

    Tests pass an ``initial_server`` so the first iteration uses an
    already-constructed instance (matches the public
    ``start_in_background`` shape that callers expect to receive a
    server handle for shutdown).

    2026-07-28 incident hardening — two holes closed:

    1. **Watchdog teardown was permanent death, not a rebind.**
       ``server.shutdown()`` makes ``serve_forever`` *return cleanly* —
       it does NOT raise. The pre-fix wrapper treated every clean return
       as an intentional stop, so the liveness watchdog's "tear down to
       trigger rebind" actually terminated the serve loop for good:
       one hung-listener detection → permanent silent outage.
       ``rebind_request`` (a shared Event the watchdog sets *before*
       calling shutdown) distinguishes the two: set → close + rebind;
       unset → a genuine external shutdown (tests, process exit) →
       return.
    2. **The failure budget never reset**, so crash bursts days apart
       eventually exhausted the 12 attempts and the wrapper gave up
       forever. A crash after ``_FAILURE_STREAK_RESET_SECONDS`` of
       healthy serving now starts a fresh streak.
    """
    server = initial_server
    failures = 0
    while True:
        if server is None:
            try:
                server = server_factory()
            except Exception:  # noqa: BLE001
                failures += 1
                log.exception(
                    "unix_socket_server: rebind attempt %d/%d failed",
                    failures, _REBIND_MAX_ATTEMPTS,
                )
                if failures >= _REBIND_MAX_ATTEMPTS:
                    log.error(
                        "unix_socket_server: %d consecutive rebind "
                        "failures — giving up. Listener is permanently "
                        "down until admin-ui restarts.",
                        _REBIND_MAX_ATTEMPTS,
                    )
                    return
                time.sleep(_REBIND_BACKOFF_SECONDS)
                continue
        served_since = time.monotonic()
        try:
            server.serve_forever()
            if rebind_request is not None and rebind_request.is_set():
                # The liveness watchdog asked for a teardown+rebind.
                # shutdown() surfaces here as a CLEAN return (it never
                # raises out of serve_forever) — rebind, don't exit.
                rebind_request.clear()
                log.warning(
                    "unix_socket_server: watchdog-requested teardown — "
                    "closing and rebinding the listener",
                )
                try:
                    server.server_close()
                except Exception:  # noqa: BLE001
                    log.debug("unix_socket_server: server_close raised; ignoring")
                server = None
                failures = 0  # a deliberate recovery, not a crash
                time.sleep(_REBIND_BACKOFF_SECONDS)
                continue
            log.warning(
                "unix_socket_server: serve_forever returned cleanly "
                "(unexpected — likely shutdown requested)"
            )
            return  # clean shutdown
        except Exception:  # noqa: BLE001
            if time.monotonic() - served_since >= _FAILURE_STREAK_RESET_SECONDS:
                # Healthy for a while before this crash — new streak.
                # Without this, bursts days apart cumulatively exhaust
                # the budget (the 2026-07-28 10-day outage ingredient).
                failures = 0
            failures += 1
            log.exception(
                "unix_socket_server: serve_forever crashed "
                "(attempt %d/%d) — listener is dead; rebinding in %.1fs",
                failures, _REBIND_MAX_ATTEMPTS,
                _REBIND_BACKOFF_SECONDS,
            )
            # Close the dead server so its file descriptors don't leak
            # and so the socket file gets cleaned up before the next
            # bind attempt. (server is None here only if the rebind
            # branch above already closed it and then its sleep raised.)
            if server is not None:
                try:
                    server.server_close()
                except Exception:  # noqa: BLE001
                    log.debug("unix_socket_server: server_close raised; ignoring")
            server = None  # force factory to make a new one
            if failures >= _REBIND_MAX_ATTEMPTS:
                log.error(
                    "unix_socket_server: %d consecutive serve_forever "
                    "crashes — giving up.", _REBIND_MAX_ATTEMPTS,
                )
                return
            time.sleep(_REBIND_BACKOFF_SECONDS)


def _run_liveness_watchdog(
    socket_path: str,
    server_ref: list,
    *,
    period: float = _LIVENESS_PROBE_PERIOD_SECONDS,
    timeout: float = _LIVENESS_PROBE_TIMEOUT_SECONDS,
    failure_threshold: int = _LIVENESS_FAILURE_THRESHOLD,
    rebind_request: "threading.Event | None" = None,
    stop_event: "threading.Event | None" = None,
) -> None:
    """Periodically self-probe the unix socket; rebind on consecutive failures.

    Catches the "thread is in accept() but never drains" hang state
    observed in production 2026-05-26. The ``_serve_with_recovery``
    wrapper only catches *crashes*; a hang leaves the thread stuck in
    select forever with no exception to trip the wrapper.

    Strategy: every ``period`` seconds, ``_probe_socket_alive`` connects
    + sends a minimal /api/health and waits ``timeout`` for a response.
    On ``failure_threshold`` consecutive failures, call
    ``server.shutdown()`` on the current server, which raises out of
    ``serve_forever`` in the daemon thread — the recovery wrapper
    catches it and rebinds.

    ``server_ref`` is a single-element list so the watchdog sees the
    current server across rebinds (Python doesn't do mutable closures
    over rebound locals cleanly without an explicit container). The
    serve thread updates index 0 whenever it rebinds.

    Failure threshold of 2 keeps false positives down — a one-off
    300ms blip during heavy admin-ui activity shouldn't trigger a
    pointless rebind.

    ``rebind_request`` MUST be set *before* calling ``shutdown()`` on
    the serve thread's server: ``shutdown()`` makes ``serve_forever``
    return cleanly (it does not raise), and the recovery wrapper only
    rebinds on a clean return when this event is set — otherwise the
    teardown would permanently kill the listener (the pre-fix behavior
    behind the 2026-07-28 10-day outage).

    The loop body is exception-proof: this thread is the last line of
    defense and must never die to a probe/teardown hiccup. ``stop_event``
    lets tests end the loop; production never sets it.
    """
    consecutive_failures = 0
    while stop_event is None or not stop_event.is_set():
        time.sleep(period)
        try:
            if _probe_socket_alive(socket_path, timeout):
                if consecutive_failures > 0:
                    log.info(
                        "unix_socket_server: liveness recovered after %d failures",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            log.warning(
                "unix_socket_server: liveness probe failed (%d/%d)",
                consecutive_failures, failure_threshold,
            )
            if consecutive_failures < failure_threshold:
                continue
            log.error(
                "unix_socket_server: %d consecutive liveness failures — "
                "listener is hung; tearing down to trigger rebind",
                failure_threshold,
            )
            srv = server_ref[0] if server_ref else None
            if srv is None:
                log.warning("unix_socket_server: no server handle to tear down")
                consecutive_failures = 0
                continue
            # Flag the intent FIRST so the recovery wrapper interprets the
            # clean serve_forever return as "rebind me", then shut down.
            if rebind_request is not None:
                rebind_request.set()
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("unix_socket_server: shutdown() raised during watchdog teardown")
            consecutive_failures = 0
        except Exception:  # noqa: BLE001 — the watchdog must outlive anything
            log.exception(
                "unix_socket_server: watchdog cycle raised — continuing",
            )
            consecutive_failures = 0


def start_in_background(
    app: Any,
    socket_path: str | Path = DEFAULT_SOCKET_PATH,
    *,
    socket_mode: int = DEFAULT_SOCKET_MODE,
    socket_group: str | None = DEFAULT_SOCKET_GROUP,
    enable_watchdog: bool = True,
) -> tuple[threading.Thread, _UnixWSGIServer]:
    """Bind a unix-socket WSGI server and run it in a daemon thread.

    Returns ``(thread, server)`` so callers can ``server.shutdown()`` /
    ``server.server_close()`` for clean teardown in tests. The thread is
    daemonic — when the main process exits, the thread goes with it.

    The Flask app's routes are accessible via this binding identically
    to the TCP binding, with the additional ``REMOTE_TRANSPORT``/
    ``REMOTE_PEER_UID``/``REMOTE_PEER_GID`` environ keys populated.
    Routes that should ONLY be reachable from trusted peers must guard
    with ``@require_trusted_peer`` from ``peer_auth``.

    Reliability layers:
      1. ``_UnixWSGIServer`` uses ``ThreadingMixIn`` — one bad request
         can't block the accept loop (per-request worker threads).
      2. ``_serve_with_recovery`` wraps ``serve_forever`` and rebinds
         on crash.
      3. ``_run_liveness_watchdog`` (when ``enable_watchdog=True``,
         the default) periodically self-probes the socket and triggers
         a rebind via ``server.shutdown()`` on consecutive timeouts.

    Tests set ``enable_watchdog=False`` to avoid the background probe
    thread interacting with their sleep timing. Production callers
    leave it on.
    """
    socket_path = str(socket_path)
    server = _UnixWSGIServer(
        socket_path, app,
        socket_mode=socket_mode, socket_group=socket_group,
    )

    # ``server_ref`` is a single-element list shared with both the
    # serve thread (updates index 0 on rebind) and the watchdog (reads
    # index 0 to know which server to tear down). Plain mutable
    # container — cheap and avoids the cross-thread closure dance.
    server_ref = [server]

    # Watchdog → recovery-wrapper handshake: the watchdog sets this
    # BEFORE server.shutdown() so the wrapper knows the resulting clean
    # serve_forever return means "rebind", not "stop". Without it a
    # watchdog teardown permanently killed the listener (2026-07-28).
    rebind_request = threading.Event()

    def _factory() -> _UnixWSGIServer:
        new_server = _UnixWSGIServer(
            socket_path, app,
            socket_mode=socket_mode, socket_group=socket_group,
        )
        server_ref[0] = new_server
        return new_server

    thread = threading.Thread(
        target=_serve_with_recovery,
        args=(_factory,),
        kwargs={"initial_server": server, "rebind_request": rebind_request},
        daemon=True,
        name="admin-daemon-unix-socket",
    )
    thread.start()

    if enable_watchdog:
        watchdog = threading.Thread(
            target=_run_liveness_watchdog,
            args=(socket_path, server_ref),
            kwargs={"rebind_request": rebind_request},
            daemon=True,
            name="admin-daemon-unix-socket-watchdog",
        )
        watchdog.start()

    log.info(
        "unix_socket_server: listening on %s (mode %o, watchdog=%s)",
        socket_path, socket_mode, enable_watchdog,
    )
    return thread, server
