"""Tests for zoom_mcp_proxy.py."""

from __future__ import annotations

import httpx
import pytest

from evolve_zoom_mcp.zoom_mcp_proxy import (
    DEFAULT_ZOOM_MCP_BASE_URL,
    ZoomMcpError,
    ZoomMcpProxy,
)


def _mock_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def _ok(result: dict, req_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


class TestListTools:
    def test_lists_tools_with_valid_token(
        self, oauth_config, saved_creds
    ) -> None:
        request_methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read()
            payload = httpx._content.json.loads(body) if False else __import__("json").loads(body)
            request_methods.append(payload["method"])
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok({"protocolVersion": "x"}, payload["id"]))
            if payload["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json=_ok({"tools": [{"name": "search_meetings"}]}, payload["id"]),
                )
            return httpx.Response(500)

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        tools = proxy.list_tools()
        assert tools == [{"name": "search_meetings"}]
        assert request_methods == ["initialize", "tools/list"]

    def test_call_tool_passes_arguments(self, oauth_config, saved_creds) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = __import__("json").loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok({}, payload["id"]))
            captured.update(payload)
            return httpx.Response(
                200,
                json=_ok(
                    {"content": [{"type": "text", "text": "found 2 meetings"}]},
                    payload["id"],
                ),
            )

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        result = proxy.call_tool("search_meetings", {"q": "kickoff"})
        assert captured["params"] == {"name": "search_meetings", "arguments": {"q": "kickoff"}}
        assert result["content"][0]["text"] == "found 2 meetings"

    def test_initialize_sent_only_once_across_calls(
        self, oauth_config, saved_creds
    ) -> None:
        init_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = __import__("json").loads(request.read())
            if payload["method"] == "initialize":
                init_count["n"] += 1
                return httpx.Response(200, json=_ok({}, payload["id"]))
            return httpx.Response(200, json=_ok({"tools": []}, payload["id"]))

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        proxy.list_tools()
        proxy.list_tools()
        proxy.list_tools()
        assert init_count["n"] == 1


class Test401Retry:
    def test_refreshes_and_retries_once_on_401(self, oauth_config, saved_creds) -> None:
        """First call returns 401 → shim refreshes the token → second call succeeds."""
        json_lib = __import__("json")
        ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"

        # Track call order so we can branch behavior.
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == ZOOM_TOKEN_URL:
                calls.append("token_refresh")
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at_refreshed",
                        "refresh_token": "rt_v2",
                        "expires_in": 3600,
                    },
                )
            if url.startswith(DEFAULT_ZOOM_MCP_BASE_URL):
                payload = json_lib.loads(request.read())
                method = payload["method"]
                if method == "initialize":
                    calls.append("initialize")
                    return httpx.Response(200, json=_ok({}, payload["id"]))
                calls.append(f"{method}:auth={request.headers.get('Authorization', '')[-12:]}")
                # First tools/list returns 401; second succeeds.
                attempts = [c for c in calls if c.startswith("tools/list")]
                if len(attempts) == 1:
                    return httpx.Response(401, json={"error": "expired"})
                return httpx.Response(
                    200, json=_ok({"tools": [{"name": "after_retry"}]}, payload["id"])
                )
            return httpx.Response(404)

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        tools = proxy.list_tools()
        assert tools == [{"name": "after_retry"}]
        # Refresh exchange happened between the two tools/list attempts.
        assert "token_refresh" in calls
        assert calls.count("token_refresh") == 1


class TestErrorPaths:
    def test_jsonrpc_error_envelope_raises(self, oauth_config, saved_creds) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = __import__("json").loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok({}, payload["id"]))
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        with pytest.raises(ZoomMcpError) as exc_info:
            proxy.list_tools()
        assert "Method not found" in str(exc_info.value)
        assert exc_info.value.code == -32601

    def test_http_5xx_raises(self, oauth_config, saved_creds) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = __import__("json").loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok({}, payload["id"]))
            return httpx.Response(503, text="upstream down")

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        with pytest.raises(ZoomMcpError) as exc_info:
            proxy.list_tools()
        assert exc_info.value.code == "http_503"

    def test_non_json_response_raises(self, oauth_config, saved_creds) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = __import__("json").loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok({}, payload["id"]))
            return httpx.Response(200, text="<html>not mcp</html>")

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        with pytest.raises(ZoomMcpError) as exc_info:
            proxy.list_tools()
        assert exc_info.value.code == "invalid_response"

    def test_no_credentials_raises_clean_error(self, oauth_config) -> None:
        # No saved_creds fixture used → credentials.json doesn't exist.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)  # should never be hit

        client = httpx.Client(transport=_mock_transport(handler))
        proxy = ZoomMcpProxy(oauth_config, client=client)
        # The first thing list_tools does is ensure_initialized, which calls
        # get_access_token, which raises OAuthError with code="not_configured".
        with pytest.raises(Exception) as exc_info:
            proxy.list_tools()
        # Don't over-specify the exception type — could be OAuthError; just
        # confirm the message mentions configuration.
        assert "not configured" in str(exc_info.value).lower() or \
               "no credentials" in str(exc_info.value).lower()
