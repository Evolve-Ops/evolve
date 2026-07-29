#!/usr/bin/env python3
"""
note_taker.py — Meeting Note-taker daemon.

Runs every 5 minutes (via LaunchDaemon). Reads upcoming and in-progress
Calendar events; for each meeting that is starting soon or in-progress
without a vault note, creates a Markdown note in the Obsidian vault.
Manages workspace/note_taker/active.json so the bot can append notes
during a meeting using its existing file-edit tools.

After a meeting ends, optionally summarizes the appended notes via an
LLM call, then seals the note.

Commands:
    check   — scan Calendar for meetings; create/update vault notes; manage active.json
    status  — print current active meeting (if any) from active.json
    seal    — manually seal the active meeting note (also runs auto-seal logic)

Usage (cron / launchd via note-taker-cron.sh):
    python3 scripts/note_taker.py check \\
        --vault-path "/Users/team_bot_a/Documents/Obsidian" \\
        --meetings-subfolder "meetings" \\
        --time-zone "America/Los_Angeles" \\
        --active-hours "7,19" \\
        --auto-summarize-after-minutes 15

Summaries shell out to the local openclaw CLI (stdlib only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

_ACTIVE_STATE_FILENAME = "active.json"
_NOTE_TAKER_SUBDIR = "note_taker"
_MEETING_LEAD_MINUTES = 5   # create note when meeting starts within this many minutes
_SEAL_SECTION = "\n\n## Sealed\n"


# ── Utilities ─────────────────────────────────────────────────────────────────


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def _workspace_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "workspace", None):
        return Path(args.workspace)
    return Path(__file__).parent.parent.resolve()


_OPENCLAW_BIN_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",   # macOS arm64
    "/usr/local/bin/openclaw",      # macOS x86_64
    "/usr/bin/openclaw",            # Linux
)


def _find_openclaw() -> str:
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in _OPENCLAW_BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("openclaw CLI not found")


def _meeting_slug(title: str, event_date: str) -> str:
    """Produce a filesystem-safe slug from a meeting title and date.

    Format: YYYY-MM-DD-<slug>
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")[:60]
    return f"{event_date}-{slug}"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON to *path* using temp-file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Active-meeting state file ─────────────────────────────────────────────────


def _state_path(workspace: Path) -> Path:
    return workspace / _NOTE_TAKER_SUBDIR / _ACTIVE_STATE_FILENAME


def read_active_state(workspace: Path) -> dict:
    """Return the active-meeting state dict (empty if none/unreadable)."""
    sp = _state_path(workspace)
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_active_state(workspace: Path, state: dict) -> None:
    """Write the active-meeting state atomically."""
    _atomic_write_json(_state_path(workspace), state)


def clear_active_state(workspace: Path) -> None:
    """Remove the active-meeting state (meeting ended)."""
    sp = _state_path(workspace)
    if sp.exists():
        try:
            sp.unlink()
        except OSError as exc:
            _warn(f"could not remove active state: {exc}")


# ── Calendar event fetching ───────────────────────────────────────────────────
#
# In production this calls the Calendar skill's Google API.  The bot's
# Google plugin (calendar skill) is accessible via the local gateway HTTP
# API — but for simplicity and to keep the daemon self-contained, we
# read Calendar events via the GOG tool's API endpoint that the bot exposes.
#
# The stub below is replaced by the provisioner at deploy time.  In the
# interim, calendar events can be injected via --events-json for testing.


def fetch_upcoming_events(time_zone: str) -> tuple[list[dict], Optional[str]]:
    """Return (events, error_str) from the Calendar skill.

    Each event dict:
      {
        "id": str,                  # calendar event ID (stable identifier)
        "title": str,
        "start": str,               # ISO datetime (local timezone)
        "end": str,                 # ISO datetime (local timezone)
        "attendees": list[str],     # email addresses
        "description": str,         # event description / agenda
        "location": str,            # optional
      }

    Production: uses the bot's Calendar skill (calendar.readonly access).
    Stub: returns [] — provisioner wires real implementation at deploy time.
    """
    # Stub: the gateway API for calendar events varies by OpenClaw version.
    # The provisioner substitutes the real implementation here.
    return [], None  # No events — Calendar skill replaces this stub at deploy.


