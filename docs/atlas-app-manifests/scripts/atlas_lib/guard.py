"""Atlas — sender + context classification.

Reads atlas/operator.json:
    {
      "operator_telegram_user_id": <int>,
      "approved_group_chat_ids": [<int>, ...]
    }

Exposes:
    classify(user_id, chat_id, chat_type, *, bot_id) → (context, role)

where:
    context ∈ {"approved_group", "dm", "foreign_group"}
    role    ∈ {"operator", "member", "stranger"}

Decision matrix the bot uses (encoded in AGENTS.md guidance):

|        | operator | member | stranger |
|--------|----------|--------|----------|
| approved_group | full   | full   | full     |   (group membership is the gate; in-group everyone is trusted)
| dm             | admin+ | research only | ignored |
| foreign_group  | ignored| ignored| ignored  |   (atlas added to a non-configured group)

Membership checks use Telegram's getChatMember (one API call per DM event from an
unknown user). The result is cached for `MEMBERSHIP_CACHE_TTL_SEC` to avoid
re-checking on every burst of DMs.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from atlas_lib import config as cfg
from atlas_lib import telegram_api as tg

MEMBERSHIP_CACHE_TTL_SEC = 300  # 5 minutes
_NON_MEMBER_STATUSES = {"left", "kicked"}


def _log(msg: str) -> None:
    print(f"[atlas:guard] {msg}", file=sys.stderr)


def _operator_config_path(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "atlas" / "operator.json"


def _membership_cache_path(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "atlas" / ".membership-cache.json"


def read_operator_config(bot_id: str) -> dict:
    return cfg.read_json(_operator_config_path(bot_id), default={
        "operator_telegram_user_id": 0,
        "approved_group_chat_ids": [],
    })


def _read_cache(bot_id: str) -> dict:
    return cfg.read_json(_membership_cache_path(bot_id), default={})


def _write_cache(bot_id: str, cache: dict) -> None:
    try:
        cfg.write_json_atomic(_membership_cache_path(bot_id), cache)
    except OSError as exc:
        _log(f"cache write failed: {exc}")


def _cache_key(user_id, chat_id) -> str:
    return f"{chat_id}:{user_id}"


def _cached_is_member(bot_id: str, user_id, chat_id) -> bool | None:
    cache = _read_cache(bot_id)
    entry = cache.get(_cache_key(user_id, chat_id))
    if not entry:
        return None
    if time.time() - entry.get("checked_at", 0) > MEMBERSHIP_CACHE_TTL_SEC:
        return None
    return bool(entry.get("is_member"))


def _cache_membership(bot_id: str, user_id, chat_id, is_member: bool) -> None:
    cache = _read_cache(bot_id)
    cache[_cache_key(user_id, chat_id)] = {
        "is_member": is_member,
        "checked_at": time.time(),
    }
    # Lightweight pruning: drop any entries older than 1h
    cutoff = time.time() - 3600
    cache = {k: v for k, v in cache.items() if v.get("checked_at", 0) > cutoff}
    _write_cache(bot_id, cache)


def _check_membership(bot_id: str, user_id, chat_id) -> bool:
    """Live check via Telegram getChatMember. Caches result."""
    cached = _cached_is_member(bot_id, user_id, chat_id)
    if cached is not None:
        return cached
    token = cfg.telegram_token(bot_id)
    if not token:
        _log("no telegram token — cannot verify membership; treating as non-member")
        return False
    result = tg.get_chat_member(token, chat_id, user_id)
    status = result.get("status", "")
    is_member = bool(status) and status not in _NON_MEMBER_STATUSES
    _cache_membership(bot_id, user_id, chat_id, is_member)
    return is_member


def classify(user_id, chat_id, chat_type: str, *, bot_id: str) -> tuple[str, str]:
    """Return (context, role).

    user_id, chat_id may be int or str (we accept either).
    chat_type is Telegram's chat.type: "private" | "group" | "supergroup" | "channel".
    """
    op_cfg = read_operator_config(bot_id)
    operator_id = op_cfg.get("operator_telegram_user_id", 0)
    approved = set(op_cfg.get("approved_group_chat_ids", []))

    # Normalize comparison (operator_id is int in config; user_id may be str)
    try:
        user_id_int = int(user_id) if user_id is not None else 0
    except (TypeError, ValueError):
        user_id_int = 0
    try:
        chat_id_int = int(chat_id) if chat_id is not None else 0
    except (TypeError, ValueError):
        chat_id_int = 0

    is_operator = (operator_id != 0 and user_id_int == operator_id)

    if chat_type in ("group", "supergroup"):
        context = "approved_group" if chat_id_int in approved else "foreign_group"
        if is_operator:
            role = "operator"
        else:
            # In an approved group, everyone is a "member" (no need for getChatMember
            # — the bot is seeing the message, which means the user is in the group).
            role = "member" if context == "approved_group" else "stranger"
        return context, role

    if chat_type == "private":
        # DM. Verify membership in any approved group.
        if is_operator:
            return "dm", "operator"
        for gid in approved:
            if _check_membership(bot_id, user_id_int, gid):
                return "dm", "member"
        return "dm", "stranger"

    # channels and unexpected types — treat as foreign / stranger
    return "foreign_group", "stranger"
