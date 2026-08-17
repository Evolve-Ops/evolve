"""
evolve_admin.mcp_bridge.tools — MCP tool handlers for the Evolve pod bridge.

Tool invocations come through the MCP server and dispatch here. Each handler
is an async function that reads from or writes to a bot's workspace via the
BotWorkspace helper (which uses sudo -u {bot_id} for all file operations).

Session state (active_bot) is maintained per-connection via a ContextVar set
by the server layer before calling any tool.

All write operations are appended to the audit log by the server layer;
handlers themselves just do the work and return a result string.
"""

from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _iso_now

from .registry import BotRegistry
from .workspace import WorkspaceError

# Per-connection active bot. The server sets this when a connection is established
# and before every tool call. Defaults to None (resolved to registry.primary_id).
_active_bot: ContextVar[str | None] = ContextVar("active_bot", default=None)


def get_session_active_bot() -> str | None:
    return _active_bot.get()


def set_session_active_bot(bot_id: str | None) -> None:
    _active_bot.set(bot_id)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve_bot(bot_id: str | None, registry: BotRegistry) -> str:
    """Resolve an optional bot_id to the active or primary bot. Raise if none found."""
    resolved = bot_id or _active_bot.get() or registry.primary_id
    if not resolved:
        raise ValueError("No active bot set and no primary bot configured in network.json")
    if registry.get(resolved) is None:
        raise ValueError(f"Bot {resolved!r} is not registered in this pod")
    return resolved


