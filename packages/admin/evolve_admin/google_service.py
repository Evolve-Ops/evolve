"""Curated Google tool layer — the single source of the Gmail / Calendar /
Drive tool logic, shared by every surface that runs a Google call as a bot.

Spec: [docs/spec-google-integration-architecture-2026-06-20.md](../../docs/spec-google-integration-architecture-2026-06-20.md)
(§4.1 delivery shape, §5 engine = ONE curated Python tool layer, §8 resolved
decisions — Q4: one shared module, no forked logic).

Three surfaces delegate here so the tool logic lives in exactly one place:

* the **bot-facing** ``/api/google/*`` admin route (``web/google_bot_routes.py``)
  — the bot's own agent reaches these via the Evolve OC plugin → loopback.
  Identity is bound server-side to the calling bot (never a request field).
* the **evolve-pod MCP bridge** (``mcp_bridge/google_tools.py``) — kept as a
  surface for Claude-Desktop / evo / admin; its thin ``tool_*`` wrappers
  resolve a bot and call into this module.
* (future) evo's own admin-side Google access.

Every tool resolves credentials through ``google_auth.load_credentials(bot_id,
scopes, network)`` — the unified Path-C (service-account + DwD) / Path-A
(free-gmail OAuth) interface. Credentials stay with the ``evolve`` user; the
caller receives only results (§1.5/§7 — a bot must never hold the DwD key).

Fifteen tools: Gmail ×9, Calendar ×2, Drive ×4 (four write, five read/list,
six modify-scope). Docs / Sheets / Slides are deferred to P3.

The tool implementations here are **synchronous** — they make blocking
``googleapiclient`` calls. The MCP bridge's ``async def tool_*`` wrappers call
them directly (same blocking shape they had inline before the extract); the
Flask route calls them directly too.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Callable

from . import config, google_auth


GMAIL_SCOPES_SEND = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_SCOPES_READ = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_SCOPES_MODIFY = ["https://www.googleapis.com/auth/gmail.modify"]
DRIVE_SCOPES_FILE = ["https://www.googleapis.com/auth/drive.file"]
DRIVE_SCOPES_READ = ["https://www.googleapis.com/auth/drive.readonly"]
CALENDAR_SCOPES_FULL = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_SCOPES_READ = ["https://www.googleapis.com/auth/calendar.readonly"]

# google_integration.mode values this tool layer can execute. A bot whose
# configured mode is outside this set (or absent entirely) gets NO Google
# tools — least-privilege by construction (spec §8 decision 3). Mirrors the
# dispatch in ``google_auth.load_credentials`` (workspace_user_oauth / Path B
# is still NotImplementedError there, so it's intentionally excluded here).
SUPPORTED_MODES = ("service_account_dwd", "free_gmail_oauth")


class UnknownGoogleToolError(ValueError):
    """The requested tool name is not in ``TOOL_SPECS``."""


def _emit_reauth_signal(bot_id: str, exc: "google_auth.RefreshTokenExpired") -> None:
    """Best-effort: write a gmail_oauth_reauth_required Signal.

    Defensive import + try/except — if the signal store isn't wired up
    in this environment (e.g. unit tests), the absence shouldn't break
    the tool call's error-propagation path. The umbrella spec §8.3
    relies on signature dedup, so a missing emission here is recovered
    by the next health-monitor sweep.

    Phase A.4 wires this up to ``signals.store.observe`` with the
    Phase-A-spec-defined signature
    ``(bot_id, "free_gmail_oauth", "refresh_token_expired")``.
    """
    try:
        from . import config as _config
        from signals.store import observe  # type: ignore[import]
    except Exception:
        return
    try:
        net = _config.load_network()
        shared_dir = Path(net.get("sharedDir") or str(_config.DEFAULT_SHARED_DIR))
    except Exception:
        return
    try:
        observe(
            shared_dir,
            producer="gmail_integration_health",
            type="gmail_oauth_reauth_required",
            severity="warn",
            scope="bot",
            bot_id=bot_id,
            signature=f"{bot_id}|free_gmail_oauth|refresh_token_expired",
            body=(
                f"Bot {bot_id!r} Google OAuth refresh token expired or revoked; "
                f"re-consent required."
            ),
            details={
                "bot_id": bot_id,
                "integration_mode": "free_gmail_oauth",
                "failure_class": "refresh_token_expired",
                "status_code": getattr(exc, "status_code", 0),
                "body_excerpt": (getattr(exc, "body", "") or "")[:200],
                "remediation_url": "/api/wizard/google/personal/reconsent",
                "remediation_hint": (
                    "Open the Personal-Gmail wizard for this bot and "
                    "re-approve. The 7-day refresh-token timer applies "
                    "to unverified OAuth apps."
                ),
            },
        )
    except Exception:
        # Signal-store errors should never break the tool's own error path.
        return


def _load_credentials_or_signal(
    bot_id: str, scopes: list[str], network: dict,
):
    """Call ``google_auth.load_credentials``; on Path-A refresh failure,
    emit the dedup'd ``gmail_oauth_reauth_required`` Signal and re-raise.

    Centralizes the Phase A.3 try/except + Phase A.4 reactive signal
    emission so each of the fifteen tools wraps its credential load
    identically — no per-tool drift, no "we added a new tool and forgot
    to emit the signal" failure mode.
    """
    try:
        return google_auth.load_credentials(
            bot_id, scopes=scopes, network=network,
        )
    except google_auth.RefreshTokenExpired as exc:
        _emit_reauth_signal(bot_id, exc)
        raise


def _resolve_sender_name(bot_cfg: dict, bot_id: str) -> str:
    """Decide what name appears in outbound mail's From header.

    Resolution order:
      1. ``correspondence.name`` — explicit alias the operator configured
         (e.g. "Jane" for an EA-style fictional persona)
      2. ``display_name`` — the bot's friendly name from network.json
         (e.g. "Lex")
      3. ``bot_id`` — the bare identifier as last resort ("lex")

    The alias block (correspondence) is OPTIONAL. Bots without it send
    as themselves — vendors see "Lex <lex@example-corp.com>" with a
    plain signature. That's the natural EA-bot default and is arguably
    more ethically transparent than a fictional human-name alias.

    Operators who explicitly want a different alias (the EA "Jane"
    pattern, or alias = primary user's own name for the
    bot-acts-on-behalf-of-user model) set ``correspondence.name`` via
    the path-C wizard.
    """
    corr = bot_cfg.get("correspondence") or {}
    name = (corr.get("name") or "").strip()
    if name:
        return name
    display = (bot_cfg.get("display_name") or "").strip()
    if display:
        return display
    return bot_id


def _build_from_header(bot_cfg: dict, default_sender: str, bot_id: str) -> str:
    """Construct an RFC 5322 From header.

    Uses ``_resolve_sender_name`` so the correspondence block is
    optional: bot sends as itself by default; an explicit alias only
    when the operator configured one.
    """
    sender_name = _resolve_sender_name(bot_cfg, bot_id)
    corr = bot_cfg.get("correspondence") or {}
    sender_email = corr.get("email_address") or default_sender
    return formataddr((sender_name, sender_email))


def _build_signature(
    bot_cfg: dict, primary_user_name: str, bot_id: str
) -> str:
    """Build the email signature.

    When the correspondence block is present, honor its ``signature``
    override and ``disclosure`` level (per the persona spec). When
    absent — the new common case for bots that haven't configured an
    explicit alias — fall back to a plain "name only" signature using
    ``_resolve_sender_name``. No "Assistant to X" line is appended
    without an explicit disclosure choice; that line is opinionated
    and shouldn't be the default.

    Disclosure values (when correspondence is configured):
      * ``soft`` (default) — "<name>\\nAssistant to <user>"
      * ``explicit``       — "<name>\\nAI assistant to <user>"
      * ``none``           — bare name only

    When ``primary_user_name`` is empty/unknown, the soft/explicit
    forms drop the "to <user>" suffix to avoid ugly "Assistant to
    (unknown)" output.
    """
    sender_name = _resolve_sender_name(bot_cfg, bot_id)
    corr = bot_cfg.get("correspondence") or {}

    # Custom signature override takes precedence over disclosure logic.
    custom = corr.get("signature")
    if custom:
        return custom.rstrip("\n")

    # No correspondence block → bare name (no role line).
    if not corr:
        return sender_name

    disclosure = (corr.get("disclosure") or "soft").strip().lower()

    # "none" disclosure → bare name regardless of primary_user_name.
    if disclosure == "none":
        return sender_name

    # soft / explicit add the role line if we know the user's name.
    has_user = primary_user_name and primary_user_name != "(unknown)"
    role_line = "AI assistant" if disclosure == "explicit" else "Assistant"
    if has_user:
        return f"{sender_name}\n{role_line} to {primary_user_name}"
    return f"{sender_name}\n{role_line}"


def _bot_cfg(bot_id: str, network: dict | None = None) -> tuple[dict, dict]:
    """Return (network, bot_cfg) for the named bot. Raises if absent.

    Loads network.json when ``network`` is not supplied — the bridge
    wrappers pass None (one load per call, as before the extract); the
    HTTP route passes its already-loaded network to avoid a re-read.
    """
    if network is None:
        network = config.load_network()
    bot_cfg = (network.get("bots") or {}).get(bot_id)
    if not bot_cfg:
        raise ValueError(f"bot {bot_id!r} not in network.json")
    return network, bot_cfg


def _build_service(api: str, version: str, creds):
    """Build a googleapiclient service (lazy import — see google_auth)."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]
    return build(api, version, credentials=creds, cache_discovery=False)


