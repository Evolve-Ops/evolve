"""evolve_admin.evo.admin_client — HTTP client over the admin-daemon unix socket.

Phase E.3 (docs/spec-evo-account-separation-2026-05-25.md): evo's tools
use this client to call the admin daemon's typed API instead of
reading from the filesystem directly. Once evo runs as the unprivileged
``evo`` user (Phase E.2.b cutover), direct fs reads of other bots'
``.openclaw/`` trees stop working — the client routes those requests
through the admin daemon which still has the cross-bot ACL access.

The client uses Python's stdlib ``http.client`` with a custom
connection class that talks over a unix socket. No third-party deps.
Requests carry no auth header — the admin daemon authenticates via the
unix-socket peer-credential check (``REMOTE_PEER_UID`` in the WSGI
environ), so the *fact that we can connect at all* is the auth.

Falls back to a direct-fs path during the migration window when the
admin daemon hasn't been restarted yet to pick up the new unix-socket
binding. The fallback shape lives in each tool — this module just
raises ``AdminDaemonUnavailable`` if the socket isn't reachable.
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR

log = logging.getLogger(__name__)

# Platform-keyed: ``/Users/Shared/evolve`` on macOS, ``/var/lib/evolve``
# on Linux. Must match the path the admin daemon binds in
# ``web.unix_socket_server`` (see that module's DEFAULT_SOCKET_PATH).
DEFAULT_SOCKET_PATH = Path(CANONICAL_SHARED_DIR) / "admin-daemon.sock"


class AdminDaemonUnavailable(RuntimeError):
    """Raised when the admin daemon's unix socket isn't reachable.

    Reasons this might happen:
      - The admin daemon hasn't been restarted since Phase E.3 deployed
        (no socket bound yet)
      - The socket file was deleted or has wrong permissions
      - libc's getpeereid isn't available (extremely unlikely on macOS)

    Tool callers catch this and fall back to the legacy direct-fs path
    during the Phase E migration window.
    """


def log_daemon_fallback(context: str, exc: BaseException) -> None:
    """One structured WARNING every time the daemon path fails and a legacy
    fallback is about to run.

    During the 2026-07-28 incident every evo tool silently caught
    ``AdminDaemonUnavailable`` (or logged at DEBUG, which production never
    ships) and used its fallback for 10 days — nothing surfaced the outage.
    This is the single, greppable line that must never be muted again. The
    errno rides on the wrapped OSError (``__cause__``): ECONNREFUSED means a
    dead accept loop behind a live socket file — the incident's signature.
    Cheap by design: fallbacks are the exception on a healthy pod.
    """
    cause = getattr(exc, "__cause__", None)
    log.warning(
        "admin-daemon unavailable — falling back to legacy path: %s via %s "
        "(errno=%s cause=%s: %s)",
        context, DEFAULT_SOCKET_PATH,
        getattr(cause, "errno", None),
        type(cause).__name__ if cause is not None else None,
        exc,
    )


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that talks over a unix-domain stream socket.

    The standard ``HTTPConnection`` opens an AF_INET socket to
    ``host:port``. This subclass connects to a filesystem path instead
    by overriding ``connect()``. The rest of the HTTP/1.1 wire protocol
    is identical, so all of ``HTTPConnection``'s methods (request,
    getresponse, etc.) work unchanged.
    """

    def __init__(self, socket_path: str, timeout: float | None = None):
        # The "host" we pass to HTTPConnection is cosmetic — it ends up
        # in the Host: header. ``localhost`` is conventional.
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
            raise AdminDaemonUnavailable(
                f"cannot connect to admin daemon socket {self._socket_path}: {exc}"
            ) from exc
        self.sock = sock