# ── Vault note creation ───────────────────────────────────────────────────────


def _is_in_vault(vault: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(vault.resolve())
        return True
    except ValueError:
        return False


def note_path_for_event(
    vault: Path,
    meetings_subfolder: str,
    slug: str,
) -> Path:
    """Return the absolute path for a meeting note."""
    if meetings_subfolder and meetings_subfolder.strip():
        note_dir = vault / meetings_subfolder.strip()
    else:
        note_dir = vault
    return note_dir / f"{slug}.md"


def create_meeting_note(
    note_path: Path,
    *,
    title: str,
    start: str,
    end: str,
    attendees: list[str],
    description: str,
    location: str,
) -> bool:
    """Create the initial meeting note at *note_path*.

    Returns True on success.  Does not overwrite if the note already exists
    (idempotent — prevents duplicate creation on repeated daemon runs).
    """
    if note_path.exists():
        return True  # already created; don't overwrite

    try:
        note_path.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        _warn(f"cannot create meetings directory: {exc}")
        return False

    # Path-traversal guard: note_path must be inside vault
    vault = note_path.parent
    if not _is_in_vault(vault.parent if vault.parent.exists() else vault, note_path):
        _warn(f"note path {note_path} escapes vault — skipping")
        return False

    attendees_line = ", ".join(attendees) if attendees else "No attendees listed"
    location_line = location if location else "No location"
    desc_block = description.strip() if description else "No description."

    content = (
        f"# {title}\n\n"
        f"## Meeting Details\n"
        f"- **Date/Time:** {start} — {end}\n"
        f"- **Attendees:** {attendees_line}\n"
        f"- **Location:** {location_line}\n\n"
        f"## Agenda / Description\n\n"
        f"{desc_block}\n\n"
        f"## Notes\n\n"
        f"<!-- Bot appends notes here during the meeting -->\n\n"
        f"## Status\n\n"
        f"In progress — created {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )

    try:
        fd, tmp = tempfile.mkstemp(dir=note_path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, note_path)
        return True
    except (PermissionError, OSError) as exc:
        _warn(f"cannot write meeting note {note_path}: {exc}")
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass
        return False


def append_to_meeting_note(note_path: Path, text: str) -> bool:
    """Append *text* to the meeting note under the ## Notes section.

    Returns True on success.
    """
    if not note_path.exists():
        _warn(f"meeting note not found: {note_path}")
        return False

    try:
        existing = note_path.read_text(encoding="utf-8", errors="replace")
    except (PermissionError, OSError) as exc:
        _warn(f"cannot read meeting note: {exc}")
        return False

    timestamp = datetime.now().strftime("%H:%M")
    append_text = f"\n- [{timestamp}] {text.strip()}\n"

    if "## Notes" in existing:
        # Insert before ## Status (or at the end of the Notes section)
        if "## Status" in existing:
            insert_at = existing.index("## Status")
            new_content = existing[:insert_at] + append_text + "\n" + existing[insert_at:]
        else:
            new_content = existing.rstrip() + append_text
    else:
        new_content = existing.rstrip() + "\n\n## Notes\n" + append_text

    try:
        fd, tmp = tempfile.mkstemp(dir=note_path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp, note_path)
        return True
    except (PermissionError, OSError) as exc:
        _warn(f"cannot append to meeting note: {exc}")
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass
        return False


def seal_meeting_note(
    note_path: Path,
    *,
    attendees: list[str],
    summary: Optional[str] = None,
) -> bool:
    """Seal the meeting note — append a ## Sealed section.

    Returns True on success.
    """
    if not note_path.exists():
        _warn(f"meeting note not found for sealing: {note_path}")
        return False

    try:
        existing = note_path.read_text(encoding="utf-8", errors="replace")
    except (PermissionError, OSError) as exc:
        _warn(f"cannot read meeting note for sealing: {exc}")
        return False

    if "## Sealed" in existing:
        return True  # already sealed

    sealed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    attendees_line = ", ".join(attendees) if attendees else "No attendees"
    summary_block = f"\n**Summary:** {summary.strip()}\n" if summary else ""

    seal_block = (
        f"{_SEAL_SECTION}"
        f"- Sealed at {sealed_at}\n"
        f"- Attendees: {attendees_line}\n"
        f"{summary_block}"
    )

    new_content = existing.rstrip() + seal_block

    try:
        fd, tmp = tempfile.mkstemp(dir=note_path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp, note_path)
        return True
    except (PermissionError, OSError) as exc:
        _warn(f"cannot seal meeting note: {exc}")
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass
        return False


# ── Optional LLM summarization ────────────────────────────────────────────────


def summarize_notes(note_path: Path) -> Optional[str]:
    """Request an LLM summary of the notes section from the bot's agent.

    This is an escalation — the daemon only calls this after the meeting ends
    and auto_summarize_after_minutes have passed.  Returns a summary string
    on success, None on failure (caller seals without summary).
    """
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError) as exc:
        _warn(f"summarize_notes: cannot read note: {exc}")
        return None

    # Extract the Notes section content
    notes_match = re.search(r"## Notes\n(.*?)(?:## Status|## Sealed|$)", text, re.DOTALL)
    if not notes_match:
        return None
    notes_content = notes_match.group(1).strip()
    # Strip the placeholder comment
    notes_content = re.sub(r"<!-- .* -->", "", notes_content).strip()
    if not notes_content:
        return None  # no real notes were taken; skip summary

    prompt = (
        f"These are raw meeting notes. Summarize them in 2-4 sentences:\n\n{notes_content}"
    )
    # One agent turn via the openclaw CLI — the supported replacement for
    # the gateway's removed /api/message return_response mode
    # (docs/spec-gallery-delivery-convention-2026-06-11.md). The reply is
    # printed to stdout; no --deliver, nothing reaches the user's channel.
    try:
        r = subprocess.run(
            [_find_openclaw(), "agent", f"--message={prompt}"],
            capture_output=True, text=True, timeout=120, cwd=str(Path.home()),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"summarize_notes: openclaw agent call failed: {exc}")
        return None
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:200]
        _warn(f"summarize_notes: openclaw agent failed (rc={r.returncode}): {err}")
        return None
    return r.stdout.strip() or None


