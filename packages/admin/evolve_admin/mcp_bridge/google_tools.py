"""MCP bridge tools for Google services (Gmail / Drive / Calendar).

**Thin delegation layer.** As of the P1 bot-agent tool work
([internal/spec-google-integration-architecture-2026-06-20.md](../../../internal/spec-google-integration-architecture-2026-06-20.md)
§8 decision 4 — one shared module, no forked logic) the curated tool logic
lives in :mod:`evolve_admin.google_service`. The ``tool_*`` handlers here are
the **evolve-pod MCP bridge** surface (Claude Desktop / evo / admin): each
resolves a bot from the bridge's session/registry — the bridge keeps its
historical ``bot``-argument behavior, an *operator* surface — and delegates to
the shared module.

The other surface, the **bot-facing** ``/api/google/*`` admin route, calls the
same shared module but binds the bot identity server-side to the authenticated
caller (never a request field) — see ``web/google_bot_routes.py``. Repointing
this bridge onto the server-bound identity model is a deliberate fast-follow
(spec §8 decision 4 / DEFERRED list); for P1 the bridge is unchanged in
behavior and shares only the tool *logic*.

Fifteen tools: Gmail ×9, Calendar ×2, Drive ×4. Docs / Sheets / Slides are
deferred to P3.

The bridge runs as the ``evolve`` user, so it has read access to the SA-JSON
secrets and OAuth tokens that ``google_auth.load_credentials`` resolves; bots
never see either — they call tools which load credentials in-process and make
the API calls server-side.

Re-exports below keep ``google_tools.<helper>`` import paths and monkeypatch
targets valid for callers/tests that referenced them before the extract.
"""

from __future__ import annotations

from .. import config, google_auth  # noqa: F401 — re-exported (test monkeypatch targets)
from .. import google_service
from .registry import BotRegistry

# Re-export the shared module's helpers + scope constants so existing import
# paths (``google_tools._build_from_header`` etc.) keep resolving. Tests that
# monkeypatch the EXECUTING module (``_build_service`` / ``_emit_reauth_signal``)
# must target ``evolve_admin.google_service`` — that's where the tool logic runs.
from ..google_service import (  # noqa: F401 — re-exported for back-compat
    GMAIL_SCOPES_SEND,
    GMAIL_SCOPES_READ,
    GMAIL_SCOPES_MODIFY,
    DRIVE_SCOPES_FILE,
    DRIVE_SCOPES_READ,
    CALENDAR_SCOPES_FULL,
    CALENDAR_SCOPES_READ,
    _emit_reauth_signal,
    _load_credentials_or_signal,
    _resolve_sender_name,
    _build_from_header,
    _build_signature,
    _bot_cfg,
    _build_service,
    _header,
    _decode_gmail_body,
)


# ── Bridge tool handlers ───────────────────────────────────────────────────────
# Each resolves the bot from the bridge session/registry (the operator-surface
# ``bot`` argument), then delegates to the shared curated tool logic. _resolve_bot
# is imported lazily to avoid a circular import (tools.py imports this module at
# load time to populate TOOL_HANDLERS).


async def tool_gmail_send(arguments: dict, registry: BotRegistry) -> dict:
    """Send email from the bot's Workspace mailbox. See google_service.gmail_send."""
    from .tools import _resolve_bot
    return google_service.gmail_send(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_drive_write_file(arguments: dict, registry: BotRegistry) -> dict:
    """Create a file in the bot's Drive. See google_service.drive_write_file."""
    from .tools import _resolve_bot
    return google_service.drive_write_file(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_calendar_create_event(arguments: dict, registry: BotRegistry) -> dict:
    """Create a Calendar event. See google_service.calendar_create_event."""
    from .tools import _resolve_bot
    return google_service.calendar_create_event(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_list_messages(arguments: dict, registry: BotRegistry) -> dict:
    """List messages in the bot's mailbox. See google_service.gmail_list_messages."""
    from .tools import _resolve_bot
    return google_service.gmail_list_messages(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_get_message(arguments: dict, registry: BotRegistry) -> dict:
    """Fetch one Gmail message. See google_service.gmail_get_message."""
    from .tools import _resolve_bot
    return google_service.gmail_get_message(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_calendar_list_events(arguments: dict, registry: BotRegistry) -> dict:
    """List calendar events. See google_service.calendar_list_events."""
    from .tools import _resolve_bot
    return google_service.calendar_list_events(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_drive_list_files(arguments: dict, registry: BotRegistry) -> dict:
    """List Drive files. See google_service.drive_list_files."""
    from .tools import _resolve_bot
    return google_service.drive_list_files(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_drive_read_file(arguments: dict, registry: BotRegistry) -> dict:
    """Fetch a Drive file's content. See google_service.drive_read_file."""
    from .tools import _resolve_bot
    return google_service.drive_read_file(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_list_labels(arguments: dict, registry: BotRegistry) -> dict:
    """List Gmail labels. See google_service.gmail_list_labels."""
    from .tools import _resolve_bot
    return google_service.gmail_list_labels(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_label_message(arguments: dict, registry: BotRegistry) -> dict:
    """Add/remove labels on a message. See google_service.gmail_label_message."""
    from .tools import _resolve_bot
    return google_service.gmail_label_message(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_archive_message(arguments: dict, registry: BotRegistry) -> dict:
    """Archive a message. See google_service.gmail_archive_message."""
    from .tools import _resolve_bot
    return google_service.gmail_archive_message(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_delete_message(arguments: dict, registry: BotRegistry) -> dict:
    """Permanently delete a message. See google_service.gmail_delete_message."""
    from .tools import _resolve_bot
    return google_service.gmail_delete_message(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_trash_message(arguments: dict, registry: BotRegistry) -> dict:
    """Move a message to Trash (recoverable). See google_service.gmail_trash_message."""
    from .tools import _resolve_bot
    return google_service.gmail_trash_message(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_mark_read(arguments: dict, registry: BotRegistry) -> dict:
    """Mark a message read. See google_service.gmail_mark_read."""
    from .tools import _resolve_bot
    return google_service.gmail_mark_read(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_gmail_mark_unread(arguments: dict, registry: BotRegistry) -> dict:
    """Mark a message unread. See google_service.gmail_mark_unread."""
    from .tools import _resolve_bot
    return google_service.gmail_mark_unread(_resolve_bot(arguments.get("bot"), registry), arguments)


async def tool_drive_search(arguments: dict, registry: BotRegistry) -> dict:
    """Search the bot's full visible Drive. See google_service.drive_search."""
    from .tools import _resolve_bot
    return google_service.drive_search(_resolve_bot(arguments.get("bot"), registry), arguments)
