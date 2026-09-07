"""Atlas — direct Telegram Bot API client.

Used because the bot's gateway routes to the bot's "default" channel; Atlas
needs to post to a SPECIFIC group chat (and react to specific messages), so
we call Telegram's API directly using the bot token from skills/telegram.json.

Methods:
- send_message(token, chat_id, text, reply_to_message_id=None, parse_mode=None)
- set_message_reaction(token, chat_id, message_id, emoji)
- pin_message(token, chat_id, message_id)
- get_me(token)

All methods return the Telegram response dict on success, or {} on failure.
Errors are logged to stderr.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _log(msg: str) -> None:
    print(f"[atlas:telegram] {msg}", file=sys.stderr)


def _call(token: str, method: str, payload: dict, timeout: int = 15) -> dict:
    if not token:
        _log(f"{method}: no token")
        return {}
    url = API_BASE.format(token=token, method=method)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
            _log(f"{method} HTTP {exc.code}: {err.get('description', '')}")
        except Exception:
            _log(f"{method} HTTP {exc.code}")
        return {}
    except (urllib.error.URLError, TimeoutError) as exc:
        _log(f"{method} unreachable: {exc}")
        return {}
    except json.JSONDecodeError:
        _log(f"{method} non-json response")
        return {}
    if not data.get("ok"):
        _log(f"{method} not ok: {data.get('description', '')}")
        return {}
    return data.get("result", {})


def get_me(token: str) -> dict:
    return _call(token, "getMe", {})


def send_message(token: str, chat_id, text: str,
                 reply_to_message_id=None, parse_mode: str | None = None) -> dict:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _call(token, "sendMessage", payload)


def set_message_reaction(token: str, chat_id, message_id, emoji: str) -> dict:
    """Set a single emoji reaction on a message. Replaces any existing reactions by the bot.

    Requires Telegram Bot API 7.0+ (setMessageReaction).
    """
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": [{"type": "emoji", "emoji": emoji}],
        "is_big": False,
    }
    return _call(token, "setMessageReaction", payload)


def pin_message(token: str, chat_id, message_id, disable_notification: bool = True) -> dict:
    return _call(token, "pinChatMessage", {
        "chat_id": chat_id,
        "message_id": message_id,
        "disable_notification": disable_notification,
    })


def get_chat_member(token: str, chat_id, user_id) -> dict:
    """Look up a user's membership status in a chat.

    Returns a dict like {"status": "member" | "administrator" | "creator" | "restricted" | "left" | "kicked", ...}
    or {} on error.

    Used by atlas_lib.guard to verify a DM sender is in one of the approved groups.
    """
    return _call(token, "getChatMember", {"chat_id": chat_id, "user_id": user_id})
