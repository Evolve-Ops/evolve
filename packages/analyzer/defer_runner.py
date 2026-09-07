#!/usr/bin/env python3
"""
defer_runner.py — Continuity Engine v2: due-row poller.

Reads each bot's defer-queue.jsonl, fires due rows by dispatching `openclaw
agent` against the row's session, and rotates fired rows to defer-archive.jsonl.

Runs as the `evolve` user (launchd plist). The runner relies on the existing
read+write ACL grant on each bot's `~/.openclaw/workspace/evolve/` set by
deploy.py's set_evolve_read_acl().

Both message-mode and action-mode rows fire through openclaw agent dispatch:
  - mode=message: agent is told "deliver this exact text to the user"
  - mode=action:  agent is told "do this, then reply to the user"

This is intentionally uniform — one code path, one failure surface, no
direct channel-send plumbing needed for v1. Cost is one cheap agent turn
per fire, which is what we'd pay for action-mode anyway.

Usage:
    defer_runner.py [--network NETWORK_JSON] [--dry-run] [--once]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from defer_queue import (
    DeferRow,
    MODE_ACTION,
    MODE_MESSAGE,
    STATUS_FAILED,
    STATUS_FIRED,
    append_archive,
    iter_bot_queues,
    rewrite_queue,
)
from evolve_config import is_bot_module_enabled, resolve_network_path


# 120s, not 30s. The dispatched agent run takes 25-40s in normal conditions
# (plugin staging ~6s, model resolution ~3s, model call ~15-25s, deliver ~3s).
# The CLI process must stay alive for the entire chain — if we SIGTERM the
# CLI before it acknowledges --deliver, the gateway aborts the channel send
# even though the agent run itself completed. Discovered 2026-05-06: agent
# turns completed at 00:14:36 and 00:17:05 server-side, but the runner had
# already timed out at 00:14:30 and 00:17:00 — Telegram never received the
# fired messages because the CLI's WebSocket connection died before --deliver.
# 120s also fits within the 2-min runner cycle, so a stuck dispatch can't
# block the next cycle.
DISPATCH_TIMEOUT_SEC = 120


def _bot_ids(network_config: dict) -> list[str]:
    """Enumerate bot ids declared in network.json. Network.json is the
    single source of truth — no /Users/ scans, no hardcoded names."""
    bots = network_config.get("bots") or {}
    if isinstance(bots, dict):
        return sorted(bots.keys())
    if isinstance(bots, list):
        return sorted(b.get("id") or b.get("bot_id") for b in bots if isinstance(b, dict))
    return []


def _build_dispatch_message(row: DeferRow) -> str:
    """Wrap the row payload in a system-initiated framing so the agent
    understands this is a follow-up to its earlier commitment.

    XML-tagged delimiters help the model treat the inner content as data,
    not instructions. For message mode, we want the agent to output the
    inner text verbatim; for action mode, we want the agent to act on it
    and reply.
    """
    if row.mode == MODE_MESSAGE:
        return (
            "[SYSTEM_DEFER_FIRE — system-initiated follow-up]\n"
            "You scheduled this earlier with the defer tool. "
            "Output ONLY the text inside <deliver> below, with no greeting, "
            "preamble, framing, or commentary. Do not say 'Sure' or "
            "'Here is the follow-up' — just the text itself.\n\n"
            f"<deliver>\n{row.message}\n</deliver>"
        )
    return (
        "[SYSTEM_DEFER_FIRE — system-initiated follow-up]\n"
        "You scheduled this earlier with the defer tool. The user is "
        "expecting a follow-up message about the action below. Complete "
        "the action and reply directly to the user with the result. "
        "Reference what you committed to so the user knows this is the "
        "promised follow-up, not a fresh interaction.\n\n"
        f"<action>\n{row.action}\n</action>"
    )


def dispatch_row(row: DeferRow, *, dry_run: bool = False) -> tuple[bool, str]:
    """Fire one row via `openclaw agent --session-id ... --message ...`.
    Returns (ok, info)."""
    # The CLI's --session-id flag wants the ephemeral session UUID, NOT the
    # routing-style session key. Caught on first integration test on
    # 2026-05-05: passing the key produced "Invalid session ID:
    # agent:main:telegram:direct:123456789" from the gateway. session_key
    # is kept on the row for diagnostics but isn't used here.
    if not row.session_id:
        return False, "no session_id on row — cannot dispatch (need OC session UUID)"

    message = _build_dispatch_message(row)
    if dry_run:
        return True, f"[dry-run] would dispatch to {row.session_id}: {message[:60]}"

    # Dispatch must run as the bot user, not as the runner's user (root).
    # `openclaw agent` connects to the bot's gateway and reads gateway-auth
    # tokens from the user's ~/.openclaw config — invoked as root, there's
    # no token, and the gateway rejects with "unauthorized: gateway token
    # missing". Per CLAUDE.md the evolve user cannot sudo to bot users, so
    # the runner runs as root and uses `sudo -H -u <bot>` from there.
    #
    # The -H flag is REQUIRED: without it, sudo preserves the invoking
    # user's HOME, and openclaw resolves its config/state path from HOME.
    # The runner runs as root so without -H openclaw reads /var/root/...
    # rather than the target bot's session store, and rejects every session
    # id as "Invalid session ID". Discovered on 2026-05-05.
    #
    # cwd=/tmp because subprocess inherits the parent CWD by default, and
    # the bot user may not have read access to wherever the runner is
    # spawned from (Python's path importer would EACCES on import). Same
    # reasoning as set out in CLAUDE.md for `sudo -u evolve python` calls.
    #
    # --deliver is critical: without it, openclaw agent runs the turn but
    # does NOT send the result to the user's channel (default false per
    # `openclaw agent --help`). The whole point of a defer firing is to
    # deliver a follow-up the user sees.
    cmd = [
        "sudo", "-H", "-u", row.bot_id,
        "openclaw", "agent",
        "--session-id", row.session_id,
        "--message", message,
        "--deliver",
        "--json",
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DISPATCH_TIMEOUT_SEC,
            cwd="/tmp",
        )
    except subprocess.TimeoutExpired:
        return False, f"openclaw agent timed out after {DISPATCH_TIMEOUT_SEC}s"
    except Exception as e:
        return False, f"openclaw agent error: {e}"

    if r.returncode != 0:
        # Bumped from 120 to 400 chars so the full session-id / auth error
        # message is captured in the archive instead of being truncated.
        err = r.stderr.strip() or r.stdout.strip()
        return False, f"openclaw agent failed (rc={r.returncode}): {err[:400]}"
    return True, f"dispatched ({len(r.stdout)} bytes stdout)"


def process_bot(bot_id: str, rows: list[DeferRow], *, dry_run: bool = False) -> dict:
    """Process one bot's rows. Returns summary counts."""
    summary = {"checked": len(rows), "fired": 0, "failed": 0, "kept": 0}
    now = datetime.now(timezone.utc)

    keep: list[DeferRow] = []
    for row in rows:
        if not row.is_due(now):
            keep.append(row)
            summary["kept"] += 1
            continue

        ok, info = dispatch_row(row, dry_run=dry_run)
        row.fired_at = now.isoformat(timespec="seconds")
        row.result = info
        if ok:
            row.status = STATUS_FIRED
            summary["fired"] += 1
            print(f"[defer_runner] ✓ {bot_id} {row.defer_id[:8]} fired — {info}")
        else:
            row.status = STATUS_FAILED
            summary["failed"] += 1
            print(f"[defer_runner] ✗ {bot_id} {row.defer_id[:8]} failed — {info}")

        if not dry_run:
            append_archive(bot_id, row)

    if not dry_run and (summary["fired"] or summary["failed"]):
        # Rewrite the active queue to drop the rows we archived.
        rewrite_queue(bot_id, keep)

    return summary