# ── Time helpers ──────────────────────────────────────────────────────────────


def _parse_event_dt(dt_str: str, tz_name: str) -> Optional[datetime]:
    """Parse an ISO datetime string, falling back to date-only format."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    return None


def _is_in_active_hours(active_hours: tuple[int, int]) -> bool:
    now = datetime.now()
    return active_hours[0] <= now.hour < active_hours[1]


def _event_needs_note(
    event: dict,
    tz_name: str,
    now: datetime,
    lead_minutes: int = _MEETING_LEAD_MINUTES,
) -> bool:
    """Return True if this event should trigger note creation.

    Conditions:
    - starts within the next `lead_minutes`, OR
    - started in the past and hasn't ended yet (ongoing)
    """
    start = _parse_event_dt(event.get("start", ""), tz_name)
    end = _parse_event_dt(event.get("end", ""), tz_name)
    if start is None:
        return False

    lead = timedelta(minutes=lead_minutes)
    if now <= start <= now + lead:
        return True  # starting soon
    if start <= now and (end is None or now < end):
        return True  # in progress
    return False


def _event_has_ended(event: dict, tz_name: str, now: datetime) -> bool:
    end = _parse_event_dt(event.get("end", ""), tz_name)
    if end is None:
        return False
    return now >= end


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_check(args: argparse.Namespace) -> int:
    """Main daemon loop: check Calendar, manage vault notes, update active.json."""
    workspace = _workspace_from_args(args)

    # ── Active-hours gate ────────────────────────────────────────────────────
    active_hours_raw = getattr(args, "active_hours", "7,19") or "7,19"
    try:
        ah_parts = [int(x.strip()) for x in active_hours_raw.split(",")]
        active_hours = (ah_parts[0], ah_parts[1])
    except (ValueError, IndexError):
        active_hours = (7, 19)

    if not _is_in_active_hours(active_hours):
        print(f"NOTE_TAKER_IDLE: outside active hours ({active_hours[0]}–{active_hours[1]})", flush=True)
        return 0

    # ── Resolve vault path ───────────────────────────────────────────────────
    vault_path_str = getattr(args, "vault_path", "") or ""
    if not vault_path_str.strip():
        _warn("no vault_path configured — cannot create meeting notes")
        print("NOTE_TAKER_FAILED: no_vault_path", flush=True)
        return 1

    try:
        vault = Path(vault_path_str.strip()).expanduser().resolve()
    except (TypeError, ValueError) as exc:
        _warn(f"invalid vault_path: {exc}")
        print("NOTE_TAKER_FAILED: invalid_vault_path", flush=True)
        return 1

    if not vault.is_dir():
        _warn(f"vault not found at {vault_path_str!r}")
        print("NOTE_TAKER_FAILED: vault_not_found", flush=True)
        return 1

    meetings_subfolder = getattr(args, "meetings_subfolder", "meetings") or "meetings"
    time_zone = getattr(args, "time_zone", "America/Los_Angeles") or "America/Los_Angeles"
    auto_summarize_minutes = int(getattr(args, "auto_summarize_after_minutes", 15) or 15)

    # ── Fetch Calendar events ────────────────────────────────────────────────
    events, cal_err = fetch_upcoming_events(time_zone)
    if cal_err:
        _warn(f"calendar fetch: {cal_err}")
        print("NOTE_TAKER_PARTIAL: calendar_unavailable", flush=True)

    now = datetime.now()
    today_str = now.date().isoformat()

    # ── Read current active state ────────────────────────────────────────────
    state = read_active_state(workspace)
    active_event_id = state.get("event_id")
    active_note_rel = state.get("note_path")  # relative to vault

    # ── Check if active meeting has ended + needs sealing ────────────────────
    if active_event_id and active_note_rel:
        # Find the active event in the current event list
        active_ev = next((e for e in events if e.get("id") == active_event_id), None)
        if active_ev and _event_has_ended(active_ev, time_zone, now):
            end_dt = _parse_event_dt(active_ev.get("end", ""), time_zone)
            grace_window = timedelta(minutes=auto_summarize_minutes)
            if end_dt and now >= end_dt + grace_window:
                # Time to seal
                abs_note_path = vault / active_note_rel
                summary: Optional[str] = None
                try:
                    summary = summarize_notes(abs_note_path)
                except Exception as exc:
                    _warn(f"summarization failed: {exc}")
                sealed = seal_meeting_note(
                    abs_note_path,
                    attendees=active_ev.get("attendees", []),
                    summary=summary,
                )
                if sealed:
                    print(f"NOTE_TAKER_SEALED: {active_note_rel}", flush=True)
                    clear_active_state(workspace)
                    active_event_id = None
                    active_note_rel = None

    # ── Scan events for meetings needing notes ───────────────────────────────
    created_count = 0
    for event in events:
        if not _event_needs_note(event, time_zone, now):
            continue

        event_id = event.get("id", "")
        title = event.get("title", "Meeting")
        start_str = event.get("start", today_str)
        event_date = start_str[:10] if len(start_str) >= 10 else today_str

        slug = _meeting_slug(title, event_date)
        note_path = note_path_for_event(vault, meetings_subfolder, slug)
        note_rel = str(note_path.relative_to(vault)) if note_path.is_relative_to(vault) else \
            f"{meetings_subfolder}/{slug}.md" if meetings_subfolder else f"{slug}.md"

        # Create note if not yet created
        created = create_meeting_note(
            note_path,
            title=title,
            start=event.get("start", ""),
            end=event.get("end", ""),
            attendees=event.get("attendees", []),
            description=event.get("description", ""),
            location=event.get("location", ""),
        )

        if created:
            created_count += 1
            print(f"NOTE_TAKER_NOTE_CREATED: {note_rel}", flush=True)

        # Update active state to point to the most recent/relevant meeting
        # (last matching event wins; daemon runs frequently enough that this is fine)
        if event_id and (event_id != active_event_id or active_note_rel != note_rel):
            write_active_state(workspace, {
                "event_id": event_id,
                "title": title,
                "note_path": note_rel,         # relative to vault
                "vault_path": str(vault),
                "start": event.get("start", ""),
                "end": event.get("end", ""),
                "attendees": event.get("attendees", []),
                "meeting_in_progress": True,
                "updated_at": now.isoformat(timespec="seconds"),
            })

    if not events or not any(_event_needs_note(e, time_zone, now) for e in events):
        # No meetings active right now — clear active state if stale
        if state.get("meeting_in_progress"):
            # Check if the state's meeting has ended
            active_end_str = state.get("end", "")
            if active_end_str:
                active_end = _parse_event_dt(active_end_str, time_zone)
                if active_end and now >= active_end + timedelta(minutes=auto_summarize_minutes):
                    clear_active_state(workspace)

    signal = "NOTE_TAKER_RAN"
    print(f"{signal}: {today_str} {created_count} notes created", flush=True)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print current active meeting state."""
    workspace = _workspace_from_args(args)
    state = read_active_state(workspace)
    if not state:
        print("No meeting in progress.")
    else:
        title = state.get("title", "Unknown meeting")
        note = state.get("note_path", "unknown")
        start = state.get("start", "")
        print(f"Meeting: {title}")
        print(f"  Start: {start}")
        print(f"  Note: {note}")
    return 0


