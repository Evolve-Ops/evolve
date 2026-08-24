"""Per-user notification queue (Evolve component).

Spec: internal/spec-forge-via-messaging-2026-05-07.md §5.

Producers (currently just :mod:`applications.forge_jobs`) write events
to ``{shared_dir}/notifications/<user_key>.jsonl``. Consumers
(:mod:`session_surface` at session_start) read unread events and inject
them into the bot's system prompt as a labeled section, then mark them
read by appending an offset record.

The queue is intentionally simple: append-only jsonl, one event per
line, no separate index. The "read" marker is itself an event that
records the byte offset up to which we've consumed. On read, we skip
events at-or-before that offset. This keeps the queue self-contained
and auditable — operators can ``cat`` a user's queue file to see
everything that ever happened.

Event shape (producer fills these):

.. code-block:: jsonc

    {
      "ts":      "2026-05-07T10:30:00Z",
      "kind":    "forge_complete" | "forge_failed",
      "bot_id":  "team_bot_a",
      "app_id":  "app_calendar_watcher",
      "app_name": "calendar-watcher",
      "summary": "Watches your calendar… (verbatim conversational summary)",
      "detail":  "How to use it: …\nReply 'snooze' to mute alerts."
                  // free-form prose, rendered to the user
    }

Consumers don't need to care about the producer-specific fields beyond
``kind`` — the ``detail`` block is the body the bot speaks.

Ownership: this module lives under :mod:`evo` because it's part of the
``evo`` UX surface (session-start injection). It is NOT an OpenClaw
primitive; OC just sees the assembled ``systemAppend`` block we build
from the events.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

from evolve_util import now_iso as _now_iso


_SUBDIR = "notifications"
_READ_MARKER_KIND = "_read_marker"


def _user_key_filename(user_key: str) -> str:
    """Encode ``user_key`` into a portable filename — colons are common
    in our keys (``ext:telegram:12345``) but trip up tools that scan
    paths. Same encoding as the wizard state file uses."""
    return user_key.replace(":", "__").replace("/", "_") + ".jsonl"


def queue_path(shared_dir: Path, user_key: str) -> Path:
    return Path(shared_dir) / _SUBDIR / _user_key_filename(user_key)


# ─────────────────────────────────────────────────────────────────────────────
# Producer side
# ─────────────────────────────────────────────────────────────────────────────


def append_event(
    shared_dir: Path,
    user_key: str,
    *,
    kind: str,
    bot_id: str,
    app_id: str | None = None,
    app_name: str | None = None,
    summary: str | None = None,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
    ts: str | None = None,
) -> None:
    """Append one event to the user's notification queue. Atomic at the
    OS level for line-sized writes; no temp+rename needed.

    ``user_key`` should be the same shape consumers (session_surface,
    wizard) use — typically ``ext:<channel>:<external_id>`` for
    secondary/external callers and ``pod:<pod_user>`` for primaries
    with a recorded pod identity.
    """
    if not user_key:
        raise ValueError("user_key is required")
    if not kind:
        raise ValueError("kind is required")

    record: dict[str, Any] = {
        "ts": ts or _now_iso(),
        "kind": kind,
        "bot_id": bot_id,
    }
    if app_id is not None:
        record["app_id"] = app_id
    if app_name is not None:
        record["app_name"] = app_name
    if summary is not None:
        record["summary"] = summary
    if detail is not None:
        record["detail"] = detail
    if extra:
        for k, v in extra.items():
            if k not in record:
                record[k] = v

    path = queue_path(shared_dir, user_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with path.open("ab") as fh:
        fh.write(line.encode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Consumer side
# ─────────────────────────────────────────────────────────────────────────────


def read_unread(
    shared_dir: Path,
    user_key: str,
) -> list[dict[str, Any]]:
    """Return all events the user hasn't seen yet. Caller is responsible
    for calling :func:`mark_read` after they've successfully surfaced
    the events to the user — this avoids losing events when the bot's
    system_prompt-build path errors out partway."""
    path = queue_path(shared_dir, user_key)
    if not path.exists():
        return []
    # The read offset is the byte position of the most recent
    # _read_marker event; everything past that is unread.
    last_marker_offset = _last_read_offset(path)
    out: list[dict[str, Any]] = []
    with path.open("rb") as fh:
        fh.seek(last_marker_offset)
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("kind") == _READ_MARKER_KIND:
                continue
            out.append(rec)
    return out


def mark_read(shared_dir: Path, user_key: str) -> None:
    """Append a ``_read_marker`` event whose ts records "all events
    before this point are consumed." Subsequent reads start after this
    marker. Idempotent — calling repeatedly with no new events between
    calls just appends multiple markers; harmless."""
    path = queue_path(shared_dir, user_key)
    if not path.exists():
        return
    record = {"ts": _now_iso(), "kind": _READ_MARKER_KIND}
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with path.open("ab") as fh:
        fh.write(line.encode("utf-8"))


def _last_read_offset(path: Path) -> int:
    """Walk the file finding the byte offset of the most recent
    ``_read_marker``. Returns the byte after that marker (so callers
    seek there and start reading new events). Returns 0 if no marker
    exists yet."""
    offset = 0
    pos = 0
    with path.open("rb") as fh:
        for line in fh:
            line_len = len(line)
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("kind") == _READ_MARKER_KIND:
                    offset = pos + line_len
            except json.JSONDecodeError:
                pass
            pos += line_len
    return offset


# ─────────────────────────────────────────────────────────────────────────────
# Rendering (consumer-side helper)
# ─────────────────────────────────────────────────────────────────────────────


def render_for_session_prompt(events: list[dict[str, Any]]) -> str:
    """Build a labeled markdown block summarizing unread events for the
    bot's session_start systemAppend. Returns ``""`` for empty input
    (caller can skip injection)."""
    if not events:
        return ""

    lines = ["[EVOLVE NOTIFICATIONS — surface to the user at the start of your reply]"]
    for ev in events:
        kind = ev.get("kind", "?")
        if kind == "forge_complete":
            lines.append(_render_forge_complete(ev))
        elif kind == "forge_failed":
            lines.append(_render_forge_failed(ev))
        elif kind == "investigation_diagnosis":
            # Workstream C — `evo fail` reply. The `detail` field already
            # carries the Plex-tested body the user should see; surface
            # it verbatim. See app_audit_investigation.render_notification_detail.
            lines.append(_render_investigation_diagnosis(ev))
        else:
            # Unknown kind — surface what we can without crashing.
            lines.append(_render_generic(ev))
    lines.append(
        "\nLead your next reply with a brief mention of these — weave it in "
        "rather than dumping the block verbatim. Then continue with whatever "
        "the user actually asked."
    )
    return "\n\n".join(lines)


def _render_forge_complete(ev: dict[str, Any]) -> str:
    name = ev.get("app_name") or ev.get("app_id") or "an app"
    summary = (ev.get("summary") or "").strip()
    detail = (ev.get("detail") or "").strip()
    parts = [f"✓ Build complete: **{name}**"]
    if summary:
        parts.append(f"What it does (verbatim from your spec):\n{summary}")
    if detail:
        parts.append(f"How to use it:\n{detail}")
    return "\n\n".join(parts)


def _render_forge_failed(ev: dict[str, Any]) -> str:
    name = ev.get("app_name") or ev.get("app_id") or "an app"
    summary = (ev.get("summary") or "").strip()
    detail = (ev.get("detail") or "").strip()
    parts = [f"✗ Build failed: **{name}**"]
    if detail:
        parts.append(f"What went wrong:\n{detail}")
    if summary:
        parts.append(
            "The spec we agreed on (for reference if you want to revise):\n"
            f"{summary}"
        )
    parts.append(
        "Reply with what you'd like to do — try again, adjust the spec, "
        "or shelve it for now."
    )
    return "\n\n".join(parts)


def _render_investigation_diagnosis(ev: dict[str, Any]) -> str:
    """Render an ``evo fail`` diagnosis reply for session-prompt injection.

    The ``detail`` field is the Plex-tested body produced by the audit
    poller; we just label it so the bot understands what it is.
    """
    detail = (ev.get("detail") or "").strip()
    summary = (ev.get("summary") or "").strip()
    parts = ["**Investigation result** (from your `evo fail` report)"]
    if summary:
        parts.append(f"You asked about: {summary}")
    if detail:
        parts.append(detail)
    return "\n\n".join(parts)


def _render_generic(ev: dict[str, Any]) -> str:
    detail = (ev.get("detail") or ev.get("summary") or "").strip()
    kind = ev.get("kind", "notification")
    if detail:
        return f"[{kind}] {detail}"
    return f"[{kind}]"
