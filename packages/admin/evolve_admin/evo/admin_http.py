"""admin_http — socket-first transport for evo's HTTP-routed tools (roadmap 2.6).

Auth is on by default now: a device-cookie gate fronts the TCP admin API, so
evo's tools — which are local processes with no browser pairing — would 401 if
they kept calling over TCP loopback. evo authenticates to the admin daemon by
unix-socket *peer-uid* instead (the daemon exempts a trusted socket peer from
the cookie gate; see server._enforce_device_auth).

This module is a drop-in replacement for ``urllib.request.urlopen`` that tries
the peer-authed unix socket first and falls back to TCP when the socket is
unreachable (pre-cutover pods, socket down, or auth opted out). Each TCP tool
swaps its single ``urllib.request.urlopen(req, timeout=…)`` call for
``urlopen_admin(req, timeout=…)`` and changes nothing else — request building
and response parsing are untouched, because ``urlopen_admin`` returns the same
``with … as resp: resp.read()`` shape and raises the same
``urllib.error.HTTPError`` on non-2xx.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit


# Hosts that resolve to the local admin daemon. Any other host (a provider
# API like api.anthropic.com, a webhook, anything on the network) is not an
# admin-API request: rerouting it would silently drop the host and POST the
# bare path at the local daemon. That exact misuse shipped in the 2.6 sweep
# (#2621) on the wizard extractor's api.anthropic.com call and killed every
# LLM-extraction wizard on socket-enabled pods until 2026-06-11.
_ADMIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _SocketResponse:
    """Minimal stand-in for an http.client.HTTPResponse — supports the
    ``with urlopen(...) as resp: resp.read()`` contract the tools use."""

    def __init__(self, body: bytes, status: int) -> None:
        self._body = body
        self.status = status
        self.code = status

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_SocketResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def urlopen_admin(req: urllib.request.Request, timeout: float = 10):
    """Like ``urllib.request.urlopen`` for admin-API requests, socket-first.

    Tries the admin daemon's unix socket (peer-uid auth) first; on any socket
    unavailability falls back to the original TCP request. Mirrors urlopen's
    interface: returns a context-managed response with ``.read()``, raises
    ``urllib.error.HTTPError`` for 4xx/5xx so callers' existing handlers fire.
    """
    full_url = req.full_url
    method = req.get_method()
    split = urlsplit(full_url)
    if split.hostname not in _ADMIN_HOSTS:
        # Not the local admin daemon — never socket-route; plain TCP.
        return urllib.request.urlopen(req, timeout=timeout)
    path = split.path + (f"?{split.query}" if split.query else "")

    body = None
    raw = req.data
    if isinstance(raw, (bytes, bytearray, str)):
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001 — non-JSON body → let TCP handle it
            return urllib.request.urlopen(req, timeout=timeout)
    elif raw is not None:
        # A streaming/iterable body we can't re-encode for the socket call —
        # let the TCP path handle it verbatim.
        return urllib.request.urlopen(req, timeout=timeout)

    try:
        from .admin_client import try_daemon_call
    except Exception:  # noqa: BLE001 — admin_client unimportable → TCP
        return urllib.request.urlopen(req, timeout=timeout)

    used, status, payload = try_daemon_call(method, path, body, timeout=timeout)
    if not used:
        # Socket unreachable (pre-cutover pod, daemon down, libc missing) —
        # fall back to TCP. On an auth-enforcing pod with the socket down this
        # 401s, but a down admin socket is already a degraded state.
        return urllib.request.urlopen(req, timeout=timeout)

    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
    if status is not None and status >= 400:
        from email.message import Message
        raise urllib.error.HTTPError(
            full_url, status, f"HTTP {status}", Message(), io.BytesIO(body_bytes),
        )
    return _SocketResponse(body_bytes, status or 200)
