#!/usr/bin/env python3
"""
calendar_summary.py — Calendar Daily Summary CLI
Compose and deliver (or preview) the daily calendar message.

Commands:
    send    — fetch calendar events, compose, send via bot gateway, write last_run
    preview — same as send but print to stdout instead of sending
    status  — print last-run info from calendar_summary/last_run.json

Usage (cron / launchd via calendar-summary-cron.sh):
    python3 scripts/calendar_summary.py send \\
        --time-zone "America/Los_Angeles"

Calendar access and message delivery shell out to local CLIs (stdlib only).
Graceful degradation: if Calendar access fails the script sends a short
alert message and exits 1 rather than silently succeeding.

Conflict detection:
  - CONFLICT: event A and event B overlap in time (start_a < end_b AND start_b < end_a)
  - TIGHT TRANSITION: events do not overlap but gap between them is <= 5 minutes
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

#: Timeout for all outbound HTTP requests (seconds).

#: Gap (minutes) below which back-to-back meetings are flagged as "tight".
_TIGHT_TRANSITION_MINUTES = 5

#: Directory for config + last_run state (relative to workspace root).
_STATE_SUBDIR = "calendar_summary"

#: Pod network config — resolves the delivery route (primary user's channel).
_NETWORK_JSON = Path("/Users/Shared/evolve/network.json")


# ── Event data helpers ────────────────────────────────────────────────────────


def _parse_dt(s: str) -> Optional[datetime]:
    """Parse an ISO datetime string (with or without seconds). Returns None on failure."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fmt_time(s: str) -> str:
    """Format a datetime string as a human-readable time (e.g. '9:00am')."""
    dt = _parse_dt(s)
    if dt is None:
        return s or "?"
    hour = dt.hour
    minute = dt.minute
    ampm = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    if minute:
        return f"{hour12}:{minute:02d}{ampm}"
    return f"{hour12}{ampm}"