def cmd_seal(args: argparse.Namespace) -> int:
    """Manually seal the active meeting note."""
    workspace = _workspace_from_args(args)
    state = read_active_state(workspace)
    if not state:
        print("No active meeting to seal.")
        return 0

    vault_path_str = state.get("vault_path", getattr(args, "vault_path", "") or "")
    note_rel = state.get("note_path", "")
    attendees = state.get("attendees", [])

    if not vault_path_str or not note_rel:
        _warn("active.json is incomplete — cannot seal")
        return 1

    vault = Path(vault_path_str).expanduser().resolve()
    note_path = vault / note_rel

    summary: Optional[str] = None
    try:
        summary = summarize_notes(note_path)
    except Exception as exc:
        _warn(f"summarization failed: {exc}")

    sealed = seal_meeting_note(note_path, attendees=attendees, summary=summary)
    if sealed:
        print(f"NOTE_TAKER_SEALED: {note_rel}", flush=True)
        clear_active_state(workspace)
    else:
        print("NOTE_TAKER_SEAL_FAILED", flush=True)
        return 1
    return 0


# ── Argument parsing ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Meeting Note-taker — create and manage meeting notes in Obsidian",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--workspace", help="Path to the bot's workspace directory (auto-detected)")
    sub = p.add_subparsers(dest="command", required=True)

    # check
    check_p = sub.add_parser("check", help="Scan Calendar; create/update meeting notes")
    _add_note_taker_flags(check_p)

    # status
    sub.add_parser("status", help="Show current active meeting note")

    # seal
    seal_p = sub.add_parser("seal", help="Manually seal the active meeting note")
    seal_p.add_argument("--vault-path", default="", help="Obsidian vault path (if not in active.json)")

    return p


