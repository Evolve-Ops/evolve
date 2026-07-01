"""
evolve_admin.mcp_bridge.auth — Request authentication for the MCP bridge.

Two modes:
  tailscale  — Only Tailscale IPs (100.x.x.x/10) and localhost are allowed.
               No credentials needed in the Claude Desktop config.
               Default for single-user home setups.

  api_key    — Requires `Authorization: Bearer <key>` on every request.
               Used as a second factor on top of Tailscale for shared setups,
               or when Tailscale is not in use (not recommended).

  none       — No auth. Only use on a firewalled loopback interface.
"""

from __future__ import annotations

# Tailscale's CGNAT range: 100.64.0.0/10 → 100.64.x.x – 100.127.x.x
_TAILSCALE_PREFIXES = tuple(
    f"100.{i}." for i in range(64, 128)
)
_LOCAL_PREFIXES = ("127.", "::1", "localhost")


class AuthError(Exception):
    pass


class Auth:
    def __init__(self, mode: str = "tailscale", api_key: str | None = None) -> None:
        if mode not in ("tailscale", "api_key", "none"):
            raise ValueError(f"Unknown auth mode: {mode!r}")
        self._mode = mode
        self._api_key = api_key

    @property
    def mode(self) -> str:
        return self._mode

    def check(self, client_ip: str, auth_header: str | None) -> None:
        """
        Raise AuthError if the request should be rejected.
        client_ip  — the connecting IP address (string, no port)
        auth_header — value of the Authorization header, or None
        """
        if self._mode == "none":
            return

        if self._mode == "tailscale":
            is_ts = any(client_ip.startswith(p) for p in _TAILSCALE_PREFIXES)
            is_local = any(
                client_ip.startswith(p) for p in _LOCAL_PREFIXES
            )
            if not (is_ts or is_local):
                raise AuthError(
                    f"Rejected: {client_ip!r} is not a Tailscale or local address. "
                    "Only devices on your Tailscale network may access the MCP bridge."
                )
            return

        if self._mode == "api_key":
            if not auth_header:
                raise AuthError("Missing Authorization header (expected: Bearer <key>)")
            if not auth_header.startswith("Bearer "):
                raise AuthError("Malformed Authorization header (expected: Bearer <key>)")
            key = auth_header[7:].strip()
            if not self._api_key:
                raise AuthError("API key auth is configured but no key is set — check network.json")
            if key != self._api_key:
                raise AuthError("Invalid API key")
