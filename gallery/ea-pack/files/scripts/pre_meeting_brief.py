#!/usr/bin/env python3
# evolve: pkg=p-aab5e569 file=f-3c4d5e6f
"""pre_meeting_brief.py — EA Pack pre-meeting briefer for {bot_id} workspace.

Runs every 15 minutes via launchd. Sends a brief ~60 min before external
calendar events that haven't already been briefed.
"""

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

# ── Shared config/gateway utilities ──────────────────────────────────────────

SHARED_DIR = Path("/Users/Shared/evolve")
NETWORK_JSON = SHARED_DIR / "network.json"


def load_network() -> dict:
    if NETWORK_JSON.exists():
        try:
            return json.loads(NETWORK_JSON.read_text())
        except Exception:
            pass
    return {}


OPENCLAW_BIN_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",   # macOS arm64
    "/usr/local/bin/openclaw",      # macOS x86_64
    "/usr/bin/openclaw",            # Linux
)

# Channel preference when a user is reachable on several. Product default
# (most personal first), not a technical constraint.
CHANNEL_PRIORITY = (
    "telegram", "whatsapp", "signal", "imessage", "slack",
    "discord", "sms", "matrix",
)


def find_openclaw() -> str:
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in OPENCLAW_BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("openclaw CLI not found")


def enabled_channels():
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


def resolve_route(bot_id: str) -> Tuple:
    """(channel, target) for the bot's primary user, from network.json.

    `bots.<id>.primary_user.external_ids` is the single source of truth
    for where this bot's person lives. (None, None) when the bot has no
    recorded delivery route — callers degrade gracefully and must NOT
    write a run file.
    """
    bot_cfg = load_network().get("bots", {}).get(bot_id) or {}
    ids = (bot_cfg.get("primary_user") or {}).get("external_ids") or {}
    enabled = enabled_channels()
    for channel in CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target and (enabled is None or channel in enabled):
            return channel, str(target)
    # The user is only recorded on channels this bot doesn't have —
    # return the best recorded id anyway so the send fails LOUDLY (a
    # miss the monitor reports) instead of silently skipping.
    for channel in CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target:
            return channel, str(target)
    return None, None


def get_workspace(bot_id: str) -> Path:
    oc_json = Path(f"/Users/{bot_id}/.openclaw/openclaw.json")
    if oc_json.exists():
        try:
            cfg = json.loads(oc_json.read_text())
            ws = cfg.get("agents", {}).get("defaults", {}).get("workspace")
            if ws:
                return Path(ws)
        except (json.JSONDecodeError, OSError):
            pass
    return Path(f"/Users/{bot_id}/.openclaw/workspace")


def ea_config(bot_id: str, network: dict) -> dict:
    return (
        network.get("bots", {})
               .get(bot_id, {})
               .get("capabilities", {})
               .get("ea-pack", {})
    )


def send_message(bot_id: str, text: str) -> bool:
    """Deliver *text* to the bot's primary user via `openclaw message send`
    (the supported plain-send surface since OC 2026.6 removed POST
    /api/message — docs/spec-gallery-delivery-convention-2026-06-11.md).

    True = the channel accepted the send. False = no delivery route
    configured (graceful skip). Raises RuntimeError on a failed send.
    Callers write run files ONLY on True.
    """
    channel, target = resolve_route(bot_id)
    if channel is None:
        print(
            f"DELIVERY_SKIPPED: no delivery route for {bot_id} "
            "(network.json primary_user.external_ids empty)",
            file=sys.stderr,
        )
        return False
    openclaw = find_openclaw()
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
    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        cwd=pwd.getpwuid(os.getuid()).pw_dir, env=env,
    )
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:400]
        raise RuntimeError(
            f"openclaw message send failed (rc={r.returncode}): {err}"
        )
    return True


def write_run_file(workspace: Path, action_id: str, sent_at_iso: str) -> None:
    """Per-day run record at memory/ea-runs/<action_id>/{date}.json.

    Written atomically, ONLY after the channel accepted the send — the
    file's existence is the delivery-monitor's proof the user got it
    (spec-proactive-delivery-monitor-2026-06-10.md §5.4).
    """
    runs_dir = workspace / "memory" / "ea-runs" / action_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = {"date": date.today().isoformat(), "sent_at": sent_at_iso}
    fd, tmp = tempfile.mkstemp(dir=runs_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp, runs_dir / f"{record['date']}.json")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Sent-briefs tracking ──────────────────────────────────────────────────────

def _sent_briefs_file(workspace: Path) -> Path:
    return workspace / "memory" / "ea-sent-briefs.json"


def load_sent_briefs(workspace: Path) -> set:
    f = _sent_briefs_file(workspace)
    if f.exists():
        try:
            return set(json.loads(f.read_text()))
        except Exception:
            pass
    return set()


