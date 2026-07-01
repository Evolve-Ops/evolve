"""Write-side meeting tools implemented locally via Zoom's REST API.

These are NOT proxied through Zoom's hosted MCP — that MCP doesn't expose
a ``create_meeting`` tool today. We call Zoom's REST API directly using a
Server-to-Server OAuth access token.

Three tools:

- ``create_meeting`` — POST /v2/users/{userId}/meetings
- ``update_meeting`` — PATCH /v2/meetings/{meetingId}
- ``delete_meeting`` — DELETE /v2/meetings/{meetingId}

The ``userId`` defaults to ``me`` (the S2S app's owning user) and is
overridable via ``host_email`` — the shim resolves the email to a Zoom
user id via /v2/users/{email}.

``start_url`` is sensitive (lets the holder join as host without auth)
and is omitted from output by default. The agent must explicitly pass
``include_start_url=True`` to receive it; the response then also includes
a ``warning`` string the conduct layer should respect.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .zoom_s2s import S2sError, ZoomS2sSession


ZOOM_API_BASE = "https://api.zoom.us/v2"


def write_tool_definitions() -> list[dict[str, Any]]:
    """Return MCP tool definitions for the three write tools.

    Mirrors Zoom's read-tool schema shape so the merged tools/list reads
    consistently. Field descriptions are operator-facing — the agent's
    model reads these.
    """
    return [
        {
            "name": "create_meeting",
            "description": (
                "Create a new Zoom meeting and return the join link. "
                "Use this when the user asks to schedule, set up, or share "
                "a Zoom meeting. By default the meeting is created as the "
                "owner of this bot's Zoom account; pass `host_email` to "
                "create it for another user in the same Zoom account."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["topic"],
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Meeting title shown to attendees.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "ISO 8601 datetime (e.g. '2026-06-10T15:00:00Z'). "
                            "Omit for an instant meeting that starts now."
                        ),
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "default": 60,
                        "description": "Duration in minutes; default 60.",
                    },
                    "host_email": {
                        "type": "string",
                        "description": (
                            "Email of the Zoom user the meeting should be "
                            "created for. Defaults to this bot's Zoom user."
                        ),
                    },
                    "agenda": {
                        "type": "string",
                        "description": "Optional meeting description / agenda.",
                    },
                    "include_start_url": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Include the start_url (host-control link) in the "
                            "response. This is SENSITIVE — only request it "
                            "when the user explicitly asks for the host link."
                        ),
                    },
                },
            },
        },
        {
            "name": "update_meeting",
            "description": (
                "Update an existing Zoom meeting (topic, start time, "
                "duration, etc.). Use the meeting_id returned by "
                "create_meeting."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["meeting_id"],
                "properties": {
                    "meeting_id": {
                        "type": "integer",
                        "description": "The Zoom meeting ID.",
                    },
                    "topic": {"type": "string"},
                    "start_time": {"type": "string"},
                    "duration_minutes": {"type": "integer"},
                    "agenda": {"type": "string"},
                },
            },
        },
        {
            "name": "delete_meeting",
            "description": "Cancel and delete a Zoom meeting.",
            "inputSchema": {
                "type": "object",
                "required": ["meeting_id"],
                "properties": {
                    "meeting_id": {
                        "type": "integer",
                        "description": "The Zoom meeting ID.",
                    },
                },
            },
        },
    ]


def create_meeting(
    s2s: ZoomS2sSession,
    *,
    topic: str,
    start_time: Optional[str] = None,
    duration_minutes: int = 60,
    host_email: Optional[str] = None,
    agenda: Optional[str] = None,
    include_start_url: bool = False,
    client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """Create a Zoom meeting; return a sanitized dict.

    The Zoom API returns a large object; we project to a stable subset
    so the agent's tool-output parsing doesn't break when Zoom adds fields.
    """
    user_id = host_email or "me"
    body: dict[str, Any] = {
        "topic": topic,
        "type": 2 if start_time else 1,  # 1=instant, 2=scheduled
        "duration": int(duration_minutes),
    }
    if start_time:
        body["start_time"] = start_time
    if agenda:
        body["agenda"] = agenda
    raw = _zoom_request(
        s2s,
        "POST",
        f"/users/{user_id}/meetings",
        json_body=body,
        client=client,
    )
    return _project_create_response(raw, include_start_url=include_start_url)


def update_meeting(
    s2s: ZoomS2sSession,
    *,
    meeting_id: int,
    topic: Optional[str] = None,
    start_time: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    agenda: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """Patch an existing meeting. Returns {meeting_id, updated: true}."""
    patch: dict[str, Any] = {}
    if topic is not None:
        patch["topic"] = topic
    if start_time is not None:
        patch["start_time"] = start_time
        patch["type"] = 2  # scheduled
    if duration_minutes is not None:
        patch["duration"] = int(duration_minutes)
    if agenda is not None:
        patch["agenda"] = agenda
    if not patch:
        return {"meeting_id": int(meeting_id), "updated": False, "note": "no fields to update"}
    _zoom_request(
        s2s,
        "PATCH",
        f"/meetings/{int(meeting_id)}",
        json_body=patch,
        expect_empty_body=True,
        client=client,
    )
    return {"meeting_id": int(meeting_id), "updated": True}


def delete_meeting(
    s2s: ZoomS2sSession,
    *,
    meeting_id: int,
    client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """Delete a meeting. Returns {meeting_id, deleted: true}."""
    _zoom_request(
        s2s,
        "DELETE",
        f"/meetings/{int(meeting_id)}",
        expect_empty_body=True,
        client=client,
    )
    return {"meeting_id": int(meeting_id), "deleted": True}


def _project_create_response(
    raw: dict[str, Any], *, include_start_url: bool
) -> dict[str, Any]:
    """Project a Zoom create-meeting response to our stable output shape."""
    out: dict[str, Any] = {
        "meeting_id": raw.get("id"),
        "topic": raw.get("topic"),
        "join_url": raw.get("join_url"),
        "password": raw.get("password"),
        "start_time": raw.get("start_time"),
        "duration_minutes": raw.get("duration"),
        "host_email": raw.get("host_email"),
    }
    if include_start_url:
        out["start_url"] = raw.get("start_url")
        out["warning"] = (
            "start_url is SENSITIVE — it grants host privileges without "
            "authentication. Send only to the meeting host, never to "
            "participants or in a shared channel."
        )
    return out


def _zoom_request(
    s2s: ZoomS2sSession,
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    expect_empty_body: bool = False,
    client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """Single REST request with one-shot S2S token refresh on 401.

    Owns its httpx.Client unless one is passed in (for tests).
    """
    owned = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        access_token = s2s.get_access_token()
        resp = _send(client, method, path, access_token, json_body)
        if resp.status_code == 401:
            s2s.invalidate()
            access_token = s2s.get_access_token(force_refresh=True)
            resp = _send(client, method, path, access_token, json_body)
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {"message": resp.text[:500]}
            raise S2sError(
                err.get("message") or f"HTTP {resp.status_code} on {method} {path}",
                code=f"http_{resp.status_code}",
            )
        if expect_empty_body:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}
    finally:
        if owned:
            client.close()


def _send(
    client: httpx.Client,
    method: str,
    path: str,
    access_token: str,
    json_body: Optional[dict],
) -> httpx.Response:
    return client.request(
        method,
        f"{ZOOM_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=json_body,
    )
