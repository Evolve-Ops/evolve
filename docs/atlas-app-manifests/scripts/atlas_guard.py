#!/usr/bin/env python3
"""Atlas Guard — sender + context classification CLI.

Invoked by the bot at the top of every incoming-Telegram-event handler:

    atlas_guard.py classify --user-id <ID> --chat-id <ID> --chat-type <TYPE>

Output (single line on stdout):
    <context>;<role>

Where context ∈ {approved_group, dm, foreign_group} and role ∈ {operator, member, stranger}.

Decision matrix the bot encodes (per AGENTS.md guidance):

    approved_group + any role     → process the event normally (capture, research, etc.)
    dm + operator                 → allow research + admin commands
    dm + member                   → allow research; refuse capture (no archive in DM)
    dm + stranger                 → silent ignore (or polite one-liner — config choice)
    foreign_group + any           → silent ignore

The CLI is read-only (no side effects on disk other than membership-cache writes).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas_lib import guard  # noqa: E402


def cmd_classify(args) -> int:
    if not args.user_id or not args.chat_id or not args.chat_type:
        print("error;missing_args")
        return 2
    context, role = guard.classify(
        user_id=args.user_id,
        chat_id=args.chat_id,
        chat_type=args.chat_type,
        bot_id=args.bot_id,
    )
    print(f"{context};{role}")
    return 0


def cmd_show(args) -> int:
    """Print the current operator config (for debugging)."""
    cfg = guard.read_operator_config(args.bot_id)
    print(f"operator_telegram_user_id: {cfg.get('operator_telegram_user_id')}")
    print(f"approved_group_chat_ids: {cfg.get('approved_group_chat_ids')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas guard — sender classification")
    parser.add_argument("mode", choices=["classify", "show"])
    parser.add_argument("--user-id", default="")
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--chat-type", default="",
                        choices=["", "private", "group", "supergroup", "channel"])
    parser.add_argument("--bot-id", default=os.environ.get("BOT_ID") or "atlas")
    args = parser.parse_args()

    if args.mode == "classify":
        return cmd_classify(args)
    if args.mode == "show":
        return cmd_show(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