def _request_json(
    method: str,
    path: str,
    *,
    body: Any = None,
    extra_headers: "dict[str, str] | None" = None,
    socket_path: str | Path = DEFAULT_SOCKET_PATH,
    timeout: float = 5.0,
) -> tuple[int, Any]:
    """Internal: issue any HTTP method to the admin daemon over the unix socket.

    Returns ``(http_status, parsed_json_body_or_None)``.

    Connection errors raise ``AdminDaemonUnavailable`` so tool callers
    can fall back to the legacy direct path during the Phase E
    migration window.

    Auth model: peer-credential uid via ``getpeereid`` on the unix
    socket. No Bearer token / API key — the daemon checks the
    connecting process's effective uid against its trusted-user list.

    ``extra_headers`` lets callers attach gateway-attested context like
    ``X-Requester-Identity`` (Phase C of the user-roster spec), so the
    daemon's per-endpoint capability check can resolve the original
    requester instead of trusting the caller blindly.
    """
    conn = _UnixSocketHTTPConnection(str(socket_path), timeout=timeout)
    try:
        headers: dict[str, str] = {}
        payload: "bytes | None" = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        if extra_headers:
            for k, v in extra_headers.items():
                # Skip empty values — header set to "" would otherwise
                # be sent and could confuse the receiver (Flask treats
                # presence with empty value differently than absence).
                if v is None or v == "":
                    continue
                headers[k] = str(v)
        if payload is not None:
            conn.request(method, path, body=payload, headers=headers)
        elif headers:
            conn.request(method, path, headers=headers)
        else:
            conn.request(method, path)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read()
    except (TimeoutError, socket.timeout) as exc:
        raise AdminDaemonUnavailable(f"timeout calling {method} {path}: {exc}") from exc
    except (ConnectionError, OSError) as exc:
        raise AdminDaemonUnavailable(f"network error calling {method} {path}: {exc}") from exc
    finally:
        conn.close()

    if not resp_body:
        return status, None
    try:
        return status, json.loads(resp_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Non-JSON response (e.g. Flask's default 403/500 HTML error
        # page). For HTTP error statuses this is expected; the caller
        # checks ``status`` and decides what to do.
        return status, None


def get_json(
    path: str,
    *,
    extra_headers: "dict[str, str] | None" = None,
    socket_path: str | Path = DEFAULT_SOCKET_PATH,
    timeout: float = 5.0,
) -> tuple[int, Any]:
    """GET ``path`` over the admin daemon's unix socket; return (status, body)."""
    return _request_json(
        "GET", path, extra_headers=extra_headers,
        socket_path=socket_path, timeout=timeout)


def post_json(
    path: str,
    body: Any = None,
    *,
    extra_headers: "dict[str, str] | None" = None,
    socket_path: str | Path = DEFAULT_SOCKET_PATH,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    """POST ``body`` (JSON-encoded) to ``path`` over the admin daemon's unix socket.

    Default timeout is longer than GET (10s vs 5s) because most POST
    endpoints in scope here are write operations (gateway kickstart,
    proposal apply, etc.) that take longer than reads.

    Pass ``body=None`` for endpoints that don't expect a payload (the
    request goes out without a Content-Type header, like an empty POST
    from curl).
    """
    return _request_json(
        "POST", path, body=body, extra_headers=extra_headers,
        socket_path=socket_path, timeout=timeout)


def patch_json(
    path: str,
    body: Any = None,
    *,
    extra_headers: "dict[str, str] | None" = None,
    socket_path: str | Path = DEFAULT_SOCKET_PATH,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    """PATCH — partial update. Added in Phase C of user-roster-and-roles spec
    for the roster mutation endpoints (PATCH /api/admin/bots/<id>/users/...)."""
    return _request_json(
        "PATCH", path, body=body, extra_headers=extra_headers,
        socket_path=socket_path, timeout=timeout)


def put_json(
    path: str,
    body: Any = None,
    *,
    extra_headers: "dict[str, str] | None" = None,
    socket_path: str | Path = DEFAULT_SOCKET_PATH,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    """PUT — replace. Used by PUT /api/admin/bots/<id>/channels/<ch>/newcomer_mode."""
    return _request_json(
        "PUT", path, body=body, extra_headers=extra_headers,
        socket_path=socket_path, timeout=timeout)


def try_daemon_call(
    method: str,
    path: str,
    body: Any = None,
    *,
    extra_headers: "dict[str, str] | None" = None,
    timeout: float | None = None,
) -> tuple[bool, int | None, Any]:
    """Tool-side helper: attempt a daemon call, return (used_daemon, status, body).

    Returns (True, status, body) on a successful HTTP round-trip
    (including 4xx/5xx — the caller decides how to handle status codes).
    Returns (False, None, None) when the daemon is unreachable (socket
    not bound, libc unavailable, etc.) — the caller should run its
    legacy fallback path.

    Encapsulates the "try daemon, fall back on connection failure" idiom
    that every re-plumbed tool uses, so the per-tool code stays a
    one-liner.

    ``extra_headers`` lets callers attach gateway-attested context like
    ``X-Requester-Identity``; the admin daemon's per-endpoint capability
    check reads it when present.
    """
    try:
        method_upper = method.upper()
        if method_upper == "GET":
            status, resp_body = get_json(
                path, extra_headers=extra_headers,
                timeout=timeout or 5.0,
            )
        elif method_upper == "PATCH":
            status, resp_body = patch_json(
                path, body, extra_headers=extra_headers,
                timeout=timeout or 10.0,
            )
        elif method_upper == "PUT":
            status, resp_body = put_json(
                path, body, extra_headers=extra_headers,
                timeout=timeout or 10.0,
            )
        elif method_upper == "DELETE":
            # Method-preserving — a DELETE must NOT silently become a POST.
            # Several routes (e.g. keys remove) share a path between
            # @app.delete and @app.post, so a downgraded verb lands on the
            # wrong handler. (roadmap 2.6 — caught when evo tools moved onto
            # the socket transport.)
            status, resp_body = _request_json(
                "DELETE", path, body=body, extra_headers=extra_headers,
                timeout=timeout or 10.0,
            )
        else:
            status, resp_body = post_json(
                path, body, extra_headers=extra_headers,
                timeout=timeout or 10.0,
            )
        return True, status, resp_body
    except AdminDaemonUnavailable as exc:
        # NEVER silent — see log_daemon_fallback (the 2026-07-28 incident
        # was 10 days of mute fallbacks).
        log_daemon_fallback(f"{method.upper()} {path}", exc)
        return False, None, None
    except Exception:  # noqa: BLE001 — never let an admin-client bug torpedo the tool
        log.warning(
            "admin-daemon call raised unexpectedly — falling back to legacy "
            "path: %s %s via %s",
            method, path, DEFAULT_SOCKET_PATH, exc_info=True,
        )
        return False, None, None