def mark_brief_sent(workspace: Path, event_id: str) -> None:
    sent = load_sent_briefs(workspace)
    sent.add(event_id)
    # Prune to last 100 entries (circular)
    pruned = list(sent)[-100:]
    sbf = _sent_briefs_file(workspace)
    sbf.parent.mkdir(parents=True, exist_ok=True)
    sbf.write_text(json.dumps(pruned))


# ── Calendar reading ──────────────────────────────────────────────────────────

def load_calendar_events(bot_id: str, workspace: Path) -> list:
    """Try two locations for calendar data. Returns list of event dicts."""
    locations = [
        workspace / "memory" / "calendar-today.json",
        SHARED_DIR / "integrations" / bot_id / "calendar-events.json",
    ]
    for loc in locations:
        if loc.exists():
            try:
                data = json.loads(loc.read_text())
                # Accept either a list or a dict with an 'events' key
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("events", [])
            except Exception:
                continue
    return []


# ── External event detection ──────────────────────────────────────────────────

def is_external_event(event: dict, bot_domain: Optional[str]) -> bool:
    """True if at least one attendee has a domain different from bot_domain."""
    attendees = event.get("attendees", [])
    if not attendees:
        return False
    if not bot_domain:
        return True  # No domain info → treat all as external
    for email in attendees:
        parts = email.rsplit("@", 1)
        if len(parts) == 2 and parts[1].lower() != bot_domain.lower():
            return True
    return False


def get_bot_domain(bot_id: str, network: dict) -> Optional[str]:
    email = network.get("bots", {}).get(bot_id, {}).get("email", "")
    if "@" in email:
        return email.rsplit("@", 1)[1]
    return None


# ── Brief composition ─────────────────────────────────────────────────────────

def compose_meeting_brief(event: dict, contacts_dir: Path) -> str:
    lines = [f"Meeting in ~60 minutes: {event['title']}", ""]
    attendees = event.get("attendees", [])
    if attendees:
        lines.append(f"Attendees: {', '.join(attendees)}")
        lines.append("")
    # Check for contact memory files for each attendee
    for email in attendees:
        name_part = email.split("@")[0]
        contact_file = contacts_dir / f"{name_part}.md"
        if contact_file.exists():
            content = contact_file.read_text().strip()
            if content:
                lines.append(f"Context for {email}:")
                relevant = [ln for ln in content.splitlines() if ln.strip()][:10]
                for ln in relevant:
                    lines.append(f"  {ln}")
                lines.append("")
    desc = event.get("description", "").strip()
    if desc:
        lines.append(f"Event notes: {desc[:200]}")
        lines.append("")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="EA Pack pre-meeting brief")
    parser.add_argument("--bot", default="{bot_id}")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    network = load_network()
    workspace = get_workspace(args.bot)
    cfg = ea_config(args.bot, network)
    lookahead_minutes = int(cfg.get("calendar_lookahead_minutes", 60))

    events = load_calendar_events(args.bot, workspace)
    if not events:
        print("INFO: no calendar data available")
        return

    contacts_dir = workspace / "memory" / "contacts"
    bot_domain = get_bot_domain(args.bot, network)
    sent = load_sent_briefs(workspace)

    now_utc = datetime.now(timezone.utc)
    window_end = now_utc + timedelta(minutes=lookahead_minutes)

    briefs_sent = 0
    send_failures = 0
    for event in events:
        event_id = event.get("id", "")
        if not event_id or event_id in sent:
            continue
        start_iso = event.get("start_iso", "")
        if not start_iso:
            continue
        try:
            start_dt = datetime.fromisoformat(start_iso)
            # Make timezone-aware if naive
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Only brief for events starting within the lookahead window
        if not (now_utc <= start_dt <= window_end):
            continue

        if not is_external_event(event, bot_domain):
            continue

        brief = compose_meeting_brief(event, contacts_dir)

        if args.dry_run:
            print(f"=== Pre-Meeting Brief (dry run): {event.get('title', event_id)} ===")
            print(brief)
            briefs_sent += 1
            continue

        try:
            accepted = send_message(args.bot, brief)
        except Exception as e:
            print(f"PREMEET_FAILED: {event_id} {e}", file=sys.stderr)
            send_failures += 1
            continue
        if not accepted:
            break  # no route — later events would skip identically
        mark_brief_sent(workspace, event_id)
        print(f"Pre-meeting brief sent for: {event.get('title', event_id)}")
        briefs_sent += 1
        try:
            write_run_file(
                workspace, "ea-premeet-brief", datetime.now().astimezone().isoformat()
            )
        except OSError as e:
            # The brief was delivered; a failed run record must not turn
            # a successful send into a non-zero exit.
            print(f"WARNING: run-record write failed: {e}", file=sys.stderr)

    if send_failures:
        # Exit non-zero so the delivery monitor sees a crashed run (its
        # delivered-evidence for this app is scheduler_state: a clean
        # exit would read as delivered).
        sys.exit(2)
    if briefs_sent == 0:
        print("INFO: no upcoming external events in lookahead window")


if __name__ == "__main__":
    main()
