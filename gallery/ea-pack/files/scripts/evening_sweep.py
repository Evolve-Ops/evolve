#!/usr/bin/env python3
# evolve: pkg=p-aab5e569 file=f-2b3c4d5e
"""evening_sweep.py — EA Pack evening task sweep for {bot_id} workspace."""

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Tuple

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


# ── Task analysis ─────────────────────────────────────────────────────────────

def iter_tasks(data: dict) -> list:
    """Task entries from tasks.json, tolerating both registry shapes:
    the original dict-of-id shape and the Task Manager v1.1 list shape
    (the v1.1 cutover crashed the EA Pack readers pod-wide on
    2026-06-11 — `'list' object has no attribute 'values'`)."""
    tasks = data.get("tasks")
    if isinstance(tasks, dict):
        return [t for t in tasks.values() if isinstance(t, dict)]
    if isinstance(tasks, list):
        return [t for t in tasks if isinstance(t, dict)]
    return []


def task_name(task: dict) -> str:
    return task.get("name") or task.get("title") or task.get("id", "")


def analyze_tasks(workspace: Path) -> dict:
    """Return completed_today, overdue, and still_open lists."""
    tasks_file = workspace / "tasks.json"
    if not tasks_file.exists():
        return {"completed_today": [], "overdue": [], "still_open": []}
    try:
        data = json.loads(tasks_file.read_text())
    except Exception:
        return {"completed_today": [], "overdue": [], "still_open": []}
    today = date.today()
    completed_today = []
    overdue = []
    still_open = []
    terminal = {"complete", "cancelled"}

    for task in iter_tasks(data):
        status = task.get("status", "open")
        name = task_name(task)

        # Completed today: v1.1 completed_date/completed_at field first,
        # falling back to the legacy status_log entries
        if status == "complete":
            completed_raw = task.get("completed_date") or task.get("completed_at") or ""
            if not completed_raw:
                for entry in reversed(task.get("status_log", [])):
                    if entry.get("status") == "complete":
                        completed_raw = entry.get("date", "")
                        break
            try:
                if completed_raw and date.fromisoformat(str(completed_raw)[:10]) == today:
                    completed_today.append(name)
            except ValueError:
                pass
            continue

        if status in terminal:
            continue

        # Overdue: has due_date in the past
        due_raw = task.get("due_date")
        if due_raw:
            try:
                if date.fromisoformat(due_raw[:10]) < today:
                    overdue.append(name)
                    continue
            except ValueError:
                pass

        still_open.append(name)

    return {"completed_today": completed_today, "overdue": overdue, "still_open": still_open}


# ── Sweep composition ─────────────────────────────────────────────────────────

def compose_sweep(analysis: dict) -> str:
    today = date.today().strftime("%A, %B %-d")
    lines = [f"Evening sweep for {today}.", ""]
    completed = analysis["completed_today"]
    overdue = analysis["overdue"]
    still_open = analysis["still_open"]

    if completed:
        lines.append(f"\u2705 Completed today ({len(completed)}):")
        for t in completed[:5]:
            lines.append(f"  \u2022 {t}")
        if len(completed) > 5:
            lines.append(f"  \u2026and {len(completed) - 5} more")
        lines.append("")
    else:
        lines += ["No tasks completed today.", ""]

    if overdue:
        lines.append(f"\U0001f534 Overdue ({len(overdue)}):")
        for t in overdue[:5]:
            lines.append(f"  \u2022 {t}")
        if len(overdue) > 5:
            lines.append(f"  \u2026and {len(overdue) - 5} more")
        lines.append("")

    if still_open:
        n = len(still_open)
        lines.append(f"{n} task{'s' if n != 1 else ''} still open. Top items:")
        for t in still_open[:3]:
            lines.append(f"  \u2022 {t}")
        lines.append("")

    if not overdue and not still_open:
        lines += ["All clear. Nothing overdue or open.", ""]

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="EA Pack evening sweep")
    parser.add_argument("--bot", default="{bot_id}")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    workspace = get_workspace(args.bot)
    analysis = analyze_tasks(workspace)
    sweep = compose_sweep(analysis)

    if args.dry_run:
        print("=== Evening Sweep (dry run) ===")
        print(sweep)
        return

    try:
        accepted = send_message(args.bot, sweep)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if not accepted:
        return  # DELIVERY_SKIPPED logged; no run file — honest absence
    try:
        write_run_file(
            workspace, "ea-evening-sweep", datetime.now().astimezone().isoformat()
        )
    except OSError as e:
        # Delivered; a failed run record must not turn a successful send
        # into a non-zero exit (the monitor reports unmeasurable, which
        # is honest).
        print(f"WARNING: run-record write failed: {e}", file=sys.stderr)
    print(f"Evening sweep sent to {args.bot}")


if __name__ == "__main__":
    main()