def _fmt_duration(start_str: str, end_str: str) -> str:
    """Return a short duration string like '1h', '30m', '1h 15m'."""
    start = _parse_dt(start_str)
    end = _parse_dt(end_str)
    if start is None or end is None:
        return ""
    delta = end - start
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return ""
    hours, mins = divmod(total_minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


# ── Conflict detection ────────────────────────────────────────────────────────


def detect_conflicts(events: list[dict]) -> list[dict]:
    """Return a list of conflict records from a list of calendar events.

    Each record has:
      type: "conflict" | "tight"
      event_a: event dict
      event_b: event dict
      description: human-readable description

    Input events must each have 'start' and 'end' keys (ISO datetime strings).
    Events with missing or unparseable times are skipped.

    Algorithm:
    - Sort by start time.
    - For each consecutive pair, check overlap (conflict) or tight transition.
    - Conflict: start_a < end_b AND start_b < end_a
    - Tight: no overlap AND 0 < gap_minutes <= _TIGHT_TRANSITION_MINUTES
    """
    parseable = []
    for ev in events:
        start = _parse_dt(ev.get("start", ""))
        end = _parse_dt(ev.get("end", ""))
        if start is not None and end is not None and end > start:
            parseable.append((start, end, ev))

    parseable.sort(key=lambda t: t[0])

    conflicts: list[dict] = []

    # Check all pairs — not just consecutive — to catch multi-overlap cases.
    for i in range(len(parseable)):
        for j in range(i + 1, len(parseable)):
            start_a, end_a, ev_a = parseable[i]
            start_b, end_b, ev_b = parseable[j]

            # Events are sorted by start; if start_b >= end_a there's no overlap
            # and the gap only grows for later j values → break inner loop.
            if start_b >= end_a:
                gap = (start_b - end_a).total_seconds() / 60
                if 0 <= gap <= _TIGHT_TRANSITION_MINUTES:
                    conflicts.append({
                        "type": "tight",
                        "event_a": ev_a,
                        "event_b": ev_b,
                        "gap_minutes": int(gap),
                        "description": (
                            f"{ev_a.get('title', 'event')} ends right before "
                            f"{ev_b.get('title', 'event')} "
                            f"({int(gap)} min gap)"
                        ),
                    })
                break  # No further j can overlap with i either

            # They overlap
            conflicts.append({
                "type": "conflict",
                "event_a": ev_a,
                "event_b": ev_b,
                "gap_minutes": 0,
                "description": (
                    f"{_fmt_time(ev_a.get('start', ''))} "
                    f"{ev_a.get('title', 'event')} overlaps with "
                    f"{_fmt_time(ev_b.get('start', ''))} "
                    f"{ev_b.get('title', 'event')}"
                ),
            })

    return conflicts


# ── Message composition ───────────────────────────────────────────────────────


def compose_summary(
    *,
    today_events: list[dict],
    tomorrow_events: Optional[list[dict]],
    conflicts: list[dict],
) -> str:
    """Compose the calendar summary message.

    Args:
        today_events:    List of today's events. Each has keys: title, start, end.
        tomorrow_events: List of tomorrow's events, or None if not requested.
        conflicts:       Output of detect_conflicts(today_events).

    Returns:
        The composed message as a plain text string.
    """
    today = date.today()
    weekday = today.strftime("%A")
    date_str = today.strftime("%B %-d")

    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(f"Today — {weekday}, {date_str}")

    # ── Today's events ────────────────────────────────────────────────────────
    if today_events:
        count = len(today_events)
        lines.append(f"Today: {count} {'meeting' if count == 1 else 'meetings'}")
        for ev in today_events:
            t = _fmt_time(ev.get("start", ""))
            name = ev.get("title", "")
            dur = _fmt_duration(ev.get("start", ""), ev.get("end", ""))
            dur_str = f" ({dur})" if dur else ""
            lines.append(f"• {t} — {name}{dur_str}")
    else:
        lines.append("No meetings today.")

    lines.append("")

    # ── Conflicts / tight transitions ─────────────────────────────────────────
    if not conflicts:
        lines.append("Conflicts: none")
    else:
        for c in conflicts:
            if c["type"] == "conflict":
                lines.append(f"Conflict: {c['description']}")
            else:
                lines.append(f"Tight: {c['description']}")

    # ── Tomorrow preview ──────────────────────────────────────────────────────
    if tomorrow_events is not None:
        lines.append("")
        if tomorrow_events:
            count = len(tomorrow_events)
            # Compact format: "Tomorrow: 2 meetings — 10am Wilson estimate, 3pm vendor"
            parts = []
            for ev in tomorrow_events[:3]:  # cap at 3 in the inline preview
                t = _fmt_time(ev.get("start", ""))
                name = ev.get("title", "")
                parts.append(f"{t} {name}")
            tomorrow_str = ", ".join(parts)
            if count > 3:
                tomorrow_str += f" (+ {count - 3} more)"
            lines.append(
                f"Tomorrow: {count} {'meeting' if count == 1 else 'meetings'} — {tomorrow_str}"
            )
        else:
            lines.append("Tomorrow: nothing scheduled")

    return "\n".join(lines).rstrip()


# ── Calendar event stub / fetch ───────────────────────────────────────────────


def fetch_calendar_events(
    time_zone: str,
    date_str: str,
) -> tuple[list[dict], Optional[str]]:
    """Return (events, error_note).

    Events are dicts with keys: id, title, start, end, attendees, description.

    This is a stub that returns empty data; the forge engine wires the real
    Google Calendar API call at deploy time (same pattern as briefing.py).
    """
    # Production: read openclaw.json to find the Google plugin, call Calendar API.
    # Stub for testing: return empty list with no error.
    return [], None  # No events — forge wires real Calendar at deploy time.


# ── State files ───────────────────────────────────────────────────────────────


def _state_dir(workspace: Path) -> Path:
    return workspace / _STATE_SUBDIR


def write_last_run(
    workspace: Path,
    *,
    sent_date: str,
    sent_at: str,
    today_count: int,
    tomorrow_count: int,
    conflict_count: int,
) -> None:
    """Write last_run.json atomically."""
    state_dir = _state_dir(workspace)
    state_dir.mkdir(parents=True, exist_ok=True)
    last_run_path = state_dir / "last_run.json"

    data = {
        "date": sent_date,
        "sent_at": sent_at,
        "today_count": today_count,
        "tomorrow_count": tomorrow_count,
        "conflict_count": conflict_count,
    }
    fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, last_run_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_last_run(workspace: Path) -> dict:
    """Return last_run.json contents, or {} if absent/corrupt."""
    last_run_path = _state_dir(workspace) / "last_run.json"
    try:
        return json.loads(last_run_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _already_sent_today(workspace: Path) -> bool:
    """Return True if last_run.json records a successful send for today."""
    lr = read_last_run(workspace)
    return lr.get("date") == date.today().isoformat() and bool(lr.get("sent_at"))


def read_status_string(workspace: Path) -> str:
    """Return a human-readable status line from last_run.json."""
    lr = read_last_run(workspace)
    if not lr:
        return "No summary sent today yet."
    run_date = lr.get("date", "")
    sent_at = lr.get("sent_at", "")
    today_count = lr.get("today_count", 0)
    conflict_count = lr.get("conflict_count", 0)
    if run_date == date.today().isoformat():
        conflict_note = (
            f", {conflict_count} conflict{'s' if conflict_count != 1 else ''}"
            if conflict_count else ""
        )
        return (
            f"Last summary: today at {sent_at} — "
            f"{today_count} event{'s' if today_count != 1 else ''}{conflict_note}"
        )
    return f"Last summary: {run_date} at {sent_at} (no run yet today)"


# ── Message delivery ──────────────────────────────────────────────────────────

_OPENCLAW_BIN_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",   # macOS arm64
    "/usr/local/bin/openclaw",      # macOS x86_64
    "/usr/bin/openclaw",            # Linux
)

# Channel preference when a user is reachable on several. Product default
# (most personal first), not a technical constraint.
_CHANNEL_PRIORITY = (
    "telegram", "whatsapp", "signal", "imessage", "slack",
    "discord", "sms", "matrix",
)


def _bot_id() -> str:
    """The logical bot id this script runs as.

    Scheduled jobs run as the bot's macOS account, which usually equals
    the network.json bot id — but a bot may run on a different account
    (the per-bot ``user`` override), so map account → bot id when one
    matches. Falls back to the account name.
    """
    account = pwd.getpwuid(os.getuid()).pw_name
    try:
        bots = json.loads(_NETWORK_JSON.read_text()).get("bots", {})
    except (OSError, json.JSONDecodeError):
        return account
    if account in bots:
        return account
    for bot_id, cfg in bots.items():
        if isinstance(cfg, dict) and cfg.get("user") == account:
            return bot_id
    return account


def _find_openclaw() -> str:
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in _OPENCLAW_BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("openclaw CLI not found")


def _enabled_channels():
    """Channels this bot can actually send on (its own openclaw.json,
    channels.<name>.enabled). None when unreadable — trust external_ids
    alone rather than refusing to send."""
    cfg = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".openclaw" / "openclaw.json"
    try:
        channels = json.loads(cfg.read_text()).get("channels", {})
    except (OSError, json.JSONDecodeError):
        return None
    enabled = {
        name for name, c in channels.items()
        if isinstance(c, dict) and c.get("enabled")
    }
    return enabled or None


def _resolve_route(bot_id: str) -> tuple:
    """(channel, target) for the bot's primary user, from network.json.

    `bots.<id>.primary_user.external_ids` is the single source of truth
    for where this bot's person lives. (None, None) when the bot has no
    recorded delivery route — callers degrade gracefully and must NOT
    write a run file.
    """
    try:
        network = json.loads(_NETWORK_JSON.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"cannot read network.json ({_NETWORK_JSON}): {exc}")
        return None, None
    bot_cfg = network.get("bots", {}).get(bot_id) or {}
    ids = (bot_cfg.get("primary_user") or {}).get("external_ids") or {}
    enabled = _enabled_channels()
    for channel in _CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target and (enabled is None or channel in enabled):
            return channel, str(target)
    # The user is only recorded on channels this bot doesn't have —
    # return the best recorded id anyway so the send fails LOUDLY (a
    # miss the monitor reports) instead of silently skipping.
    for channel in _CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target:
            return channel, str(target)
    return None, None


def send_message(bot_id: str, text: str) -> bool:
    """Deliver *text* to the bot's primary user via `openclaw message send`
    (the supported plain-send surface since OC 2026.6 removed POST
    /api/message — docs/spec-gallery-delivery-convention-2026-06-11.md).

    True = the channel accepted the send. False = no delivery route
    configured (graceful skip — caller does NOT write a run record).
    Raises RuntimeError on a failed send.
    """
    channel, target = _resolve_route(bot_id)
    if channel is None:
        print(
            f"DELIVERY_SKIPPED: no delivery route for {bot_id} "
            "(network.json primary_user.external_ids empty)",
            file=sys.stderr,
        )
        return False
    openclaw = _find_openclaw()
    cmd = [
        openclaw, "message", "send",
        f"--channel={channel}", f"--target={target}",
        f"--message={text}", "--json",
    ]
    # launchd/systemd jobs get a minimal PATH; the openclaw entrypoint is
    # `#!/usr/bin/env node`, so node must be resolvable — prepend the CLI's
    # own bin dir (where node also lives) to the child PATH.
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(openclaw) + os.pathsep + env.get("PATH", "")
    # cwd must be bot-readable and env-independent: node resolves its CWD
    # at startup, and under sudo-to-the-bot-account without -H, $HOME (and so
    # Path.home()) still points at the invoking user's untraversable home.
    # The 60s timeout outlives delivery so the CLI is never killed mid-send.
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=pwd.getpwuid(os.getuid()).pw_dir, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"openclaw message send failed: {exc}") from exc
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:400]
        raise RuntimeError(
            f"openclaw message send failed (rc={r.returncode}): {err}"
        )
    return True


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_send(args: argparse.Namespace, dry_run: bool = False) -> int:
    """Compose and send (or preview) the calendar summary.

    Returns 0 on success, 1 on hard failure (Calendar down).
    """
    workspace = _workspace_from_args(args)
    today = date.today()
    today_str = today.isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()

    # ── Idempotency: skip if already sent today (not in dry_run) ─────────────
    if not dry_run and _already_sent_today(workspace):
        print(f"CAL_SUMMARY_SENT: {today_str} already-sent", flush=True)
        return 0

    # ── Fetch today's events ──────────────────────────────────────────────────
    today_events, cal_err = fetch_calendar_events(args.time_zone, today_str)
    if cal_err:
        _warn(f"calendar (today): {cal_err}")

    # ── If Calendar is totally down, alert and exit ───────────────────────────
    if cal_err and not today_events:
        alert_msg = (
            f"Calendar summary couldn't run on {today_str} — "
            f"Calendar access needs a refresh. "
            f"You can fix this from the admin dashboard."
        )
        if dry_run:
            print(alert_msg)
        else:
            try:
                send_message(_bot_id(), alert_msg)
            except RuntimeError as exc:
                _warn(str(exc))
        print(f"CAL_SUMMARY_FAILED: {today_str} calendar-unavailable", flush=True)
        return 1

    # ── Fetch tomorrow's events (opt-in) ──────────────────────────────────────
    tomorrow_events: Optional[list[dict]] = None
    include_tomorrow = getattr(args, "include_tomorrow", True)
    if include_tomorrow:
        tomorrow_events, _ = fetch_calendar_events(args.time_zone, tomorrow_str)
        # tomorrow failure is non-fatal — omit the section gracefully

    # ── Conflict detection (today only) ───────────────────────────────────────
    conflicts = detect_conflicts(today_events)

    # ── Compose ───────────────────────────────────────────────────────────────
    message = compose_summary(
        today_events=today_events,
        tomorrow_events=tomorrow_events,
        conflicts=conflicts,
    )

    # ── Deliver ───────────────────────────────────────────────────────────────
    if dry_run:
        print(message)
    else:
        try:
            accepted = send_message(_bot_id(), message)
        except RuntimeError as exc:
            _warn(f"delivery failed: {exc}")
            print(f"CAL_SUMMARY_FAILED: {today_str} delivery-error", flush=True)
            return 1
        if not accepted:
            # No route to a person — nothing was delivered; skip last_run
            # so the next run doesn't read this as already-sent.
            print(f"CAL_SUMMARY_SKIPPED: {today_str} no-delivery-route", flush=True)
            return 0

    # ── Write last_run ────────────────────────────────────────────────────────
    if not dry_run:
        sent_at = datetime.now().strftime("%H:%M")
        write_last_run(
            workspace,
            sent_date=today_str,
            sent_at=sent_at,
            today_count=len(today_events),
            tomorrow_count=len(tomorrow_events) if tomorrow_events is not None else 0,
            conflict_count=len(conflicts),
        )

    # ── Signal ────────────────────────────────────────────────────────────────
    signal = "CAL_SUMMARY_PARTIAL" if cal_err else "CAL_SUMMARY_SENT"
    print(
        f"{signal}: {today_str} "
        f"{len(today_events)}ev {len(conflicts)}conflicts",
        flush=True,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workspace = _workspace_from_args(args)
    print(read_status_string(workspace))
    return 0


# ── Utilities ─────────────────────────────────────────────────────────────────


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def _workspace_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "workspace", None):
        return Path(args.workspace)
    return Path(__file__).parent.parent.resolve()


# ── Argument parsing ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Calendar Daily Summary — compose and deliver the daily schedule message",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--workspace",
        help="Path to the bot's workspace directory (default: auto-detected)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # send
    send_p = sub.add_parser("send", help="Compose and send the daily calendar summary")
    _add_summary_flags(send_p)

    # preview (same flags, dry-run)
    prev_p = sub.add_parser(
        "preview", help="Preview the summary (print to stdout, don't send)"
    )
    _add_summary_flags(prev_p)

    # status
    sub.add_parser("status", help="Show last-run info from last_run.json")

    return p


def _add_summary_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--time-zone",
        default="America/Los_Angeles",
        help="IANA timezone for the operator (e.g. America/New_York)",
    )
    p.add_argument(
        "--include-tomorrow",
        dest="include_tomorrow",
        action="store_true",
        default=True,
        help="Include tomorrow's meeting preview (default: on)",
    )
    p.add_argument(
        "--no-include-tomorrow",
        dest="include_tomorrow",
        action="store_false",
        help="Omit tomorrow's meeting preview",
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "send":
        sys.exit(cmd_send(args, dry_run=False))
    elif args.command == "preview":
        sys.exit(cmd_send(args, dry_run=True))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
