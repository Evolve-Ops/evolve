#!/usr/bin/env python3
"""
example_script.py — template application script

Replace this with your actual script. Scripts in applications/<app>/scripts/
run as cron jobs or one-off analyzers.

Standard conventions:
- Read config from /Users/Shared/evolve/network.json
- Read bot-specific state from /Users/Shared/evolve/applications/{bot}/
- Deliver user-facing messages via `openclaw message send` (see below and
  docs/spec-gallery-delivery-convention-2026-06-11.md)
- Exit 0 on success, non-zero on failure
- Write output to stdout; errors to stderr
- If the app declares run_file delivery evidence, write the run file
  atomically and ONLY after send_message() returned True
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

SHARED_DIR = Path("/Users/Shared/evolve")
NETWORK_JSON = SHARED_DIR / "network.json"

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


def load_network() -> dict:
    if NETWORK_JSON.exists():
        return json.loads(NETWORK_JSON.read_text())
    return {}


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


def resolve_route(bot_id: str) -> "tuple[str, str] | tuple[None, None]":
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


def send_message(bot_id: str, text: str) -> bool:
    """Deliver *text* to the bot's primary user via `openclaw message send`
    (the supported plain-send surface since OC 2026.6 removed POST
    /api/message — docs/spec-gallery-delivery-convention-2026-06-11.md).

    True = the channel accepted the send. False = no delivery route
    configured (graceful skip — log it and exit 0, but do NOT write a run
    file). Raises RuntimeError on a failed send (log it and exit non-zero,
    no run file).
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


def main() -> None:
    print("Example script ran OK")


if __name__ == "__main__":
    main()
