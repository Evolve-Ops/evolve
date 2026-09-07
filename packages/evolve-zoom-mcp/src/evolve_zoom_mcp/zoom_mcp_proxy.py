"""Proxy client for Zoom's hosted MCP at ``mcp.zoom.us/mcp/zoom/streamable``.

This module handles the read side of the shim. It speaks streamable-HTTP
MCP directly (JSON-RPC POSTs with the appropriate ``Accept`` headers) —
we deliberately don't pull in the MCP SDK's streamable-HTTP client to keep
the dependency surface narrow and the wire behavior auditable.

The flow on each ``tools/list`` or ``tools/call``:

1. Get a fresh Zoom user-OAuth access token (cached or refreshed).
2. Send a JSON-RPC POST to the configured Zoom MCP base URL.
3. If we get a 401, refresh the access token and retry once.
4. Return the parsed JSON-RPC ``result`` (or raise on JSON-RPC ``error``).

The remote server's ``id`` field in our JSON-RPC requests is a counter,
not the same id our parent MCP server received from OC — this is
deliberate, the shim is opaque about parent IDs.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any, Optional

import httpx

from .zoom_oauth import OAuthConfig, get_access_token


DEFAULT_ZOOM_MCP_BASE_URL = "https://mcp.zoom.us/mcp/zoom/streamable"
MCP_PROTOCOL_VERSION = "2025-03-26"


class ZoomMcpError(Exception):
    """Raised when the remote MCP returns a JSON-RPC error or HTTP failure."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int | str] = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class ZoomMcpProxy:
    """Read-side proxy. Wraps the hosted MCP with OAuth-aware retries."""

    def __init__(
        self,
        oauth_config: OAuthConfig,
        *,
        base_url: str = DEFAULT_ZOOM_MCP_BASE_URL,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._oauth_config = oauth_config
        self._base_url = base_url
        self._client = client or httpx.Client(timeout=30.0)
        self._owned_client = client is None
        self._id_counter = itertools.count(1)
        self._counter_lock = threading.Lock()
        self._initialized = False

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def list_tools(self) -> list[dict]:
        """Return the remote MCP's tool list as a list of dicts.

        Tools come back in Zoom's native shape (name, description, inputSchema,
        outputSchema, annotations); we pass them through verbatim so the
        upstream surface and the OC-facing surface stay aligned.
        """
        result = self._call("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Invoke a tool on the remote MCP and return the result dict."""
        return self._call("tools/call", {"name": name, "arguments": arguments})

    def _ensure_initialized(self) -> None:
        """Send MCP ``initialize`` once per proxy lifetime.

        Zoom's MCP gateway accepts subsequent calls without explicit
        re-initialization within the same session; we still send one
        initialize so it knows what protocol version we're speaking.
        """
        if self._initialized:
            return
        self._call_inner(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "evolve-zoom-mcp", "version": "0.1.0"},
            },
            refresh_on_401=True,
        )
        self._initialized = True

    def _call(self, method: str, params: dict) -> dict:
        """Initialize-then-invoke wrapper."""
        self._ensure_initialized()
        return self._call_inner(method, params, refresh_on_401=True)

    def _call_inner(self, method: str, params: dict, *, refresh_on_401: bool) -> dict:
        """Single JSON-RPC POST with optional 401-then-refresh retry."""
        access_token = get_access_token(self._oauth_config, client=self._client)
        resp = self._post(method, params, access_token)
        if resp.status_code == 401 and refresh_on_401:
            # Force-refresh by invalidating cache via direct credential rotation,
            # then retry once. get_access_token reads from disk, so we just
            # call it again after explicitly refreshing in-process.
            from .credentials import load_credentials
            from .zoom_oauth import refresh_access_token

            creds = load_credentials(self._oauth_config.credentials_dir)
            if creds is None:
                raise ZoomMcpError("401 from Zoom MCP and no stored credentials")
            refreshed = refresh_access_token(self._oauth_config, creds, client=self._client)
            assert refreshed.access_token is not None
            resp = self._post(method, params, refreshed.access_token)
        return self._parse_jsonrpc(resp, method)

    def _post(self, method: str, params: dict, access_token: str) -> httpx.Response:
        with self._counter_lock:
            req_id = next(self._id_counter)
        return self._client.post(
            self._base_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            },
        )

    @staticmethod
    def _parse_jsonrpc(resp: httpx.Response, method: str) -> dict:
        """Validate the JSON-RPC envelope; return ``result`` or raise."""
        if resp.status_code >= 400 and resp.status_code != 401:
            raise ZoomMcpError(
                f"HTTP {resp.status_code} from Zoom MCP on {method}",
                code=f"http_{resp.status_code}",
                data=resp.text[:500],
            )
        try:
            body = resp.json()
        except ValueError:
            raise ZoomMcpError(
                f"non-JSON response from Zoom MCP on {method} "
                f"(HTTP {resp.status_code}); first 500 chars: {resp.text[:500]!r}",
                code="invalid_response",
            )
        if "error" in body:
            err = body["error"] or {}
            raise ZoomMcpError(
                err.get("message") or "Zoom MCP returned an error",
                code=err.get("code"),
                data=err.get("data"),
            )
        result = body.get("result")
        if not isinstance(result, dict):
            raise ZoomMcpError(
                f"missing or non-object 'result' in JSON-RPC response to {method}",
                code="malformed_response",
            )
        return result
