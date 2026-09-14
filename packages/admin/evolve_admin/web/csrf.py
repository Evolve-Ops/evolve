"""csrf — Origin/Host validation + double-submit CSRF tokens (roadmap 2.7).

The admin server binds 127.0.0.1, but loopback + a device cookie is not a
CSRF defense: a malicious web page the operator visits can issue
cross-origin POSTs to ``http://127.0.0.1:5050`` and the browser attaches the
``evolve_device`` cookie, so the request runs with the operator's authority.
``SameSite=Lax`` on the device cookie blocks cross-site *form/navigation*
POSTs but not all shapes (and not DNS-rebinding). Decision D4
(internal/decision-security-defaults-2026-06-10.md) adds explicit defense:

  1. **Origin/Referer same-origin check** on mutating methods — when the
     header is present (browsers always send ``Origin`` on POST), its host
     must equal the request ``Host``. This is the load-bearing control and
     the anti-DNS-rebinding defense (a rebound page's Origin ≠ the loopback
     Host).
  2. **Double-submit CSRF token** — a readable ``evolve_csrf`` cookie whose
     value must be echoed in the ``X-CSRF-Token`` header on mutating
     requests. Same-origin policy stops a cross-origin page from reading the
     cookie, and a custom header forces a CORS preflight the server never
     satisfies — so only same-origin JS (ours) can produce the header.
  3. **Host allowlist** — for the rare Origin-less mutating request, the
     ``Host`` must be loopback, the configured ``adminBaseUrl`` host, or a
     ``network.json::trustedHosts`` entry.

Scope: enforced only for **cookie-authenticated** requests (``is_auth``
True) — a request with no device cookie carries no ambient authority to
ride, so there is nothing to forge. Unix-socket requests are exempt (the
admin daemon authenticates those by kernel peer-uid, not a cookie). The
pairing submit (``/api/pair``) is exempt from the *token* check (the device
has no CSRF cookie until it pairs) but still gets the Origin/Host check.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

CSRF_COOKIE_NAME = "evolve_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Paths exempt from the CSRF *token* requirement (Origin/Host still apply):
# the pairing submit runs before the device has a CSRF cookie.
TOKEN_EXEMPT_PATHS = frozenset({"/api/pair"})

_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1", "localhost", "[::1]", "::1",
})


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _host_only(host_header: str) -> str:
    """Strip an optional port from a Host/Origin host value."""
    h = (host_header or "").strip().lower()
    if h.startswith("[") and "]" in h:  # bracketed IPv6, keep the brackets
        return h.split("]")[0] + "]"
    return h.split(":")[0]


def allowed_hosts(network: dict | None) -> set[str]:
    """Host values accepted for an Origin-less mutating request.

    Loopback always, plus the configured ``adminBaseUrl`` host and any
    ``trustedHosts`` (host-only, lowercased). The set is intentionally small;
    a remote operator reaches the UI over an SSH/Tailscale tunnel whose Host
    is either loopback (tunnel) or their configured ``adminBaseUrl``.
    """
    out = {_host_only(h) for h in _LOOPBACK_HOSTS}
    net = network or {}
    base = net.get("adminBaseUrl")
    if isinstance(base, str) and base:
        out.add(_host_only(urlsplit(base).hostname or base))
    trusted = net.get("trustedHosts")
    if isinstance(trusted, list):
        out.update(_host_only(str(h)) for h in trusted if h)
    return {h for h in out if h}


def _origin_host(headers, get) -> str | None:
    """The host of the request's Origin (preferred) or Referer, or None."""
    origin = get("Origin")
    if origin:
        return _host_only(urlsplit(origin).netloc or origin)
    referer = get("Referer")
    if referer:
        return _host_only(urlsplit(referer).netloc or referer)
    return None


def check_request(
    *,
    method: str,
    path: str,
    transport: str,
    is_authenticated: bool,
    request_host: str,
    header_get,
    cookie_get,
    network: dict | None,
) -> tuple[bool, str]:
    """Evaluate the CSRF/Origin/Host policy for one request.

    Returns ``(ok, reason)``. ``ok=True`` (reason "") means allow. Pure —
    no Flask import — so it unit-tests without a request context.

    ``header_get`` / ``cookie_get`` are ``name -> value | None`` callables.
    """
    if method.upper() in SAFE_METHODS:
        return True, ""
    if transport == "unix-socket":
        return True, ""  # peer-uid authenticated; no ambient cookie to ride
    if not is_authenticated:
        return True, ""  # no session to forge

    host = _host_only(request_host)
    origin_host = _origin_host(None, header_get)

    # 1/3. Origin present → must be same-origin. Absent → Host must be known.
    if origin_host is not None:
        if origin_host != host:
            return False, (
                f"cross-origin {method} rejected: Origin host {origin_host!r} "
                f"!= request host {host!r}"
            )
    else:
        if host not in allowed_hosts(network):
            return False, (
                f"{method} with no Origin/Referer and unrecognized Host "
                f"{host!r} rejected (possible DNS-rebinding)"
            )

    # 2. Double-submit token (skip the bootstrap pairing submit).
    if path not in TOKEN_EXEMPT_PATHS:
        cookie_tok = cookie_get(CSRF_COOKIE_NAME)
        header_tok = header_get(CSRF_HEADER_NAME)
        if not cookie_tok or not header_tok:
            return False, "missing CSRF token (cookie + X-CSRF-Token header required)"
        if not secrets.compare_digest(str(cookie_tok), str(header_tok)):
            return False, "CSRF token mismatch"

    return True, ""