def _truncate(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[… truncated — {len(text) - max_chars} chars omitted]"


# ── Discovery & session tools ─────────────────────────────────────────────────

async def tool_list_bots(arguments: dict, registry: BotRegistry) -> dict:
    active = _active_bot.get() or registry.primary_id
    bots = []
    for info in registry.list():
        ws_ok = await info.workspace.exists(".")
        bots.append({
            "name": info.bot_id,
            "role": info.role,
            "workspace_readable": ws_ok,
            "active": info.bot_id == active,
            "is_primary": info.is_primary,
        })
    return {
        "bots": bots,
        "active_bot": active,
        "primary_bot": registry.primary_id,
        "total": len(bots),
    }


async def tool_get_active_bot(arguments: dict, registry: BotRegistry) -> dict:
    active = _active_bot.get() or registry.primary_id
    return {
        "active_bot": active,
        "is_primary": active == registry.primary_id,
    }


async def tool_set_active_bot(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = arguments.get("bot", "").strip()
    if not bot_id:
        raise ValueError("bot is required")
    if registry.get(bot_id) is None:
        raise ValueError(f"Bot {bot_id!r} is not registered. Use list_bots to see available bots.")
    _active_bot.set(bot_id)
    return {"ok": True, "active_bot": bot_id}


async def tool_get_pod_status(arguments: dict, registry: BotRegistry) -> dict:
    """
    Returns workspace reachability for each bot.
    Full health metrics (sessions, scores) require Evolve's metrics files;
    we return what we can read directly.
    """
    bots_status: dict[str, Any] = {}
    for info in registry.list():
        ws_readable = await info.workspace.exists(".")
        bots_status[info.bot_id] = {
            "role": info.role,
            "workspace_readable": ws_readable,
        }
        # Try to read last-modified time on MEMORY.md as a proxy for "recent activity"
        if ws_readable:
            try:
                st = await info.workspace.stat(info.workspace.memory_path())
                bots_status[info.bot_id]["memory_last_modified"] = st.get("last_modified")
            except WorkspaceError:
                pass
    return {
        "bots": bots_status,
        "pod_bot_count": len(bots_status),
        "timestamp": _iso_now(),
    }


# ── Read tools ────────────────────────────────────────────────────────────────

async def tool_get_context(arguments: dict, registry: BotRegistry) -> dict:
    """Primary entry point — rich context bundle for one bot."""
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    info = registry.get(bot_id)
    ws = info.workspace

    result: dict[str, Any] = {
        "bot": bot_id,
        "timestamp": _iso_now(),
        "other_bots": [b.bot_id for b in registry.list() if b.bot_id != bot_id],
    }

    # Memory summary
    try:
        memory = await ws.read(ws.memory_path())
        result["memory_summary"] = _truncate(memory, 2000)
        result["memory_chars"] = len(memory)
    except WorkspaceError as e:
        result["memory_summary"] = f"(unavailable: {e})"

    # Today's notes
    try:
        notes = await ws.read(ws.today_note_path())
        result["today_notes"] = notes
    except WorkspaceError:
        result["today_notes"] = "(no notes yet today)"

    # Tasks (raw markdown — Claude parses the format)
    try:
        tasks_raw = await ws.read(ws.tasks_path())
        result["tasks_raw"] = tasks_raw
        # Quick count of incomplete tasks (lines with "- [ ]")
        result["pending_task_count"] = tasks_raw.count("- [ ]")
    except WorkspaceError:
        result["tasks_raw"] = "(no tasks file)"
        result["pending_task_count"] = 0

    # Pending handoffs (files in handoffs/ not yet in completed/)
    try:
        handoff_files = await ws.list_dir("handoffs")
        pending = [f for f in handoff_files if not f.startswith(".")]
        result["pending_handoffs"] = pending
        result["pending_handoff_count"] = len(pending)
    except Exception:
        result["pending_handoffs"] = []
        result["pending_handoff_count"] = 0

    return result


async def tool_get_memory(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    file_path = arguments.get("file", "memory/MEMORY.md")
    # Normalise: if caller passes just "MEMORY.md", resolve to memory/MEMORY.md
    if "/" not in file_path:
        file_path = f"memory/{file_path}"
    ws = registry.get(bot_id).workspace
    try:
        content = await ws.read(file_path)
        st = await ws.stat(file_path)
        return {"bot": bot_id, "file": file_path, "content": content, **st}
    except WorkspaceError as e:
        raise ValueError(str(e)) from e


async def tool_get_tasks(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    ws = registry.get(bot_id).workspace
    try:
        raw = await ws.read(ws.tasks_path())
    except WorkspaceError:
        raw = "(no tasks file found)"
    return {
        "bot": bot_id,
        "tasks_raw": raw,
        "note": "Tasks are in markdown format. Parse '- [ ]' for pending, '- [x]' for done.",
    }


async def tool_get_proposals(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    ws = registry.get(bot_id).workspace
    # Proposals live in memory/proposals/ or as a proposals.md file
    proposals: list[str] = []
    for candidate in ["memory/proposals.md", "proposals.md", "memory/proposals"]:
        try:
            if await ws.exists(candidate):
                content = await ws.read(candidate)
                proposals.append(content)
                break
        except WorkspaceError:
            continue
    # Also check memory/proposals/ directory
    try:
        files = await ws.list_dir("memory/proposals")
        for f in files:
            if f.endswith(".md"):
                proposals.append(await ws.read(f"memory/proposals/{f}"))
    except WorkspaceError:
        pass
    return {
        "bot": bot_id,
        "proposals_raw": "\n\n---\n\n".join(proposals) if proposals else "(no proposals found)",
        "count": len(proposals),
    }


async def tool_get_daily_notes(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    note_date = arguments.get("date", date.today().isoformat())
    # Validate date format
    try:
        datetime.strptime(note_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD, got: {note_date!r}")
    ws = registry.get(bot_id).workspace
    rel_path = f"memory/{note_date}.md"
    exists = await ws.exists(rel_path)
    content = ""
    if exists:
        try:
            content = await ws.read(rel_path)
        except WorkspaceError as e:
            raise ValueError(str(e)) from e
    return {"bot": bot_id, "date": note_date, "exists": exists, "content": content}


async def tool_get_evolve_metrics(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    days = int(arguments.get("days", 7))
    ws = registry.get(bot_id).workspace
    # Try known metrics file locations
    for candidate in ["memory/metrics.md", "metrics.md", ".openclaw/metrics.json"]:
        try:
            if await ws.exists(candidate):
                content = await ws.read(candidate)
                return {"bot": bot_id, "period_days": days, "metrics_raw": content}
        except WorkspaceError:
            continue
    return {
        "bot": bot_id,
        "period_days": days,
        "metrics_raw": "(metrics file not found — Evolve metrics may be stored separately)",
    }


async def tool_list_workspace_files(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    path = arguments.get("path", ".")
    ws = registry.get(bot_id).workspace
    files = await ws.list_dir(path)
    return {"bot": bot_id, "path": path, "files": files, "count": len(files)}


async def tool_read_workspace_file(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    path = arguments.get("path")
    if not path:
        raise ValueError("path is required")
    ws = registry.get(bot_id).workspace
    try:
        content = await ws.read(path)
        st = await ws.stat(path)
        return {"bot": bot_id, "path": path, "content": content, **st}
    except WorkspaceError as e:
        raise ValueError(str(e)) from e


# ── Write tools ───────────────────────────────────────────────────────────────

async def tool_add_note(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    text = (arguments.get("text") or "").strip()
    if not text:
        raise ValueError("text is required and must not be empty")
    ws = registry.get(bot_id).workspace
    note_path = ws.today_note_path()
    timestamp = datetime.utcnow().strftime("%H:%M UTC")
    entry = f"\n### {timestamp}\n{text}\n"
    await ws.append(note_path, entry)
    return {"ok": True, "bot": bot_id, "appended_to": note_path}


async def tool_update_task(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    title = (arguments.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    status = arguments.get("status", "pending")
    valid_statuses = {"pending", "in_progress", "done", "blocked"}
    if status not in valid_statuses:
        raise ValueError(f"status must be one of: {', '.join(sorted(valid_statuses))}")
    notes_text = (arguments.get("notes") or "").strip()

    checkbox = "[x]" if status == "done" else "[ ]"
    status_tag = f" `{status}`" if status not in ("pending", "done") else ""
    entry = f"- {checkbox} {title}{status_tag}"
    if notes_text:
        entry += f"\n  > {notes_text}"
    entry += "\n"

    ws = registry.get(bot_id).workspace
    await ws.append(ws.tasks_path(), entry)
    return {"ok": True, "bot": bot_id, "task": title, "status": status}


async def tool_add_to_memory(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    section = (arguments.get("section") or "").strip()
    content = (arguments.get("content") or "").strip()
    if not content:
        raise ValueError("content is required and must not be empty")

    timestamp = date.today().isoformat()
    if section:
        entry = f"\n## {section} ({timestamp})\n{content}\n"
    else:
        entry = f"\n---\n*Added {timestamp}*\n{content}\n"

    ws = registry.get(bot_id).workspace
    await ws.append(ws.memory_path(), entry)
    return {"ok": True, "bot": bot_id, "appended_to": ws.memory_path()}


async def tool_create_handoff(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    title = (arguments.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    context = (arguments.get("context") or "").strip()
    decisions = arguments.get("decisions") or []
    next_steps = arguments.get("next_steps") or []

    # Sanitise title for use in filename
    safe_title = re.sub(r"[^a-z0-9-]", "-", title.lower())
    safe_title = re.sub(r"-+", "-", safe_title).strip("-")[:50]
    timestamp = datetime.utcnow().strftime("%Y-%m-%d-%H-%M")
    filename = f"handoffs/{timestamp}-{safe_title}.md"

    sections = [f"# Handoff: {title}", f"*Created: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*", ""]
    if context:
        sections += ["## Context", context, ""]
    if decisions:
        sections.append("## Decisions Made")
        sections += [f"- {d}" for d in decisions]
        sections.append("")
    if next_steps:
        sections.append("## Next Steps")
        sections += [f"- {s}" for s in next_steps]
        sections.append("")

    content = "\n".join(sections)
    ws = registry.get(bot_id).workspace
    # Ensure handoffs dir exists
    await ws.write(filename, content, overwrite=False)
    return {"ok": True, "bot": bot_id, "file": filename}


async def tool_write_workspace_file(arguments: dict, registry: BotRegistry) -> dict:
    bot_id = _resolve_bot(arguments.get("bot"), registry)
    path = (arguments.get("path") or "").strip()
    content = arguments.get("content", "")
    overwrite = bool(arguments.get("overwrite", False))
    if not path:
        raise ValueError("path is required")
    ws = registry.get(bot_id).workspace
    try:
        await ws.write(path, content, overwrite=overwrite)
    except WorkspaceError as e:
        raise ValueError(str(e)) from e
    return {"ok": True, "bot": bot_id, "path": path}


# ── Audit tools ───────────────────────────────────────────────────────────────

async def tool_get_connection_log(arguments: dict, audit_log) -> dict:
    limit = min(int(arguments.get("limit", 50)), 500)
    entries = audit_log.tail(limit)
    return {"entries": entries, "count": len(entries)}


async def tool_get_write_log(arguments: dict, audit_log) -> dict:
    limit = min(int(arguments.get("limit", 50)), 500)
    entries = audit_log.tail(limit, writes_only=True)
    return {"entries": entries, "count": len(entries)}


# ── Dispatch table ────────────────────────────────────────────────────────────

# Google-services tools (path-C: service-account + DwD).
# Defined in mcp_bridge/google_tools.py; registered here so the registry
# is the one place to scan for all tools.
from . import google_tools as _gt


#: Maps MCP tool name → (handler, is_write_op)
#: The server uses this to route calls and flag writes in the audit log.
TOOL_HANDLERS: dict[str, tuple[Any, bool]] = {
    # Discovery & session
    "list_bots":           (tool_list_bots,           False),
    "get_active_bot":      (tool_get_active_bot,       False),
    "set_active_bot":      (tool_set_active_bot,       False),
    "get_pod_status":      (tool_get_pod_status,       False),
    # Read
    "get_context":         (tool_get_context,          False),
    "get_memory":          (tool_get_memory,           False),
    "get_tasks":           (tool_get_tasks,            False),
    "get_proposals":       (tool_get_proposals,        False),
    "get_daily_notes":     (tool_get_daily_notes,      False),
    "get_evolve_metrics":  (tool_get_evolve_metrics,   False),
    "list_workspace_files":(tool_list_workspace_files, False),
    "read_workspace_file": (tool_read_workspace_file,  False),
    # Write
    "add_note":            (tool_add_note,             True),
    "update_task":         (tool_update_task,          True),
    "add_to_memory":       (tool_add_to_memory,        True),
    "create_handoff":      (tool_create_handoff,       True),
    "write_workspace_file":(tool_write_workspace_file, True),
    # Google services (path-C: service-account + DwD)
    "gmail_send":            (_gt.tool_gmail_send,             True),
    "drive_write_file":      (_gt.tool_drive_write_file,       True),
    "calendar_create_event": (_gt.tool_calendar_create_event,  True),
    "gmail_list_messages":   (_gt.tool_gmail_list_messages,    False),
    "gmail_get_message":     (_gt.tool_gmail_get_message,      False),
    "gmail_list_labels":     (_gt.tool_gmail_list_labels,      False),
    "gmail_label_message":   (_gt.tool_gmail_label_message,    True),
    "gmail_archive_message": (_gt.tool_gmail_archive_message,  True),
    "gmail_delete_message":  (_gt.tool_gmail_delete_message,   True),
    "gmail_trash_message":   (_gt.tool_gmail_trash_message,    True),
    "gmail_mark_read":       (_gt.tool_gmail_mark_read,        True),
    "gmail_mark_unread":     (_gt.tool_gmail_mark_unread,      True),
    "calendar_list_events":  (_gt.tool_calendar_list_events,   False),
    "drive_list_files":      (_gt.tool_drive_list_files,       False),
    "drive_read_file":       (_gt.tool_drive_read_file,        False),
    "drive_search":          (_gt.tool_drive_search,           False),
    # Audit
    "get_connection_log":  (tool_get_connection_log,   False),
    "get_write_log":       (tool_get_write_log,        False),
}
