"""MCP server entry point — unified read-proxy + write tools.

Spawned over stdio by OC when it sees ``mcp.servers.zoom`` in the bot's
``openclaw.json``. Responds to standard MCP requests (``initialize``,
``tools/list``, ``tools/call``) and presents a unified tool surface that
merges:

- Tools proxied from Zoom's hosted MCP at ``mcp.zoom.us/mcp/zoom/streamable``
  (passed through verbatim from the upstream ``tools/list``).
- Tools implemented locally in this package — ``create_meeting``,
  ``update_meeting``, ``delete_meeting``. These are appended IFF S2S env
  vars are present; otherwise they're silently absent from ``tools/list``
  and the bot can't call them.

Failure modes:

- Upstream ``tools/list`` errors → we log and surface an MCP error response
  for tools/list, but the server stays up so the operator's status check
  can still distinguish "shim is alive" from "shim is dead."
- Per-tool errors (Zoom returns 4xx/5xx, OAuth expired, etc.) → returned
  as MCP error results; the server-loop keeps running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent
from mcp.types import Tool as McpTool

from .zoom_mcp_proxy import DEFAULT_ZOOM_MCP_BASE_URL, ZoomMcpError, ZoomMcpProxy
from .zoom_oauth import OAuthConfig
from .zoom_s2s import S2sConfig, S2sError, ZoomS2sSession
from .zoom_write import (
    create_meeting,
    delete_meeting,
    update_meeting,
    write_tool_definitions,
)


_SERVER_NAME = "evolve-zoom-mcp"
_LOG = logging.getLogger(_SERVER_NAME)

# Tool name → handler. Populated at server construction; the write-tool
# entries are only added when S2S is configured.
WRITE_TOOL_NAMES = frozenset({"create_meeting", "update_meeting", "delete_meeting"})


@dataclass
class RuntimeContext:
    """Resolved-from-env state needed by the server loop."""

    proxy: ZoomMcpProxy
    s2s: Optional[ZoomS2sSession]
    write_tools_enabled: bool


def build_server(context: RuntimeContext) -> Server:
    """Construct the MCP Server instance with handlers wired up."""
    server: Server = Server(_SERVER_NAME)

    @server.list_tools()  # type: ignore[no-untyped-call]
    async def _list_tools() -> list[McpTool]:
        tools: list[McpTool] = []
        # Read tools — proxy fetches Zoom's tools/list and we relay verbatim.
        try:
            remote_tools = await asyncio.to_thread(context.proxy.list_tools)
        except ZoomMcpError as exc:
            _LOG.warning("zoom MCP tools/list failed: %s", exc)
            remote_tools = []
        for t in remote_tools:
            tools.append(_dict_to_mcp_tool(t))
        # Write tools — only if S2S is configured.
        if context.write_tools_enabled:
            for t in write_tool_definitions():
                tools.append(_dict_to_mcp_tool(t))
        return tools

    @server.call_tool()  # type: ignore[no-untyped-call]
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name in WRITE_TOOL_NAMES:
            return await _call_write_tool(context, name, arguments)
        return await _call_proxied_tool(context, name, arguments)

    return server


async def _call_write_tool(
    context: RuntimeContext, name: str, arguments: dict
) -> list[TextContent]:
    """Dispatch a write tool. Always returns a single TextContent."""
    if not context.write_tools_enabled or context.s2s is None:
        return _err(
            f"Write tools are not enabled — set ZOOM_S2S_CLIENT_ID, "
            f"ZOOM_S2S_CLIENT_SECRET, and ZOOM_S2S_ACCOUNT_ID."
        )
    try:
        if name == "create_meeting":
            result = await asyncio.to_thread(
                create_meeting,
                context.s2s,
                topic=arguments["topic"],
                start_time=arguments.get("start_time"),
                duration_minutes=int(arguments.get("duration_minutes") or 60),
                host_email=arguments.get("host_email"),
                agenda=arguments.get("agenda"),
                include_start_url=bool(arguments.get("include_start_url") or False),
            )
        elif name == "update_meeting":
            result = await asyncio.to_thread(
                update_meeting,
                context.s2s,
                meeting_id=int(arguments["meeting_id"]),
                topic=arguments.get("topic"),
                start_time=arguments.get("start_time"),
                duration_minutes=arguments.get("duration_minutes"),
                agenda=arguments.get("agenda"),
            )
        elif name == "delete_meeting":
            result = await asyncio.to_thread(
                delete_meeting,
                context.s2s,
                meeting_id=int(arguments["meeting_id"]),
            )
        else:
            return _err(f"Unknown write tool: {name}")
    except (S2sError, KeyError, ValueError) as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def _call_proxied_tool(
    context: RuntimeContext, name: str, arguments: dict
) -> list[TextContent]:
    """Pass through to Zoom's hosted MCP. Returns a single TextContent."""
    try:
        result = await asyncio.to_thread(context.proxy.call_tool, name, arguments)
    except ZoomMcpError as exc:
        return _err(f"ZoomMcpError on {name}: {exc} (code={exc.code})")
    # Pass through whatever the upstream returned. MCP tools/call result is a
    # dict with a 'content' list; we forward that as our text content.
    content = result.get("content")
    if isinstance(content, list) and content:
        items: list[TextContent] = []
        for item in content:
            text = item.get("text") if isinstance(item, dict) else None
            if text is not None:
                items.append(TextContent(type="text", text=str(text)))
        if items:
            return items
    # Fallback: JSON-encode the whole result.
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _err(message: str) -> list[TextContent]:
    """Return a single TextContent with a `[error] <message>` payload.

    Conventional shape — OC and downstream models recognize the [error]
    prefix as a tool-call failure without us raising back to MCP (which
    would tear down the session).
    """
    return [TextContent(type="text", text=f"[error] {message}")]


def _dict_to_mcp_tool(t: dict) -> McpTool:
    """Convert a tool-definition dict (ours or Zoom's) into an MCP Tool."""
    return McpTool(
        name=str(t.get("name")),
        description=str(t.get("description") or ""),
        inputSchema=t.get("inputSchema") or {"type": "object", "properties": {}},
    )


def runtime_from_env(env: Optional[dict[str, str]] = None) -> RuntimeContext:
    """Resolve env into a RuntimeContext.

    Raises KeyError if required env vars (user-OAuth) are missing. S2S env
    is optional — its absence flips ``write_tools_enabled`` to False.
    """
    env = dict(env if env is not None else os.environ)
    oauth_config = OAuthConfig.from_env(env)
    base_url = env.get("ZOOM_MCP_BASE_URL") or DEFAULT_ZOOM_MCP_BASE_URL
    proxy = ZoomMcpProxy(oauth_config, base_url=base_url)
    s2s_config = S2sConfig.from_env(env)
    s2s = ZoomS2sSession(s2s_config) if s2s_config else None
    return RuntimeContext(
        proxy=proxy,
        s2s=s2s,
        write_tools_enabled=s2s is not None,
    )


async def run() -> None:
    """Spawn the MCP server on stdio and run until the parent disconnects."""
    logging.basicConfig(level=logging.WARNING, stream=os.sys.stderr)
    context = runtime_from_env()
    server = build_server(context)
    init_options = server.create_initialization_options()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    finally:
        context.proxy.close()
        if context.s2s is not None:
            context.s2s.close()