def _add_note_taker_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--vault-path",
        default="",
        help="Absolute path to Obsidian vault (e.g. /Users/team_bot_a/Documents/Obsidian)",
    )
    p.add_argument(
        "--meetings-subfolder",
        default="meetings",
        help="Subfolder inside the vault for meeting notes (default: meetings)",
    )
    p.add_argument(
        "--time-zone",
        default="America/Los_Angeles",
        help="IANA timezone (e.g. America/New_York)",
    )
    p.add_argument(
        "--active-hours",
        default="7,19",
        help="Active hour range as 'START,END' (e.g. '7,19' = 7am–7pm)",
    )
    p.add_argument(
        "--auto-summarize-after-minutes",
        type=int,
        default=15,
        help="Minutes after meeting end before auto-sealing with LLM summary (default: 15)",
    )
    p.add_argument(
        "--events-json",
        default="",
        help="(Test/debug) Path to a JSON file with events; overrides Calendar fetch",
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "check":
        # --events-json override for testing
        if getattr(args, "events_json", "") and args.events_json.strip():
            global fetch_upcoming_events
            events_path = Path(args.events_json)
            def _stub_fetch(tz):
                try:
                    return json.loads(events_path.read_text()), None
                except (OSError, json.JSONDecodeError) as exc:
                    return [], str(exc)
            fetch_upcoming_events = _stub_fetch

        sys.exit(cmd_check(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "seal":
        sys.exit(cmd_seal(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
