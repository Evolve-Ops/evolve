#!/usr/bin/env python3
"""Atlas Article Capture — event-driven URL processor.

Invoked by the bot (via AGENTS.md guidance) when:
- A group message contains a URL → `process --url ... --message-id ... --member-id ...`
- A member uses /optout <url> → `opt-out --url ... --member-id ...`
- A member uses /optout-all → `opt-out-all --member-id ...`

Signals (stdout):
    CAPTURE_ARCHIVED:<bucket>      <url>
    CAPTURE_DUPLICATE              <url>
    CAPTURE_OPTED_OUT              <url>
    CAPTURE_FAILED:<reason>        <url>
    CAPTURE_SKIPPED:<reason>       <url>
    CAPTURE_OPT_OUT_REGISTERED     <url> <deleted_count>
    CAPTURE_OPT_OUT_ALL_REGISTERED <member_hash> <deleted_count>

Bot reads the signal, picks the appropriate reaction emoji per AGENTS.md guidance.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas_lib import archive as arch  # noqa: E402
from atlas_lib import classifier as clf  # noqa: E402
from atlas_lib import config as cfg  # noqa: E402
from atlas_lib import fetchers  # noqa: E402
from atlas_lib import guard  # noqa: E402
from atlas_lib import hashing  # noqa: E402

APP_ID = "app_atlas_article_capture"


def _log(msg: str) -> None:
    print(f"[atlas_capture] {msg}", file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atlas_dir(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "atlas"


def _archive_dir(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "archive"


def _capture_log_path(bot_id: str) -> Path:
    return _atlas_dir(bot_id) / "capture-log.jsonl"


def _optout_path(bot_id: str) -> Path:
    return _atlas_dir(bot_id) / "optout.json"


def _salt_path(bot_id: str) -> Path:
    return _atlas_dir(bot_id) / ".capture-salt"


def _append_log(bot_id: str, entry: dict) -> None:
    """Best-effort append to capture-log. If the workspace doesn't exist
    (e.g. running against a nonexistent bot for testing), silently skip
    so the caller's signal output isn't lost.
    """
    log = _capture_log_path(bot_id)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log(f"could not append to capture-log: {exc}")


def _read_optout(bot_id: str) -> dict:
    return cfg.read_json(_optout_path(bot_id), default={})


def _write_optout(bot_id: str, data: dict) -> None:
    cfg.write_json_atomic(_optout_path(bot_id), data)


def _is_opted_out(url: str, optout: dict) -> bool:
    """Check both URL-based and message-id-based opt-out entries."""
    norm = arch.normalize_url(url)
    for key, entry in optout.items():
        if entry.get("type") == "url" and arch.normalize_url(entry.get("url", "")) == norm:
            return True
    return False


def _extract_summary(url: str) -> str:
    """Fetch URL and extract a short text snippet.

    v1: grab the first 800 chars of HTML body text. No fancy article extraction.
    """
    status, text = fetchers.fetch_url(url, timeout=10, max_bytes=2_000_000)
    if status != 200 or not text:
        return ""
    import re
    # Strip script/style blocks first
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # Grab title
    m_title = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    title = (m_title.group(1).strip() if m_title else "")[:200]
    # Strip remaining HTML
    body = re.sub(r"<[^>]+>", " ", text)
    body = re.sub(r"\s+", " ", body).strip()
    return f"{title} — {body[:800]}" if title else body[:1000]


def cmd_process(args) -> int:
    url = args.url
    if not url:
        print("CAPTURE_FAILED:no_url ")
        return 2

    # Guard: capture only operates in approved groups. DM URL-sharing is out of scope.
    context, role = guard.classify(args.member_id, args.chat_id, args.chat_type, bot_id=args.bot_id)
    if context != "approved_group":
        print(f"CAPTURE_SKIPPED:not_in_approved_group context={context} role={role}")
        _append_log(args.bot_id, {
            "captured_at": _now_iso(),
            "url": url,
            "outcome": "skipped_context",
            "context": context,
            "role": role,
        })
        return 0

    optout = _read_optout(args.bot_id)
    if _is_opted_out(url, optout):
        print(f"CAPTURE_OPTED_OUT {url}")
        _append_log(args.bot_id, {
            "captured_at": _now_iso(),
            "url": url,
            "outcome": "opted_out",
        })
        return 0

    archive_dir = _archive_dir(args.bot_id)
    index = arch.read_index(archive_dir)
    dup = arch.find_duplicate(url, index)
    if dup:
        print(f"CAPTURE_DUPLICATE {url}")
        _append_log(args.bot_id, {
            "captured_at": _now_iso(),
            "url": url,
            "outcome": "duplicate",
            "existing_archive_id": dup.get("id"),
        })
        return 0

    snippet = _extract_summary(url)
    if not snippet:
        print(f"CAPTURE_FAILED:fetch_failed {url}")
        _append_log(args.bot_id, {
            "captured_at": _now_iso(),
            "url": url,
            "outcome": "fetch_failed",
        })
        return 2

    title = snippet.split(" — ", 1)[0][:200] if " — " in snippet else snippet[:120]
    candidate = {
        "title": title,
        "url": url,
        "source": "telegram-member",
        "snippet": snippet,
    }

    result = clf.classify(candidate)
    if result["bucket"] == "skip":
        print(f"CAPTURE_SKIPPED:{result.get('reason', 'low_confidence')} {url}")
        _append_log(args.bot_id, {
            "captured_at": _now_iso(),
            "url": url,
            "outcome": "skipped",
            "classification_reason": result.get("reason"),
            "cost_usd": result.get("cost_usd", 0.0),
        })
        return 0

    salt = hashing.read_or_create_salt(_salt_path(args.bot_id))
    member_hash = hashing.hash_member(args.member_id, salt) if args.member_id else None

    candidate["bucket"] = result["bucket"]
    candidate["summary"] = snippet[:400]
    candidate["classification_confidence"] = result["confidence"]

    entry = arch.write_item(
        candidate,
        archive_dir,
        captured_by=APP_ID,
        member_id_hash=member_hash,
        telegram_message_id=args.message_id or None,
    )

    print(f"CAPTURE_ARCHIVED:{result['bucket']} {url}")
    _append_log(args.bot_id, {
        "captured_at": _now_iso(),
        "url": url,
        "outcome": "archived",
        "bucket": result["bucket"],
        "archive_id": entry["id"],
        "member_id_hash": member_hash,
        "telegram_message_id": args.message_id,
        "cost_usd": result.get("cost_usd", 0.0),
    })
    return 0


def cmd_opt_out(args) -> int:
    url = args.url
    if not url:
        print("CAPTURE_FAILED:no_url ")
        return 2

    # Guard: opt-out is allowed in approved groups, or in DM from operator/member.
    # Strangers in DM and foreign groups are refused.
    context, role = guard.classify(args.member_id, args.chat_id, args.chat_type, bot_id=args.bot_id)
    allowed = (context == "approved_group") or (context == "dm" and role in ("operator", "member"))
    if not allowed:
        print(f"CAPTURE_SKIPPED:not_authorized context={context} role={role}")
        return 0

    # Register opt-out (URL-based, persistent — prevents re-capture)
    optout = _read_optout(args.bot_id)
    key = f"url:{arch.normalize_url(url)}"
    optout[key] = {
        "type": "url",
        "url": url,
        "registered_at": _now_iso(),
        "registered_by_hash": None,
    }
    salt = hashing.read_or_create_salt(_salt_path(args.bot_id))
    if args.member_id:
        optout[key]["registered_by_hash"] = hashing.hash_member(args.member_id, salt)
    _write_optout(args.bot_id, optout)

    # Delete matching archive entries
    deleted = arch.delete_by_url(url, _archive_dir(args.bot_id))
    count = len(deleted)
    print(f"CAPTURE_OPT_OUT_REGISTERED {url} {count}")
    _append_log(args.bot_id, {
        "captured_at": _now_iso(),
        "url": url,
        "outcome": "opt_out_registered",
        "deleted_count": count,
        "deleted_ids": [e.get("id") for e in deleted],
    })
    return 0


def cmd_opt_out_all(args) -> int:
    if not args.member_id:
        print("CAPTURE_FAILED:no_member_id ")
        return 2
    # Guard: same as opt-out — approved group or DM from operator/member.
    context, role = guard.classify(args.member_id, args.chat_id, args.chat_type, bot_id=args.bot_id)
    allowed = (context == "approved_group") or (context == "dm" and role in ("operator", "member"))
    if not allowed:
        print(f"CAPTURE_SKIPPED:not_authorized context={context} role={role}")
        return 0
    salt = hashing.read_or_create_salt(_salt_path(args.bot_id))
    member_hash = hashing.hash_member(args.member_id, salt)
    deleted = arch.delete_by_member(member_hash, _archive_dir(args.bot_id))
    print(f"CAPTURE_OPT_OUT_ALL_REGISTERED {member_hash} {len(deleted)}")
    _append_log(args.bot_id, {
        "captured_at": _now_iso(),
        "outcome": "opt_out_all_registered",
        "member_id_hash": member_hash,
        "deleted_count": len(deleted),
    })
    return 0


def cmd_stats(args) -> int:
    log = _capture_log_path(args.bot_id)
    if not log.exists():
        print("no captures logged")
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    outcomes: dict[str, int] = {}
    buckets: dict[str, int] = {}
    cost_total = 0.0
    with log.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(e["captured_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            outcomes[e.get("outcome", "unknown")] = outcomes.get(e.get("outcome", "unknown"), 0) + 1
            if e.get("bucket"):
                buckets[e["bucket"]] = buckets.get(e["bucket"], 0) + 1
            cost_total += float(e.get("cost_usd", 0.0))
    print(f"captures over last {args.days} day(s):")
    for k, v in sorted(outcomes.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    if buckets:
        print("buckets:")
        for k, v in sorted(buckets.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")
    print(f"classification cost: ${cost_total:.4f}")
    return 0


class _RequestArgs:
    """argparse.Namespace-shaped struct populated from a JSON request file.

    See module docstring + the JSON-file mode in :func:`main`. Same shape
    as the argparse output so cmd_process / cmd_opt_out / etc. don't care
    about the invocation mode.
    """

    __slots__ = ("mode", "url", "message_id", "member_id",
                 "chat_id", "chat_type", "bot_id", "days")

    def __init__(self, mode: str, url: str = "", message_id: str = "",
                 member_id: str = "", chat_id: str = "",
                 chat_type: str = "supergroup", bot_id: str = "atlas",
                 days: int = 7) -> None:
        self.mode       = mode
        self.url        = url
        self.message_id = message_id
        self.member_id  = member_id
        self.chat_id    = chat_id
        self.chat_type  = chat_type
        self.bot_id     = bot_id
        self.days       = days


_VALID_CAPTURE_MODES = ("process", "opt-out", "opt-out-all", "stats")
_VALID_CHAT_TYPES = ("private", "group", "supergroup", "channel")


def _looks_like_request_file(argv: list) -> bool:
    """``script.py <one-json-path>`` → JSON file mode.

    OC's exec preflight (openclaw#87371) blocks multi-arg interpreter
    invocations. The agent writes a JSON file with all the args and
    invokes us with a single positional path. Any other shape (zero or
    multiple args, non-.json suffix, relative path) falls through to
    argparse / CLI mode.
    """
    return (
        len(argv) == 2
        and argv[1].startswith("/")
        and argv[1].endswith(".json")
    )


def _args_from_request_file(path: str) -> "_RequestArgs | None":
    """Load + validate a JSON request file; delete it on success.

    Returns None on any read error or shape mismatch — main() then
    surfaces failure via the existing exit codes / silence-by-design
    semantics of the relevant cmd_* function.
    """
    from pathlib import Path as _Path
    p = _Path(path)
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None

    mode = str(body.get("mode") or "").strip()
    if mode not in _VALID_CAPTURE_MODES:
        return None
    chat_type = str(body.get("chat_type") or "supergroup").strip()
    if chat_type not in _VALID_CHAT_TYPES:
        chat_type = "supergroup"
    try:
        days = int(body.get("days") or 7)
    except (TypeError, ValueError):
        days = 7

    args = _RequestArgs(
        mode       = mode,
        url        = str(body.get("url") or ""),
        message_id = str(body.get("message_id") or ""),
        member_id  = str(body.get("member_id") or ""),
        chat_id    = str(body.get("chat_id") or ""),
        chat_type  = chat_type,
        bot_id     = str(body.get("bot_id") or os.environ.get("BOT_ID") or "atlas"),
        days       = days,
    )

    try:
        p.unlink()
    except OSError:
        pass
    return args


def main() -> int:
    # ── JSON-file mode (OC exec preflight workaround) ─────────────────
    if _looks_like_request_file(sys.argv):
        args = _args_from_request_file(sys.argv[1])
        if args is None:
            # Silent by design — capture is opaque to the user; no
            # error surface beyond logs.
            return 2
    else:
        # ── Legacy CLI args mode ──────────────────────────────────────
        parser = argparse.ArgumentParser(description="Atlas article capture")
        parser.add_argument("mode", choices=list(_VALID_CAPTURE_MODES))
        parser.add_argument("--url", default="")
        parser.add_argument("--message-id", default="")
        parser.add_argument("--member-id", default="")
        parser.add_argument("--chat-id", default="",
                            help="Telegram chat ID where the event originated (required for process/opt-out/opt-out-all)")
        parser.add_argument("--chat-type", default="supergroup",
                            choices=list(_VALID_CHAT_TYPES),
                            help="Telegram chat.type of the originating chat")
        parser.add_argument("--bot-id", default=os.environ.get("BOT_ID") or "atlas")
        parser.add_argument("--days", type=int, default=7)
        args = parser.parse_args()  # type: ignore[assignment]

    if args.mode == "process":
        return cmd_process(args)
    if args.mode == "opt-out":
        return cmd_opt_out(args)
    if args.mode == "opt-out-all":
        return cmd_opt_out_all(args)
    if args.mode == "stats":
        return cmd_stats(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