def run_once(network_config: dict, *, dry_run: bool = False) -> dict:
    """One full cycle across all bots in the network."""
    bot_ids = _bot_ids(network_config)
    totals = {"bots": 0, "checked": 0, "fired": 0, "failed": 0, "kept": 0, "skipped_disabled": 0}

    for bot_id, rows in iter_bot_queues(bot_ids):
        if not rows:
            continue
        # Per-bot opt-out: when continuity_engine is disabled for this bot,
        # leave the queue intact so anything queued before disable still
        # fires once re-enabled. We just don't dispatch this cycle.
        if not is_bot_module_enabled(network_config, bot_id, "continuity_engine", default=True):
            print(f"[defer_runner] - {bot_id} continuity_engine disabled — skipping {len(rows)} queued row(s)")
            totals["skipped_disabled"] += len(rows)
            continue
        s = process_bot(bot_id, rows, dry_run=dry_run)
        totals["bots"] += 1
        for k in ("checked", "fired", "failed", "kept"):
            totals[k] += s[k]

    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuity Engine v2 — defer queue poller")
    parser.add_argument("--network", help="Path to network.json")
    parser.add_argument("--dry-run", action="store_true", help="Don't dispatch or rewrite files")
    parser.add_argument("--once", action="store_true", default=True,
                        help="Run one cycle and exit (default; loop mode is launchd's job)")
    args = parser.parse_args()

    network_path = Path(args.network) if args.network else resolve_network_path()
    try:
        network_config = json.loads(Path(network_path).read_text())
    except Exception as e:
        print(f"[defer_runner] could not load network.json from {network_path}: {e}", file=sys.stderr)
        sys.exit(2)

    totals = run_once(network_config, dry_run=args.dry_run)
    print(f"[defer_runner] cycle complete: {totals}")


if __name__ == "__main__":
    main()
