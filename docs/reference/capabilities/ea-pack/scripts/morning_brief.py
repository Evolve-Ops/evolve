#!/usr/bin/env python3
"""
morning_brief.py — EA Pack application
Sends a morning brief to the primary messaging channel.

Reads:
- memory/tasks.md          — overdue and today's tasks
- /Users/Shared/evolve/proposals/pending/  — pending proposals count
- /Users/Shared/evolve/network.json        — bot config + delivery route

Sends via `openclaw message send` (see
docs/spec-gallery-delivery-convention-2026-06-11.md).

Usage (cron / launchd):
  /Users/Shared/evolve-venv/bin/python3 morning_brief.py --bot admin_bot
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import sys
from datetime import date
from pathlib import Path

SHARED_DIR = Path("/Users/Shared/evolve")
NETWORK_JSON = SHARED_DIR / "network.json"
REPO_DIR = Path("/Users/Shared/evolve-repo")


# ── Config ────────────────────────────────────────────────────────────────────

def load_network() -> dict:
    if NETWORK_JSON.exists():
        return json.loads(NETWORK_JSON.read_text())
    return {}


def _bot_home(bot_id: str, network: dict) -> Path:
    """Resolve a bot's home directory via network.json user override.

    bot_id (logical name) may differ from the macOS account name.
    e.g. team_bot_b runs on the personal_bot_user account: network["bots"]["team_bot_b"]["user"] == "personal_bot_user".
    Falls back to /Users/{bot_id} when no override is configured.
    """
    user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    try:
        import pwd
        return Path(pwd.getpwnam(user).pw_dir)
    except (KeyError, ImportError):
        return Path(f"/Users/{user}")


def bot_config(network: dict, bot_id: str) -> dict:
    """Return the bot entry from network.json, plus ea-pack application config."""
    bot = network.get("bots", {}).get(bot_id, {})
    caps = bot.get("applications", {}).get("ea-pack", {})
    return {"bot": bot, "caps": caps}


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
    import shutil
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in OPENCLAW_BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("openclaw CLI not found")


def resolve_route(bot_id: str) -> "tuple[str, str] | tuple[None, None]":
    """(channel, target) for the bot's primary user, from network.json.

    `bots.<id>.primary_user.external_ids` is the single source of truth
    for where this bot's person lives. (None, None) when the bot has no
    recorded delivery route — callers degrade gracefully and must NOT
    write a run file. (The per-bot gallery copies additionally intersect
    with the bot's own enabled channels; this admin-side reference can't
    read other bots' configs, so the send fails loudly instead.)
    """
    bot_cfg = load_network().get("bots", {}).get(bot_id) or {}
    ids = (bot_cfg.get("primary_user") or {}).get("external_ids") or {}
    for channel in CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target:
            return channel, str(target)
    return None, None


# ── Task parsing ──────────────────────────────────────────────────────────────

URGENT_SECTION = re.compile(r"##\s*🔴\s*URGENT", re.IGNORECASE)
SOON_SECTION = re.compile(r"##\s*🟡\s*SOON", re.IGNORECASE)
SECTION_HEADER = re.compile(r"^##\s+", re.MULTILINE)
TABLE_ROW = re.compile(r"^\|[^|]+\|(.+)\|(.+)\|(.+)\|$")

# Strikethrough = done: ~~text~~
DONE_PATTERN = re.compile(r"~~.+~~")
CHECKMARK_PATTERN = re.compile(r"✅")


def _extract_section(text: str, header_pattern: re.Pattern) -> str:
    """Extract the content of a markdown section matching header_pattern."""
    m = header_pattern.search(text)
    if not m:
        return ""
    start = m.end()
    # Find next section header
    next_header = SECTION_HEADER.search(text, start)
    end = next_header.start() if next_header else len(text)
    return text[start:end].strip()


def _parse_tasks_from_section(section_text: str) -> list[str]:
    """Extract non-done task descriptions from a markdown table section."""
    tasks = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Skip header/separator rows
        if re.match(r"^\|[-| :]+\|$", line):
            continue
        # Skip rows that are clearly done (strikethrough or checkmark)
        if DONE_PATTERN.search(line) and not re.search(r"[^~]~~", line):
            continue
        if CHECKMARK_PATTERN.search(line):
            # Has a checkmark — check if the whole row is completed
            cols = [c.strip() for c in line.strip("|").split("|")]
            task_col = cols[1] if len(cols) > 1 else ""
            if task_col.startswith("✅") or DONE_PATTERN.search(task_col):
                continue
        # Extract task text (column 2)
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) >= 2:
            task_text = cols[1].strip()
            # Clean up markdown artifacts
            task_text = re.sub(r"\*\*(.+?)\*\*", r"\1", task_text)
            task_text = re.sub(r"~~.+~~", "", task_text).strip()
            if task_text and task_text not in ("-", "#", "Task"):
                tasks.append(task_text)
    return tasks


def read_tasks(workspace: Path) -> dict[str, list[str]]:
    """Read tasks.md and return {'urgent': [...], 'soon': [...]}."""
    tasks_file = workspace / "memory" / "tasks.md"
    if not tasks_file.exists():
        return {"urgent": [], "soon": []}
    text = tasks_file.read_text()
    urgent = _parse_tasks_from_section(_extract_section(text, URGENT_SECTION))
    soon = _parse_tasks_from_section(_extract_section(text, SOON_SECTION))
    return {"urgent": urgent, "soon": soon}


# ── Proposals ─────────────────────────────────────────────────────────────────

def count_pending_proposals() -> int:
    pending_dir = SHARED_DIR / "proposals" / "pending"
    if not pending_dir.exists():
        return 0
    return sum(1 for f in pending_dir.iterdir() if f.suffix == ".json")


# ── Brief composition ─────────────────────────────────────────────────────────

def compose_brief(tasks: dict[str, list[str]], pending_proposals: int, bot_id: str) -> str:
    today = date.today().strftime("%A, %B %-d")
    lines = [f"Good morning. Here's your brief for {today}."]
    lines.append("")

    urgent = tasks.get("urgent", [])
    soon = tasks.get("soon", [])

    if urgent:
        lines.append(f"🔴 Urgent ({len(urgent)}):")
        for t in urgent[:5]:
            lines.append(f"  • {t}")
        if len(urgent) > 5:
            lines.append(f"  …and {len(urgent) - 5} more")
        lines.append("")

    if soon:
        lines.append(f"🟡 Due soon ({len(soon)}):")
        for t in soon[:3]:
            lines.append(f"  • {t}")
        if len(soon) > 3:
            lines.append(f"  …and {len(soon) - 3} more")
        lines.append("")

    if not urgent and not soon:
        lines.append("No urgent or upcoming tasks. Clean slate.")
        lines.append("")

    if pending_proposals > 0:
        lines.append(f"📋 {pending_proposals} proposal(s) pending review.")
        lines.append("  Run: evolve-admin proposals list")
        lines.append("")

    lines.append("Have a good day.")
    return "\n".join(lines)


# ── Message delivery ──────────────────────────────────────────────────────────

def send_message(bot_id: str, text: str) -> bool:
    """Deliver *text* to the bot's primary user via `openclaw message send`
    (the supported plain-send surface since OC 2026.6 removed POST
    /api/message — docs/spec-gallery-delivery-convention-2026-06-11.md).

    True = the channel accepted the send. False = no delivery route
    configured (graceful skip). Raises RuntimeError on a failed send.
    """
    import subprocess
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
    # cwd must be readable by the executing account and env-independent
    # (node resolves its CWD at startup; sudo without -H leaves a foreign
    # $HOME). The 60s timeout outlives delivery so the CLI is never
    # killed mid-send.
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


# ── Main ──────────────────────────────────────────────────────────────────────

def get_workspace(bot_id: str, network: "dict | None" = None) -> Path | None:
    net = network or load_network()
    home = _bot_home(bot_id, net)
    oc_json = home / ".openclaw" / "openclaw.json"
    if not oc_json.exists():
        return None
    try:
        cfg = json.loads(oc_json.read_text())
        ws = cfg.get("agents", {}).get("defaults", {}).get("workspace")
        if ws:
            return Path(ws)
    except (json.JSONDecodeError, OSError):
        pass
    return home / ".openclaw" / "workspace"


def main() -> None:
    parser = argparse.ArgumentParser(description="EA Pack morning brief")
    parser.add_argument("--bot", default="admin_bot", help="Bot ID to send brief for")
    parser.add_argument("--dry-run", action="store_true", help="Print brief, don't send")
    args = parser.parse_args()

    bot_id = args.bot
    network = load_network()

    workspace = get_workspace(bot_id, network)
    if workspace is None:
        print(f"ERROR: Cannot find workspace for bot '{bot_id}'", file=sys.stderr)
        sys.exit(1)

    tasks = read_tasks(workspace)
    pending_proposals = count_pending_proposals()
    brief = compose_brief(tasks, pending_proposals, bot_id)

    if args.dry_run:
        print("=== Morning Brief (dry run) ===")
        print(brief)
        return

    try:
        accepted = send_message(bot_id, brief)
    except Exception as e:
        print(f"ERROR: Failed to send brief: {e}", file=sys.stderr)
        sys.exit(1)
    if not accepted:
        return  # DELIVERY_SKIPPED logged; no run file — honest absence
    print(f"Morning brief sent to {bot_id}")


if __name__ == "__main__":
    main()
