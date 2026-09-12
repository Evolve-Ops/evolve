"""
evolve_admin.mcp_bridge.server — MCP HTTP server (SSE transport).

Uses the official `mcp` Python SDK with a Starlette/uvicorn HTTP server.
Each SSE connection is one Claude Desktop session; active_bot is tracked
per-connection via a ContextVar.

Claude Desktop config (paste into claude_desktop_config.json):
  { "mcpServers": { "evolve-pod": { "url": "http://<host>:5051/sse" } } }
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .audit import AuditLog
from .auth import Auth, AuthError
from .registry import BotRegistry
from .tools import (
    TOOL_HANDLERS,
    get_session_active_bot,
    set_session_active_bot,
)

log = logging.getLogger(__name__)

# ── MCP tool schema definitions ───────────────────────────────────────────────

_TOOLS = [
    Tool(
        name="list_bots",
        description="List all bots registered in this Evolve pod with their status.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_active_bot",
        description="Get the bot that is the current write target for this session.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="set_active_bot",
        description=(
            "Change the active bot for this session. Writes (add_note, update_task, "
            "create_handoff, add_to_memory) target the active bot. Does not persist "
            "across sessions."
        ),
        inputSchema={
            "type": "object",
            "properties": {"bot": {"type": "string", "description": "Bot name (e.g. 'admin_bot', 'team_bot_a')"}},
            "required": ["bot"],
        },
    ),
    Tool(
        name="get_pod_status",
        description="Fleet-wide status: which bots are reachable and their recent activity.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_context",
        description=(
            "Load full context for a bot: memory summary, today's notes, tasks, "
            "pending handoffs. Call this at session start to get situational awareness. "
            "Omit 'bot' to use the active bot."
        ),
        inputSchema={
            "type": "object",
            "properties": {"bot": {"type": "string", "description": "Bot name (optional)"}},
        },
    ),
    Tool(
        name="get_memory",
        description="Read a memory file from a bot's workspace (default: MEMORY.md).",
        inputSchema={
            "type": "object",
            "properties": {
                "bot": {"type": "string"},
                "file": {"type": "string", "description": "Relative path, e.g. 'MEMORY.md' or 'memory/projects.md'"},
            },
        },
    ),
    Tool(
        name="get_tasks",
        description="Read the task list for a bot (markdown format).",
        inputSchema={
            "type": "object",
            "properties": {"bot": {"type": "string"}},
        },
    ),
    Tool(
        name="get_proposals",
        description="Read pending Evolve proposals for a bot.",
        inputSchema={
            "type": "object",
            "properties": {"bot": {"type": "string"}},
        },
    ),
    Tool(
        name="get_daily_notes",
        description="Read a bot's daily log for a specific date (default: today).",
        inputSchema={
            "type": "object",
            "properties": {
                "bot": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD (default: today)"},
            },
        },
    ),
    Tool(
        name="get_evolve_metrics",
        description="Read session and cost metrics for a bot.",
        inputSchema={
            "type": "object",
            "properties": {
                "bot": {"type": "string"},
                "days": {"type": "integer", "description": "Number of days (default: 7)"},
            },
        },
    ),
    Tool(
        name="list_workspace_files",
        description="List files in a bot's workspace directory.",
        inputSchema={
            "type": "object",
            "properties": {
                "bot": {"type": "string"},
                "path": {"type": "string", "description": "Subdirectory path (default: workspace root)"},
            },
        },
    ),
    Tool(
        name="read_workspace_file",
        description="Read any file within a bot's workspace (sandboxed to workspace root).",
        inputSchema={
            "type": "object",
            "properties": {
                "bot": {"type": "string"},
                "path": {"type": "string", "description": "Relative path within workspace"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="add_note",
        description=(
            "Append a timestamped note to the active bot's daily log. "
            "Use for decisions, observations, or context you want the bot to have."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Note content"},
                "bot": {"type": "string"},
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="update_task",
        description="Add or update a task in the active bot's task list.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                    "description": "Default: pending",
                },
                "notes": {"type": "string", "description": "Optional notes"},
                "bot": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="add_to_memory",
        description=(
            "Append a section to the active bot's MEMORY.md. "
            "Use for significant decisions or context that should persist across sessions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "Section heading (optional)"},
                "content": {"type": "string"},
                "bot": {"type": "string"},
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="create_handoff",
        description=(
            "Create a structured handoff file for the bot to act on at its next heartbeat. "
            "Use when you want the OpenClaw bot to continue or follow up on something "
            "after this session ends."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "context": {"type": "string", "description": "Background and current situation"},
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Decisions already made",
                },
                "next_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Actions for the bot to take",
                },
                "bot": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="write_workspace_file",
        description="Write a file to a bot's workspace (sandboxed). Won't overwrite unless overwrite=true.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean", "description": "Default: false"},
                "bot": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    ),
    # ── Google services (path-C: service-account + DwD) ──────────────────────
    # The handlers live in google_tools.py; declared here so MCP list_tools
    # advertises them to bots (previously the three write tools were in
    # TOOL_HANDLERS but missing from _TOOLS, so bots couldn't discover them).
    Tool(
        name="gmail_send",
        description="Send email from the bot's Workspace mailbox, signed as the bot's correspondence persona.",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Recipient address(es)"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text message body. Persona signature is appended automatically."},
                "cc": {"type": ["string", "array"], "items": {"type": "string"}},
                "bcc": {"type": ["string", "array"], "items": {"type": "string"}},
                "bot": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    ),
    Tool(
        name="gmail_list_messages",
        description="List messages in the bot's Gmail mailbox. Returns id/from/subject/date/snippet for each.",
        inputSchema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Gmail search query, e.g. 'from:alice newer_than:7d'"},
                "label_ids": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Gmail label IDs to filter on, e.g. ['INBOX','UNREAD']"},
                "max_results": {"type": "integer", "description": "Default 25, max 500"},
                "bot": {"type": "string"},
            },
        },
    ),
    Tool(
        name="gmail_get_message",
        description="Fetch one Gmail message with full headers and decoded text body.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Gmail message id from gmail_list_messages"},
                "bot": {"type": "string"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="calendar_create_event",
        description="Create a Calendar event on the bot's calendar or one it has edit access to.",
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "ISO 8601 with timezone, e.g. 2026-06-15T18:00:00-07:00"},
                "end": {"type": "string", "description": "ISO 8601 with timezone"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "calendar_id": {"type": "string", "description": "Default 'primary'"},
                "attendees": {"type": ["string", "array"], "items": {"type": "string"}},
                "bot": {"type": "string"},
            },
            "required": ["summary", "start", "end"],
        },
    ),
    Tool(
        name="calendar_list_events",
        description="List events on a calendar within a time window. Defaults to upcoming events from now.",
        inputSchema={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Default 'primary'"},
                "time_min": {"type": "string", "description": "ISO 8601 with timezone; defaults to now"},
                "time_max": {"type": "string", "description": "ISO 8601 with timezone; no upper bound by default"},
                "q": {"type": "string", "description": "Free-text search across summary/description/location"},
                "max_results": {"type": "integer", "description": "Default 25, max 2500"},
                "bot": {"type": "string"},
            },
        },
    ),
    Tool(
        name="drive_write_file",
        description="Create a file in the bot's Drive (drive.file scope: bot-owned or shared-with-bot folders).",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename"},
                "content": {"type": "string", "description": "File body (text)"},
                "mime_type": {"type": "string", "description": "Default 'text/plain'"},
                "parent_folder_id": {"type": "string", "description": "Optional Drive folder ID"},
                "bot": {"type": "string"},
            },
            "required": ["name", "content"],
        },
    ),
    Tool(
        name="drive_list_files",
        description="List Drive files visible to the bot (drive.file scope: only bot-owned or shared-with-bot files).",
        inputSchema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Drive query, e.g. \"mimeType='application/pdf'\" or \"'<folder_id>' in parents\""},
                "page_size": {"type": "integer", "description": "Default 25, max 1000"},
                "order_by": {"type": "string", "description": "e.g. 'modifiedTime desc'"},
                "bot": {"type": "string"},
            },
        },
    ),
    Tool(
        name="drive_read_file",
        description="Fetch a Drive file's contents. Text MIME types are decoded; binary returned as base64. Google-native Docs/Sheets/Slides not yet supported (need separate scopes).",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Drive file id from drive_list_files"},
                "max_bytes": {"type": "integer", "description": "Default 1 MiB; max 10 MiB"},
                "bot": {"type": "string"},
            },
            "required": ["id"],
        },
    ),
    # ── Gmail modify (needs gmail.modify scope; high_privilege, off by default) ──
    Tool(
        name="gmail_list_labels",
        description="List Gmail labels (system + user) for the bot's mailbox. Use this to discover label IDs before calling gmail_label_message.",
        inputSchema={
            "type": "object",
            "properties": {
                "bot": {"type": "string"},
            },
        },
    ),
    Tool(
        name="gmail_label_message",
        description="Add and/or remove labels on a Gmail message. Needs the gmail.modify scope. Pass at least one of add_label_ids / remove_label_ids.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Gmail message id from gmail_list_messages"},
                "add_label_ids": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Label IDs to add (e.g. ['STARRED'] or a user label id from gmail_list_labels)"},
                "remove_label_ids": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Label IDs to remove"},
                "bot": {"type": "string"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="gmail_archive_message",
        description="Archive a Gmail message (remove INBOX label). Needs the gmail.modify scope.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Gmail message id from gmail_list_messages"},
                "bot": {"type": "string"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="gmail_delete_message",
        description="Permanently delete a Gmail message (NOT Trash — unrecoverable). Needs the gmail.modify scope. Requires confirm=true. For a recoverable delete, use gmail_trash_message.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Gmail message id from gmail_list_messages"},
                "confirm": {"type": "boolean", "description": "Must be true — guard rail against accidental permanent deletes"},
                "bot": {"type": "string"},
            },
            "required": ["id", "confirm"],
        },
    ),
    Tool(
        name="gmail_trash_message",
        description="Move a Gmail message to Trash — recoverable (~30 days, then purged). Needs the gmail.modify scope. Requires confirm=true. gmail_label_message will not apply the TRASH label; this is the recoverable-delete path.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Gmail message id from gmail_list_messages"},
                "confirm": {"type": "boolean", "description": "Must be true — guard rail against accidental trashing"},
                "bot": {"type": "string"},
            },
            "required": ["id", "confirm"],
        },
    ),
    Tool(
        name="gmail_mark_read",
        description="Mark a Gmail message read (remove UNREAD label). Needs the gmail.modify scope.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Gmail message id from gmail_list_messages"},
                "bot": {"type": "string"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="gmail_mark_unread",
        description="Mark a Gmail message unread (add UNREAD label). Needs the gmail.modify scope.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Gmail message id from gmail_list_messages"},
                "bot": {"type": "string"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="drive_search",
        description="Search across ALL Drive files visible to the bot's subject (not just bot-owned/shared). Needs the drive.readonly scope. Distinct from drive_list_files which is limited to drive.file's bot-owned/shared slice.",
        inputSchema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Drive query, e.g. \"name contains 'invoice' and mimeType='application/pdf'\". Strongly recommended — without a query this returns the most recently modified files in the entire visible Drive."},
                "page_size": {"type": "integer", "description": "Default 25, max 1000"},
                "order_by": {"type": "string", "description": "e.g. 'modifiedTime desc'"},
                "bot": {"type": "string"},
            },
        },
    ),
    Tool(
        name="get_connection_log",
        description="Show recent MCP connections and tool calls to this bridge.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries (default: 50)"}
            },
        },
    ),
    Tool(
        name="get_write_log",
        description="Show recent write operations performed via this bridge.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries (default: 50)"}
            },
        },
    ),
]


# ── Server factory ────────────────────────────────────────────────────────────

def build_starlette_app(
    registry: BotRegistry,
    audit: AuditLog,
    auth: Auth,
    *,
    port: int = 5051,
) -> Starlette:
    """
    Build and return the Starlette ASGI app that serves the MCP bridge.
    Register this with uvicorn to start the server.
    """

    mcp_server = Server("evolve-pod")

    @mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        return _TOOLS

    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        t0 = time.monotonic()
        session_id = str(uuid.uuid4())[:8]
        # client_ip injected via ContextVar by the auth middleware below
        client_ip = _request_ip.get("unknown")
        bot_arg = arguments.get("bot")

        handler_entry = TOOL_HANDLERS.get(name)
        if handler_entry is None:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

        handler, is_write = handler_entry
        result_str = ""
        error = ""
        try:
            # Audit tools get the audit log; all others get registry
            if name in ("get_connection_log", "get_write_log"):
                result = await handler(arguments, audit)
            else:
                result = await handler(arguments, registry)
            result_str = json.dumps(result, ensure_ascii=False)
        except (ValueError, Exception) as e:
            error = str(e)
            result_str = json.dumps({"error": error})
            log.warning("Tool %s failed: %s", name, error)

        duration_ms = int((time.monotonic() - t0) * 1000)
        resolved_bot = get_session_active_bot() or registry.primary_id

        # Input summary for audit (truncate to avoid huge logs)
        input_summary = ", ".join(
            f"{k}={str(v)[:40]!r}" for k, v in arguments.items()
        )[:120]

        audit.log(
            session_id=session_id,
            client_ip=client_ip,
            tool=name,
            bot=bot_arg or resolved_bot,
            input_summary=input_summary,
            result="ok" if not error else f"error: {error[:80]}",
            duration_ms=duration_ms,
            is_write=is_write,
        )

        return [TextContent(type="text", text=result_str)]

    # ── SSE transport + auth middleware ───────────────────────────────────────

    sse_transport = SseServerTransport("/messages")

    # ContextVar to pass client IP into tool calls (set per-request)
    _request_ip: ContextVar = ContextVar("request_ip", default="unknown")

    async def handle_sse(request: Request):
        # Extract client IP
        client_ip = request.client.host if request.client else "unknown"
        auth_header = request.headers.get("authorization")

        try:
            auth.check(client_ip, auth_header)
        except AuthError as e:
            log.warning("Auth rejected %s: %s", client_ip, e)
            return Response(str(e), status_code=403)

        # Set active bot to primary for new sessions; it may be changed via set_active_bot
        set_session_active_bot(registry.primary_id)
        token = _request_ip.set(client_ip)
        try:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await mcp_server.run(
                    streams[0],
                    streams[1],
                    mcp_server.create_initialization_options(),
                )
        finally:
            _request_ip.reset(token)

        return Response()

    # ── Health endpoint (used by Evolve admin to check bridge status) ─────────

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "service": "evolve-mcp-bridge",
            "port": port,
            "bots": registry.bot_ids(),
            "primary_bot": registry.primary_id,
        })

    # Shutdown hook via the lifespan context manager. Starlette removed the
    # ``on_startup=``/``on_shutdown=`` kwargs in favour of ``lifespan=`` — passing
    # the old kwargs raises ``TypeError: Starlette.__init__() got an unexpected
    # keyword argument 'on_shutdown'`` at construction, which crashed the bridge
    # post-fork before it could bind :5051 (W10-G round-6). ``lifespan`` yields
    # once between startup and shutdown; the code after the yield runs on
    # teardown. registry.stop() is sync (it just sets a threading.Event), so no
    # await is needed.
    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette):
        yield
        registry.stop()

    return Starlette(
        routes=[
            Route("/health", endpoint=health),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages", app=sse_transport.handle_post_message),
        ],
        lifespan=_lifespan,
    )
