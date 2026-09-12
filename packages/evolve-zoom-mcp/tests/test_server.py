"""Tests for server.py — the MCP server wiring + tool dispatch.

We exercise the handlers directly rather than spinning up a stdio MCP
session in-process; the handlers are async functions registered on the
Server object, and we can invoke them through the public test surface.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from evolve_zoom_mcp.server import (
    RuntimeContext,
    WRITE_TOOL_NAMES,
    build_server,
)
from evolve_zoom_mcp.zoom_mcp_proxy import ZoomMcpError, ZoomMcpProxy
from evolve_zoom_mcp.zoom_oauth import OAuthConfig
from evolve_zoom_mcp.zoom_s2s import S2sConfig, ZoomS2sSession


def _mock_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def _make_proxy(oauth_config: OAuthConfig, handler) -> ZoomMcpProxy:  # type: ignore[no-untyped-def]
    return ZoomMcpProxy(oauth_config, client=httpx.Client(transport=_mock_transport(handler)))


def _make_s2s(s2s_config: S2sConfig, handler) -> ZoomS2sSession:  # type: ignore[no-untyped-def]
    return ZoomS2sSession(s2s_config, client=httpx.Client(transport=_mock_transport(handler)))


def _ok_jsonrpc(result: dict, req_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _get_handler(server, name: str):  # type: ignore[no-untyped-def]
    """Pull a registered handler from the Server by request method name.

    The mcp.server.Server stores handlers in request_handlers keyed by the
    MCP request type. This indirection keeps the test agnostic to internal
    attribute naming changes.
    """
    # Inspect via request_handlers, which is a dict keyed by request class.
    handlers = getattr(server, "request_handlers")
    # Match by class name's first chunk (ListToolsRequest, CallToolRequest).
    for request_class, handler in handlers.items():
        if name in request_class.__name__:
            return handler
    raise KeyError(f"no handler for {name} in {list(handlers)!r}")


# ---------- list_tools merging ----------


class TestListToolsMerge:
    def test_remote_tools_only_when_no_s2s(
        self, oauth_config: OAuthConfig, saved_creds
    ) -> None:
        def remote(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok_jsonrpc({}, payload["id"]))
            return httpx.Response(
                200,
                json=_ok_jsonrpc(
                    {
                        "tools": [
                            {"name": "search_meetings", "description": "search"},
                            {"name": "recordings_list", "description": "list"},
                        ]
                    },
                    payload["id"],
                ),
            )

        ctx = RuntimeContext(
            proxy=_make_proxy(oauth_config, remote),
            s2s=None,
            write_tools_enabled=False,
        )
        server = build_server(ctx)
        # _list_tools is registered; call it via the mcp.server internal dispatch.
        # We just call the closure directly by re-importing via the registered handler.
        # Easier: rebuild via a tiny helper that exercises the same closure.
        tool_names = asyncio.run(_call_list_tools(server))
        assert tool_names == ["search_meetings", "recordings_list"]

    def test_remote_plus_write_tools_when_s2s_present(
        self, oauth_config: OAuthConfig, s2s_config: S2sConfig, saved_creds
    ) -> None:
        def remote(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok_jsonrpc({}, payload["id"]))
            return httpx.Response(
                200,
                json=_ok_jsonrpc(
                    {"tools": [{"name": "search_meetings", "description": "search"}]},
                    payload["id"],
                ),
            )

        def s2s(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600})

        ctx = RuntimeContext(
            proxy=_make_proxy(oauth_config, remote),
            s2s=_make_s2s(s2s_config, s2s),
            write_tools_enabled=True,
        )
        server = build_server(ctx)
        tool_names = asyncio.run(_call_list_tools(server))
        assert "search_meetings" in tool_names
        # All write tools present.
        for w in WRITE_TOOL_NAMES:
            assert w in tool_names

    def test_remote_failure_yields_empty_remote_list_but_keeps_writes(
        self, oauth_config: OAuthConfig, s2s_config: S2sConfig, saved_creds
    ) -> None:
        def remote(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream down")

        def s2s(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600})

        ctx = RuntimeContext(
            proxy=_make_proxy(oauth_config, remote),
            s2s=_make_s2s(s2s_config, s2s),
            write_tools_enabled=True,
        )
        server = build_server(ctx)
        tool_names = asyncio.run(_call_list_tools(server))
        # Remote failed → only write tools should appear.
        for w in WRITE_TOOL_NAMES:
            assert w in tool_names
        assert all(name in WRITE_TOOL_NAMES for name in tool_names)


# ---------- call_tool dispatch ----------


class TestCallToolDispatch:
    def test_write_tool_blocked_without_s2s(
        self, oauth_config: OAuthConfig, saved_creds
    ) -> None:
        ctx = RuntimeContext(
            proxy=_make_proxy(oauth_config, lambda r: httpx.Response(500)),
            s2s=None,
            write_tools_enabled=False,
        )
        server = build_server(ctx)
        content = asyncio.run(_call_tool(server, "create_meeting", {"topic": "x"}))
        assert len(content) == 1
        text = content[0].text
        assert text.startswith("[error]")
        assert "not enabled" in text.lower()

    def test_proxied_tool_passes_text_through(
        self, oauth_config: OAuthConfig, saved_creds
    ) -> None:
        def remote(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok_jsonrpc({}, payload["id"]))
            return httpx.Response(
                200,
                json=_ok_jsonrpc(
                    {
                        "content": [
                            {"type": "text", "text": "found 7 meetings"},
                            {"type": "text", "text": "extra metadata"},
                        ]
                    },
                    payload["id"],
                ),
            )

        ctx = RuntimeContext(
            proxy=_make_proxy(oauth_config, remote),
            s2s=None,
            write_tools_enabled=False,
        )
        server = build_server(ctx)
        content = asyncio.run(_call_tool(server, "search_meetings", {"q": "kickoff"}))
        assert [c.text for c in content] == ["found 7 meetings", "extra metadata"]

    def test_proxied_error_surfaces_as_text(
        self, oauth_config: OAuthConfig, saved_creds
    ) -> None:
        def remote(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read())
            if payload["method"] == "initialize":
                return httpx.Response(200, json=_ok_jsonrpc({}, payload["id"]))
            return httpx.Response(503, text="upstream down")

        ctx = RuntimeContext(
            proxy=_make_proxy(oauth_config, remote),
            s2s=None,
            write_tools_enabled=False,
        )
        server = build_server(ctx)
        content = asyncio.run(_call_tool(server, "search_meetings", {}))
        text = content[0].text
        assert text.startswith("[error]")
        assert "ZoomMcpError" in text


# ---------- helpers ----------


async def _call_list_tools(server) -> list[str]:  # type: ignore[no-untyped-def]
    """Invoke the registered list_tools handler and return tool names."""
    from mcp.types import ListToolsRequest

    handler = _get_handler(server, "ListToolsRequest")
    # The mcp server wraps handlers; the wrapper expects a ListToolsRequest
    # instance and returns a ServerResult.
    result = await handler(ListToolsRequest(method="tools/list"))
    return [t.name for t in result.root.tools]


async def _call_tool(server, name: str, arguments: dict):  # type: ignore[no-untyped-def]
    """Invoke the registered call_tool handler and return its content list."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = _get_handler(server, "CallToolRequest")
    result = await handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=arguments),
        )
    )
    return result.root.content