# ── Write tools ───────────────────────────────────────────────────────────────


def gmail_send(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Send email from the bot's Workspace mailbox, signed as the persona.

    Arguments:
        to: string or list of recipient addresses, required
        subject: required
        body: required, plain text
        cc, bcc: optional list or string
    """
    network, bot_cfg = _bot_cfg(bot_id, network)

    to = args.get("to")
    if not to:
        raise ValueError("to is required")
    if isinstance(to, str):
        to = [to]
    subject = args.get("subject")
    if not subject:
        raise ValueError("subject is required")
    body = args.get("body")
    if body is None or body == "":
        raise ValueError("body is required")
    cc = args.get("cc") or []
    if isinstance(cc, str):
        cc = [cc]
    bcc = args.get("bcc") or []
    if isinstance(bcc, str):
        bcc = [bcc]

    gi = bot_cfg.get("google_integration") or {}
    subject_email = gi.get("subject")
    if not subject_email and gi.get("mode") == "free_gmail_oauth":
        # Path A: subject is the consenting personal-Gmail account, not
        # a Workspace impersonation target. Resolve from the canonical
        # token record's google_account.
        try:
            record = google_auth.load_token_record(bot_id)
        except FileNotFoundError:
            record = {}
        subject_email = record.get("google_account") or ""
    if not subject_email:
        raise ValueError(
            f"bot {bot_id!r}: cannot resolve send-from address. For Path C "
            f"set google_integration.subject; for Path A run the Personal-"
            f"Gmail wizard so a token record exists."
        )
    primary_user_name = (bot_cfg.get("primary_user") or {}).get("name") or "(unknown)"

    from_header = _build_from_header(bot_cfg, subject_email, bot_id)
    signature = _build_signature(bot_cfg, primary_user_name, bot_id)

    google_auth.assert_scopes_available(
        bot_id, GMAIL_SCOPES_SEND, network=network
    )
    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_SEND, network)
    service = _build_service("gmail", "v1", creds)

    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["From"] = from_header
    msg["Subject"] = subject
    msg.set_content(f"{body}\n\n--\n{signature}\n")

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()

    return {
        "ok": True,
        "id": result.get("id"),
        "thread_id": result.get("threadId"),
        "bot": bot_id,
        "from": from_header,
        "to": to,
        "subject": subject,
    }


def drive_write_file(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Create a file in the bot's Drive (drive.file scope).

    Arguments:
        name: file name, required
        content: file body (text), required
        mime_type: optional, defaults to text/plain
        parent_folder_id: optional Drive folder ID (e.g. a shared
            folder the bot's subject has Editor access to)
    """
    network, _ = _bot_cfg(bot_id, network)

    name = args.get("name")
    if not name:
        raise ValueError("name is required")
    content = args.get("content")
    if content is None:
        raise ValueError("content is required")
    mime_type = args.get("mime_type", "text/plain")
    parent_folder_id = args.get("parent_folder_id")

    google_auth.assert_scopes_available(
        bot_id, DRIVE_SCOPES_FILE, network=network
    )
    creds = _load_credentials_or_signal(bot_id, DRIVE_SCOPES_FILE, network)
    service = _build_service("drive", "v3", creds)

    from googleapiclient.http import MediaInMemoryUpload  # type: ignore[import-untyped]

    metadata = {"name": name}
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
    result = service.files().create(
        body=metadata, media_body=media, fields="id, name, webViewLink"
    ).execute()

    return {
        "ok": True,
        "id": result["id"],
        "name": result["name"],
        "web_view_link": result.get("webViewLink"),
        "bot": bot_id,
    }


def calendar_create_event(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Create a Calendar event.

    Arguments:
        summary: event title, required
        start: ISO 8601 datetime with timezone, required (e.g. "2026-06-15T18:00:00-07:00")
        end: ISO 8601 datetime with timezone, required
        description: optional
        location: optional
        calendar_id: optional, defaults to "primary" (the bot's own calendar)
        attendees: optional list of email strings
    """
    network, _ = _bot_cfg(bot_id, network)

    summary = args.get("summary")
    if not summary:
        raise ValueError("summary is required")
    start = args.get("start")
    if not start:
        raise ValueError("start is required (ISO 8601 with timezone)")
    end = args.get("end")
    if not end:
        raise ValueError("end is required (ISO 8601 with timezone)")

    description = args.get("description")
    location = args.get("location")
    calendar_id = args.get("calendar_id", "primary")
    attendees = args.get("attendees") or []
    if isinstance(attendees, str):
        attendees = [attendees]

    google_auth.assert_scopes_available(
        bot_id, CALENDAR_SCOPES_FULL, network=network
    )
    creds = _load_credentials_or_signal(bot_id, CALENDAR_SCOPES_FULL, network)
    service = _build_service("calendar", "v3", creds)

    event_body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location
    if attendees:
        event_body["attendees"] = [{"email": e} for e in attendees]

    result = service.events().insert(
        calendarId=calendar_id, body=event_body
    ).execute()

    return {
        "ok": True,
        "id": result["id"],
        "summary": result.get("summary"),
        "html_link": result.get("htmlLink"),
        "calendar_id": calendar_id,
        "bot": bot_id,
    }


# ── Read / list tools ─────────────────────────────────────────────────────────


def _header(headers: list[dict], name: str) -> str | None:
    """Lookup a header by case-insensitive name from a Gmail metadata list."""
    name_lower = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == name_lower:
            return h.get("value")
    return None


def _decode_gmail_body(payload: dict) -> str:
    """Walk a Gmail message payload and return the best text/plain body.

    Falls back to text/html (with a note) if no text/plain part exists.
    Returns an empty string for messages with no decodable body part
    (e.g. attachment-only mail).
    """
    if not payload:
        return ""

    # Single-part body
    body_data = (payload.get("body") or {}).get("data")
    mime_type = payload.get("mimeType") or ""
    if body_data and mime_type.startswith("text/"):
        try:
            return base64.urlsafe_b64decode(body_data + "===").decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""

    # Multi-part: prefer text/plain, fall back to text/html
    parts = payload.get("parts") or []
    plain = None
    html = None
    for part in parts:
        part_mime = part.get("mimeType") or ""
        nested = _decode_gmail_body(part)  # recurse for multipart/alternative
        if not nested:
            continue
        if part_mime == "text/plain" and plain is None:
            plain = nested
        elif part_mime == "text/html" and html is None:
            html = nested
    if plain is not None:
        return plain
    if html is not None:
        return html
    return ""


def gmail_list_messages(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """List messages in the bot's mailbox.

    Arguments:
        q: Gmail search query (e.g. "from:alice newer_than:7d"), optional
        label_ids: list of Gmail label IDs to filter on, optional
            (e.g. ["INBOX", "UNREAD"]). String accepted; converted to list.
        max_results: int, default 25 (Gmail caps the underlying call at 500)
    """
    network, _ = _bot_cfg(bot_id, network)

    q = args.get("q")
    label_ids = args.get("label_ids") or []
    if isinstance(label_ids, str):
        label_ids = [label_ids]
    max_results = int(args.get("max_results", 25))
    max_results = max(1, min(max_results, 500))

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_READ, network)
    service = _build_service("gmail", "v1", creds)

    list_kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_results}
    if q:
        list_kwargs["q"] = q
    if label_ids:
        list_kwargs["labelIds"] = label_ids
    listing = service.users().messages().list(**list_kwargs).execute()
    refs = listing.get("messages") or []

    # Fetch metadata for each (From / Subject / Date / snippet)
    messages: list[dict] = []
    for ref in refs:
        msg = service.users().messages().get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date", "To"],
        ).execute()
        headers = (msg.get("payload") or {}).get("headers") or []
        messages.append({
            "id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "snippet": msg.get("snippet"),
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "date": _header(headers, "Date"),
            "label_ids": msg.get("labelIds") or [],
        })

    return {
        "ok": True,
        "bot": bot_id,
        "messages": messages,
        "count": len(messages),
        "next_page_token": listing.get("nextPageToken"),
    }


def gmail_get_message(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Fetch one Gmail message with headers and decoded plain-text body.

    Arguments:
        id: Gmail message id, required (from gmail_list_messages)
    """
    network, _ = _bot_cfg(bot_id, network)

    msg_id = args.get("id")
    if not msg_id:
        raise ValueError("id is required (Gmail message id)")

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_READ, network)
    service = _build_service("gmail", "v1", creds)

    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    body = _decode_gmail_body(payload)

    return {
        "ok": True,
        "bot": bot_id,
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "cc": _header(headers, "Cc"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "snippet": msg.get("snippet"),
        "body": body,
        "label_ids": msg.get("labelIds") or [],
    }


def calendar_list_events(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """List events on a calendar within a time window.

    Arguments:
        calendar_id: optional, defaults to "primary" (the bot's own calendar)
        time_min: ISO 8601 with timezone, optional (defaults to now)
        time_max: ISO 8601 with timezone, optional (no upper bound by default)
        q: free-text search across summary/description/location, optional
        max_results: int, default 25 (Google caps the underlying call at 2500)
    """
    network, _ = _bot_cfg(bot_id, network)

    calendar_id = args.get("calendar_id", "primary")
    time_min = args.get("time_min")
    time_max = args.get("time_max")
    q = args.get("q")
    max_results = int(args.get("max_results", 25))
    max_results = max(1, min(max_results, 2500))

    # Default time_min = now (UTC) so the bot doesn't get a flood of
    # historical events when it just asks "what's coming up".
    if not time_min:
        from datetime import datetime, timezone
        time_min = datetime.now(timezone.utc).isoformat()

    creds = _load_credentials_or_signal(bot_id, CALENDAR_SCOPES_READ, network)
    service = _build_service("calendar", "v3", creds)

    list_kwargs: dict[str, Any] = {
        "calendarId": calendar_id,
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
        "timeMin": time_min,
    }
    if time_max:
        list_kwargs["timeMax"] = time_max
    if q:
        list_kwargs["q"] = q

    listing = service.events().list(**list_kwargs).execute()
    items = listing.get("items") or []

    events = [
        {
            "id": ev.get("id"),
            "summary": ev.get("summary"),
            "description": ev.get("description"),
            "location": ev.get("location"),
            "start": (ev.get("start") or {}).get("dateTime")
                     or (ev.get("start") or {}).get("date"),
            "end": (ev.get("end") or {}).get("dateTime")
                   or (ev.get("end") or {}).get("date"),
            "attendees": [
                a.get("email") for a in (ev.get("attendees") or [])
                if a.get("email")
            ],
            "html_link": ev.get("htmlLink"),
            "status": ev.get("status"),
        }
        for ev in items
    ]

    return {
        "ok": True,
        "bot": bot_id,
        "calendar_id": calendar_id,
        "events": events,
        "count": len(events),
        "next_page_token": listing.get("nextPageToken"),
    }


def drive_list_files(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """List Drive files visible to the bot.

    With the default ``drive.file`` scope this lists ONLY files the bot
    created or that are explicitly shared with the bot's subject — NOT
    the user's full Drive. Wider listing requires the ``drive.readonly``
    or ``drive`` scope (catalogued in wizard_google_routes.py:SCOPE_CATALOG
    but not granted by default).

    Arguments:
        q: Drive query (e.g. "mimeType='application/pdf'" or
            "'<folder_id>' in parents"), optional
        page_size: int, default 25 (max 1000)
        order_by: e.g. "modifiedTime desc", optional
    """
    network, _ = _bot_cfg(bot_id, network)

    q = args.get("q")
    page_size = int(args.get("page_size", 25))
    page_size = max(1, min(page_size, 1000))
    order_by = args.get("order_by")

    creds = _load_credentials_or_signal(bot_id, DRIVE_SCOPES_FILE, network)
    service = _build_service("drive", "v3", creds)

    list_kwargs: dict[str, Any] = {
        "pageSize": page_size,
        "fields": (
            "nextPageToken, files(id, name, mimeType, modifiedTime, "
            "webViewLink, owners(emailAddress, displayName), size, "
            "parents)"
        ),
    }
    if q:
        list_kwargs["q"] = q
    if order_by:
        list_kwargs["orderBy"] = order_by

    listing = service.files().list(**list_kwargs).execute()
    items = listing.get("files") or []

    files = [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mimeType"),
            "modified_time": f.get("modifiedTime"),
            "web_view_link": f.get("webViewLink"),
            "owners": [
                {"email": o.get("emailAddress"), "name": o.get("displayName")}
                for o in (f.get("owners") or [])
            ],
            "size_bytes": int(f["size"]) if f.get("size") else None,
            "parent_ids": f.get("parents") or [],
        }
        for f in items
    ]

    return {
        "ok": True,
        "bot": bot_id,
        "files": files,
        "count": len(files),
        "next_page_token": listing.get("nextPageToken"),
    }


def drive_read_file(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Fetch a single Drive file's content.

    Returns decoded text for ``text/*`` MIME types; raw base64 for
    binary. For Google-native types (Docs, Sheets, Slides) this returns
    a structured error pointing at the missing scope — those formats
    need ``documents`` / ``spreadsheets`` / ``presentations`` scopes
    and are not part of this PR's tool surface.

    Arguments:
        id: Drive file id, required (from drive_list_files)
        max_bytes: int, default 1_048_576 (1 MiB); files larger than this
            return metadata only with a ``truncated: true`` flag.
    """
    network, _ = _bot_cfg(bot_id, network)

    file_id = args.get("id")
    if not file_id:
        raise ValueError("id is required (Drive file id)")
    max_bytes = int(args.get("max_bytes", 1_048_576))
    max_bytes = max(1024, min(max_bytes, 10_485_760))  # 1 KiB .. 10 MiB

    creds = _load_credentials_or_signal(bot_id, DRIVE_SCOPES_FILE, network)
    service = _build_service("drive", "v3", creds)

    meta = service.files().get(
        fileId=file_id,
        fields="id, name, mimeType, size, modifiedTime, webViewLink",
    ).execute()
    mime_type = meta.get("mimeType") or ""
    size_str = meta.get("size")
    size_bytes = int(size_str) if size_str else None

    # Google-native types can't be downloaded via files().get(media=True);
    # they need files().export(). Exporting needs the docs/sheets/slides
    # scopes which aren't in this PR.
    if mime_type.startswith("application/vnd.google-apps."):
        return {
            "ok": False,
            "bot": bot_id,
            "id": meta.get("id"),
            "name": meta.get("name"),
            "mime_type": mime_type,
            "web_view_link": meta.get("webViewLink"),
            "error": (
                f"Google-native files ({mime_type}) cannot be read via "
                f"this tool yet. Native Docs/Sheets/Slides reads need "
                f"the documents/spreadsheets/presentations scopes plus "
                f"their own MCP tools. Use the web link for now."
            ),
        }

    if size_bytes is not None and size_bytes > max_bytes:
        return {
            "ok": True,
            "bot": bot_id,
            "id": meta.get("id"),
            "name": meta.get("name"),
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "web_view_link": meta.get("webViewLink"),
            "content": None,
            "truncated": True,
            "note": (
                f"file is {size_bytes} bytes; refusing to load more than "
                f"{max_bytes}. Pass max_bytes to override (up to 10 MiB)."
            ),
        }

    raw = service.files().get_media(fileId=file_id).execute()
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = bytes(raw)

    if mime_type.startswith("text/") or mime_type in (
        "application/json", "application/xml", "application/x-yaml",
    ):
        try:
            content = raw_bytes.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(raw_bytes).decode("ascii")
            encoding = "base64"
    else:
        content = base64.b64encode(raw_bytes).decode("ascii")
        encoding = "base64"

    return {
        "ok": True,
        "bot": bot_id,
        "id": meta.get("id"),
        "name": meta.get("name"),
        "mime_type": mime_type,
        "size_bytes": size_bytes if size_bytes is not None else len(raw_bytes),
        "modified_time": meta.get("modifiedTime"),
        "web_view_link": meta.get("webViewLink"),
        "content": content,
        "encoding": encoding,
        "truncated": False,
    }


# ── Gmail modify (gmail.modify scope; high_privilege, off by default) ─────────


def gmail_list_labels(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """List Gmail labels (system + user) for the bot's mailbox.

    Discovery for the label/archive/mark tools — those take label IDs,
    not display names, and the only safe way to learn the IDs is via
    this list. Uses ``gmail.readonly`` so it's available with the
    default scope set (no ``gmail.modify`` opt-in required).
    """
    network, _ = _bot_cfg(bot_id, network)

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_READ, network)
    service = _build_service("gmail", "v1", creds)

    result = service.users().labels().list(userId="me").execute()
    labels = [
        {
            "id": lbl.get("id"),
            "name": lbl.get("name"),
            "type": lbl.get("type"),  # "system" or "user"
        }
        for lbl in (result.get("labels") or [])
    ]
    return {
        "ok": True,
        "bot": bot_id,
        "labels": labels,
        "count": len(labels),
    }


def gmail_label_message(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Add and/or remove labels on a Gmail message.

    Arguments:
        id: Gmail message id, required (from gmail_list_messages)
        add_label_ids: list of Gmail label IDs to add, optional
            (string accepted; converted to list)
        remove_label_ids: list of Gmail label IDs to remove, optional
            (string accepted; converted to list)

    At least one of add_label_ids / remove_label_ids must be non-empty.
    """
    network, _ = _bot_cfg(bot_id, network)

    msg_id = args.get("id")
    if not msg_id:
        raise ValueError("id is required (Gmail message id)")

    add = args.get("add_label_ids") or []
    if isinstance(add, str):
        add = [add]
    remove = args.get("remove_label_ids") or []
    if isinstance(remove, str):
        remove = [remove]
    if not add and not remove:
        raise ValueError(
            "at least one of add_label_ids / remove_label_ids must be non-empty"
        )

    # Applying the TRASH system label is a recoverable delete: messages.modify
    # with addLabelIds:["TRASH"] is equivalent to messages.trash. That makes it
    # the email kind's ``delete`` verb (autonomy ladder §1.4 "never deletes
    # email"), which a tool-granular deny list cannot block at the argument
    # level on a general labeling tool. So labeling refuses TRASH and routes the
    # caller to the dedicated, ladder-governed ``gmail_trash_message`` — keeping
    # the never-delete wall honest for both delete forms while leaving every
    # benign label working. Removing TRASH (untrash / restore) stays allowed.
    if any(str(lbl).strip().upper() == "TRASH" for lbl in add):
        raise ValueError(
            "applying the TRASH label is a delete action and is not available "
            "through gmail_label_message. Use gmail_trash_message for a "
            "recoverable delete (it is governed by the autonomy ladder), or "
            "gmail_archive_message to remove a message from the inbox without "
            "trashing it."
        )

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_MODIFY, network)
    service = _build_service("gmail", "v1", creds)

    body: dict[str, Any] = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove

    result = service.users().messages().modify(
        userId="me", id=msg_id, body=body
    ).execute()

    return {
        "ok": True,
        "bot": bot_id,
        "id": result.get("id"),
        "thread_id": result.get("threadId"),
        "label_ids": result.get("labelIds") or [],
        "added": add,
        "removed": remove,
    }


def gmail_archive_message(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Archive a Gmail message (remove the INBOX label).

    Convenience wrapper around messages.modify. Matches the Gmail UI's
    "Archive" affordance — the message stays in All Mail but disappears
    from the inbox view.

    Arguments:
        id: Gmail message id, required
    """
    network, _ = _bot_cfg(bot_id, network)

    msg_id = args.get("id")
    if not msg_id:
        raise ValueError("id is required (Gmail message id)")

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_MODIFY, network)
    service = _build_service("gmail", "v1", creds)

    result = service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()

    return {
        "ok": True,
        "bot": bot_id,
        "id": result.get("id"),
        "thread_id": result.get("threadId"),
        "label_ids": result.get("labelIds") or [],
        "archived": True,
    }


def gmail_delete_message(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Permanently delete a Gmail message. NOT recoverable.

    This is messages.delete, not messages.trash — the message is gone
    for good. ``confirm: true`` is required to avoid an accidental
    call wiping mail.

    Arguments:
        id: Gmail message id, required
        confirm: must be true (boolean), required — guard rail
    """
    network, _ = _bot_cfg(bot_id, network)

    msg_id = args.get("id")
    if not msg_id:
        raise ValueError("id is required (Gmail message id)")
    if args.get("confirm") is not True:
        raise ValueError(
            "confirm=true is required to permanently delete a message "
            "(this is messages.delete, not Trash — the message is "
            "unrecoverable). Use gmail_trash_message if you want a "
            "recoverable delete."
        )

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_MODIFY, network)
    service = _build_service("gmail", "v1", creds)

    # messages.delete returns empty body on success (204).
    service.users().messages().delete(userId="me", id=msg_id).execute()

    return {
        "ok": True,
        "bot": bot_id,
        "id": msg_id,
        "deleted": True,
    }


def gmail_trash_message(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Move a Gmail message to Trash. Recoverable (~30 days, then purged).

    This is messages.trash — the recoverable counterpart to
    ``gmail_delete_message`` (messages.delete, permanent) and the canonical
    home for label-based trashing that ``gmail_label_message`` now refuses.
    It is the email kind's ``delete`` verb for the autonomy ladder (spec
    §1.4): the renderer denies it at every rung, so a governed bot never
    reaches it. ``confirm: true`` is required — the same guard rail
    ``gmail_delete_message`` carries — so that in the window before a bot's
    posture render lands (or for any caller the ladder does not govern)
    trashing stays a deliberate act, not a one-token side effect.

    Arguments:
        id: Gmail message id, required
        confirm: must be true (boolean), required — guard rail
    """
    network, _ = _bot_cfg(bot_id, network)

    msg_id = args.get("id")
    if not msg_id:
        raise ValueError("id is required (Gmail message id)")
    if args.get("confirm") is not True:
        raise ValueError(
            "confirm=true is required to move a message to Trash "
            "(recoverable for ~30 days, then purged). To restore a trashed "
            "message, remove the TRASH label with gmail_label_message."
        )

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_MODIFY, network)
    service = _build_service("gmail", "v1", creds)

    result = service.users().messages().trash(userId="me", id=msg_id).execute()

    return {
        "ok": True,
        "bot": bot_id,
        "id": result.get("id"),
        "thread_id": result.get("threadId"),
        "label_ids": result.get("labelIds") or [],
        "trashed": True,
    }


def gmail_mark_read(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Mark a Gmail message read (remove the UNREAD label).

    Arguments:
        id: Gmail message id, required
    """
    network, _ = _bot_cfg(bot_id, network)

    msg_id = args.get("id")
    if not msg_id:
        raise ValueError("id is required (Gmail message id)")

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_MODIFY, network)
    service = _build_service("gmail", "v1", creds)

    result = service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()

    return {
        "ok": True,
        "bot": bot_id,
        "id": result.get("id"),
        "thread_id": result.get("threadId"),
        "label_ids": result.get("labelIds") or [],
        "unread": False,
    }


def gmail_mark_unread(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Mark a Gmail message unread (add the UNREAD label).

    Arguments:
        id: Gmail message id, required
    """
    network, _ = _bot_cfg(bot_id, network)

    msg_id = args.get("id")
    if not msg_id:
        raise ValueError("id is required (Gmail message id)")

    creds = _load_credentials_or_signal(bot_id, GMAIL_SCOPES_MODIFY, network)
    service = _build_service("gmail", "v1", creds)

    result = service.users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": ["UNREAD"]}
    ).execute()

    return {
        "ok": True,
        "bot": bot_id,
        "id": result.get("id"),
        "thread_id": result.get("threadId"),
        "label_ids": result.get("labelIds") or [],
        "unread": True,
    }


# ── Drive search (drive.readonly scope; off by default) ───────────────────────


def drive_search(bot_id: str, args: dict, network: dict | None = None) -> dict:
    """Search across ALL Drive files visible to the bot's subject.

    Distinct from ``drive_list_files``, which loads creds with
    ``drive.file`` and so only sees bot-owned or explicitly shared
    files. This tool loads ``drive.readonly`` so the query runs
    against the subject's full visible corpus — anything the impersonated
    Workspace user could see in the Drive web UI.

    The scope is catalogued in wizard_google_routes.py:SCOPE_CATALOG
    as ``high_privilege=false, default_set=false``. Operators grant it
    via the wizard scope picker; bots without the grant get a clear
    credentials error from the underlying API call.

    Result shape mirrors ``drive_list_files`` so callers can swap one
    for the other depending on the breadth of search they need.

    Arguments:
        q: Drive query (e.g. "name contains 'invoice' and "
            "mimeType='application/pdf'"), optional but recommended —
            without a query this returns the most recently modified files
            visible to the bot, which can be a LOT.
        page_size: int, default 25 (max 1000)
        order_by: e.g. "modifiedTime desc", optional
    """
    network, _ = _bot_cfg(bot_id, network)

    q = args.get("q")
    page_size = int(args.get("page_size", 25))
    page_size = max(1, min(page_size, 1000))
    order_by = args.get("order_by")

    creds = _load_credentials_or_signal(bot_id, DRIVE_SCOPES_READ, network)
    service = _build_service("drive", "v3", creds)

    list_kwargs: dict[str, Any] = {
        "pageSize": page_size,
        "fields": (
            "nextPageToken, files(id, name, mimeType, modifiedTime, "
            "webViewLink, owners(emailAddress, displayName), size, "
            "parents)"
        ),
    }
    if q:
        list_kwargs["q"] = q
    if order_by:
        list_kwargs["orderBy"] = order_by

    listing = service.files().list(**list_kwargs).execute()
    items = listing.get("files") or []

    files = [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mimeType"),
            "modified_time": f.get("modifiedTime"),
            "web_view_link": f.get("webViewLink"),
            "owners": [
                {"email": o.get("emailAddress"), "name": o.get("displayName")}
                for o in (f.get("owners") or [])
            ],
            "size_bytes": int(f["size"]) if f.get("size") else None,
            "parent_ids": f.get("parents") or [],
        }
        for f in items
    ]

    return {
        "ok": True,
        "bot": bot_id,
        "files": files,
        "count": len(files),
        "next_page_token": listing.get("nextPageToken"),
    }


# ── Tool registry + dispatch ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GoogleToolSpec:
    """One curated Google tool.

    ``func`` is the sync implementation ``(bot_id, args, network) -> dict``.
    ``write`` flags mutating tools for the write-audit channel (mirrors the
    bridge's ``is_write_op``). ``summary`` is the one-line description the
    bot-facing surface advertises to the agent.
    """
    name: str
    func: Callable[..., dict]
    write: bool
    summary: str


TOOL_SPECS: dict[str, GoogleToolSpec] = {
    spec.name: spec for spec in (
        # write
        GoogleToolSpec("gmail_send", gmail_send, True,
                       "Send an email from the bot's mailbox."),
        GoogleToolSpec("drive_write_file", drive_write_file, True,
                       "Create a file in the bot's Drive (drive.file scope)."),
        GoogleToolSpec("calendar_create_event", calendar_create_event, True,
                       "Create an event on the bot's calendar."),
        # read / list
        GoogleToolSpec("gmail_list_messages", gmail_list_messages, False,
                       "List messages in the bot's mailbox (optional query / labels)."),
        GoogleToolSpec("gmail_get_message", gmail_get_message, False,
                       "Fetch one message with headers + decoded body."),
        GoogleToolSpec("calendar_list_events", calendar_list_events, False,
                       "List calendar events in a time window."),
        GoogleToolSpec("drive_list_files", drive_list_files, False,
                       "List Drive files the bot owns or is shared on."),
        GoogleToolSpec("drive_read_file", drive_read_file, False,
                       "Fetch a single Drive file's content."),
        # modify / optional-scope
        GoogleToolSpec("gmail_list_labels", gmail_list_labels, False,
                       "List Gmail labels (system + user) — discovery for label IDs."),
        GoogleToolSpec("gmail_label_message", gmail_label_message, True,
                       "Add/remove labels on a message (gmail.modify)."),
        GoogleToolSpec("gmail_archive_message", gmail_archive_message, True,
                       "Archive a message — remove the INBOX label (gmail.modify)."),
        GoogleToolSpec("gmail_delete_message", gmail_delete_message, True,
                       "Permanently delete a message (gmail.modify; confirm required)."),
        GoogleToolSpec("gmail_trash_message", gmail_trash_message, True,
                       "Move a message to Trash — recoverable (gmail.modify; confirm required)."),
        GoogleToolSpec("gmail_mark_read", gmail_mark_read, True,
                       "Mark a message read (gmail.modify)."),
        GoogleToolSpec("gmail_mark_unread", gmail_mark_unread, True,
                       "Mark a message unread (gmail.modify)."),
        GoogleToolSpec("drive_search", drive_search, False,
                       "Search the bot's full visible Drive (drive.readonly)."),
    )
}


def run_google_tool(
    bot_id: str, tool_name: str, args: dict, *, network: dict | None = None
) -> dict:
    """Execute one curated Google tool as ``bot_id``.

    The SINGLE entry point both the bot-facing route and (via the thin
    ``tool_*`` wrappers) the MCP bridge call. ``bot_id`` is supplied by the
    caller's *authenticated* identity — this function NEVER reads a bot/
    subject from ``args``. Raises ``UnknownGoogleToolError`` for an
    unregistered tool name; tool-level validation errors propagate as
    ``ValueError`` (the route maps them to 400).
    """
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        raise UnknownGoogleToolError(
            f"unknown google tool {tool_name!r}; "
            f"known tools: {', '.join(sorted(TOOL_SPECS))}"
        )
    return spec.func(bot_id, args or {}, network)


# ── Configuration / eligibility ───────────────────────────────────────────────


def _bot_google_block(bot_id: str, network: dict) -> dict:
    """Return the bot's ``google_integration`` block (or {})."""
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    return bot_cfg.get("google_integration") or {}


def is_google_configured(bot_id: str, network: dict) -> bool:
    """True iff this bot has a Google integration this tool layer can run.

    The eligibility gate (spec §8 decision 3): a bot gets Google tools ONLY
    when it has a configured ``google_integration`` whose ``mode`` is one
    this layer supports. No config ⇒ no tools — least-privilege by
    construction. Not fleet-wide, not tier-gated.
    """
    gi = _bot_google_block(bot_id, network)
    return gi.get("mode") in SUPPORTED_MODES


def google_state(bot_id: str, network: dict) -> dict:
    """Describe a bot's Google eligibility + the tool surface it would get.

    Returned by ``GET /api/google/state`` so the Evolve OC plugin can decide
    whether to register the curated tools for this bot. The ``tools`` list is
    static (the curated set) but only meaningful when ``configured`` is true.
    """
    gi = _bot_google_block(bot_id, network)
    configured = gi.get("mode") in SUPPORTED_MODES
    return {
        "bot": bot_id,
        "configured": configured,
        "mode": gi.get("mode") if configured else None,
        "scopes": list(gi.get("scopes") or []) if configured else [],
        "tools": [
            {"name": s.name, "description": s.summary, "write": s.write}
            for s in TOOL_SPECS.values()
        ] if configured else [],
    }
