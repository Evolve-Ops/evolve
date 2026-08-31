#!/usr/bin/env python3
"""Atlas Weekly Recap — Sunday synthesis of the past week's archive.

Usage:
    atlas_recap.py send    [--bot-id ID] [--chat-id ID] [--week ISO_WEEK] [--detail ...]
    atlas_recap.py preview [--bot-id ID] [--week ISO_WEEK] [--detail ...]
    atlas_recap.py status  [--bot-id ID]

Pipeline:
1. Read archive/index.json
2. Filter to entries in the target week (default: previous 7 days)
3. Exclude items whose URL is in optout.json
4. Group by bucket; score each item; take top N per bucket
5. Pattern-detection pass (Haiku, ~5000 token budget) — surfaces 1-3 cross-item patterns
6. Compose Team_bot_a-style recap via composer.compose_recap
7. Post to Telegram, write recap/{YYYY-WW}.md, emit RECAP_SENT

Signals:
    RECAP_SENT:    <iso_week> <total_items>
    RECAP_FAILED:  <iso_week> <reason>
    RECAP_EMPTY:   <iso_week>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas_lib import BUCKETS  # noqa: E402
from atlas_lib import archive as arch  # noqa: E402
from atlas_lib import composer  # noqa: E402
from atlas_lib import config as cfg  # noqa: E402
from atlas_lib import oc_dispatch  # noqa: E402
from atlas_lib import telegram_api as tg  # noqa: E402

APP_ID = "app_atlas_weekly_recap"

SOURCE_WEIGHTS = {
    "anthropic-blog": 1.0, "openai-blog": 1.0, "google-blog-ai": 1.0,
    "openclaw-blog": 1.2, "hn-frontpage": 0.8,
    "r-LocalLLaMA": 0.7,
    "telegram-member": 0.7,
}


def _log(msg: str) -> None:
    print(f"[atlas_recap] {msg}", file=sys.stderr)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atlas_dir(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "atlas"


def _archive_dir(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "archive"


def _recap_dir(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "recap"


def _optout_path(bot_id: str) -> Path:
    return _atlas_dir(bot_id) / "optout.json"


def _week_range(week: str | None, now: datetime) -> tuple[datetime, datetime, str]:
    """Resolve a week argument to (start, end, iso_week_label).

    week is ISO_YEAR-WNN, or None (=> previous 7 days ending now).
    """
    if not week:
        end = now
        start = end - timedelta(days=7)
        iso = f"{end.isocalendar().year}-W{end.isocalendar().week:02d}"
        return start, end, iso
    try:
        year_s, wk_s = week.split("-W")
        year = int(year_s)
        wk = int(wk_s)
    except ValueError:
        raise SystemExit(f"invalid --week: {week!r}, expected YYYY-WNN")
    # Monday of the requested ISO week (ISO weekday 1)
    start = datetime.fromisocalendar(year, wk, 1).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    return start, end, f"{year}-W{wk:02d}"


def _source_weight(source: str) -> float:
    """Match by prefix (e.g. 'brave-search:openclaw case study' → 0.6)."""
    if not source:
        return 0.7
    for prefix, weight in SOURCE_WEIGHTS.items():
        if source.startswith(prefix):
            return weight
    if source.startswith("github-release:"):
        return 0.9
    if source.startswith("brave-search:"):
        return 0.6
    return 0.7


def _domain_key(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def _score(item: dict, all_items: list[dict], now: datetime) -> float:
    try:
        ts = datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        ts = now
    age_days = max(0, (now - ts).days)
    recency = max(0.4, 1.0 - 0.1 * age_days)
    sw = _source_weight(item.get("source", ""))
    # Duplicate signal: how many other items in the same bucket reference the same domain?
    my_domain = _domain_key(item.get("url", ""))
    similar = sum(
        1 for o in all_items
        if o.get("bucket") == item.get("bucket") and _domain_key(o.get("url", "")) == my_domain
    )
    dup_signal = 1.0 + 0.2 * (similar - 1)
    return sw * recency * dup_signal


def _filter_optout(items: list[dict], optout: dict) -> list[dict]:
    if not optout:
        return items
    blocked_urls = {
        arch.normalize_url(entry.get("url", ""))
        for entry in optout.values()
        if entry.get("type") == "url"
    }
    if not blocked_urls:
        return items
    return [i for i in items if arch.normalize_url(i.get("url", "")) not in blocked_urls]


PATTERN_PROMPT = """You are looking at the past week of items archived by Atlas, an OpenClaw / AI-agent ecosystem digest bot. Identify 1-3 patterns visible ACROSS items — themes that connect multiple items, not commentary on any single one.

Items this week (title, bucket, source):

{items}

Rules:
- Patterns are OBSERVATIONS, not recommendations.
- If you see fewer than 2 items supporting a pattern, don't list it.
- If you genuinely see nothing emergent, return {{"patterns": [], "confidence": 0.0}}.

Return JSON only: {{"patterns": ["...", "..."], "confidence": 0.0-1.0}}

If confidence < 0.6, return patterns: [].
"""


def _format_items_for_patterns(items: list[dict]) -> str:
    lines = []
    for it in items[:30]:
        lines.append(f"- [{it.get('bucket')}] {it.get('title', '')[:120]} ({it.get('source', '')})")
    return "\n".join(lines)


def _detect_patterns(items: list[dict]) -> list[str]:
    """Pattern-detection pass over the week's items via the bot's local OC agent."""
    if len(items) < 3:
        return []
    prompt = PATTERN_PROMPT.format(items=_format_items_for_patterns(items))
    text, tel = oc_dispatch.dispatch(prompt, timeout_s=45)
    if tel["error"]:
        _log(f"pattern detection failed: {tel['error']}")
        return []
    parsed = oc_dispatch.parse_json_reply(text)
    if parsed is None:
        _log(f"pattern detection returned non-json: {text[:200]!r}")
        return []
    confidence = float(parsed.get("confidence", 0.0))
    if confidence < 0.6:
        return []
    return [p for p in parsed.get("patterns", []) if isinstance(p, str)][:3]


def _gather_week(bot_id: str, start: datetime, end: datetime) -> list[dict]:
    items = arch.items_in_range(start, end, _archive_dir(bot_id))
    optout = cfg.read_json(_optout_path(bot_id), default={})
    return _filter_optout(items, optout)


def _rank_per_bucket(items: list[dict], max_items_per_bucket: int,
                     now: datetime) -> list[dict]:
    by_bucket: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for it in items:
        b = it.get("bucket")
        if b in by_bucket:
            by_bucket[b].append(it)
    final = []
    for b in BUCKETS:
        bucket_items = by_bucket[b]
        scored = sorted(bucket_items, key=lambda i: -_score(i, items, now))
        final.extend(scored[:max_items_per_bucket])
    return final


def cmd_send(args) -> int:
    now = _now()
    try:
        start, end, iso_week = _week_range(args.week, now)
    except SystemExit as exc:
        print(f"RECAP_FAILED: {exc}")
        return 2

    week_items = _gather_week(args.bot_id, start, end)
    if len(week_items) < 3:
        print(f"RECAP_EMPTY: {iso_week}")
        return 0

    patterns = _detect_patterns(week_items)
    final = _rank_per_bucket(week_items, args.max_items_per_bucket, now)
    if not final:
        print(f"RECAP_EMPTY: {iso_week}")
        return 0

    recap_text = composer.compose_recap(final, patterns, args.detail, start, end)
    token = cfg.telegram_token(args.bot_id)
    if not token or not args.chat_id:
        print(f"RECAP_FAILED: {iso_week} no_telegram_config")
        return 2
    result = tg.send_message(token, args.chat_id, recap_text)
    if not result:
        print(f"RECAP_FAILED: {iso_week} telegram_send_error")
        return 2

    recap_path = _recap_dir(args.bot_id) / f"{iso_week}.md"
    recap_path.parent.mkdir(parents=True, exist_ok=True)
    posted_at = now.isoformat()
    source_ids = ", ".join(i.get("id", "") for i in final)
    recap_path.write_text(
        f"<!-- posted_at: {posted_at} -->\n\n{recap_text}\n\n---\nsource_archive_ids: {source_ids}\n",
        encoding="utf-8",
    )

    print(f"RECAP_SENT: {iso_week} {len(final)}")
    return 0


def cmd_preview(args) -> int:
    now = _now()
    try:
        start, end, iso_week = _week_range(args.week, now)
    except SystemExit as exc:
        print(f"(invalid week: {exc})")
        return 2
    week_items = _gather_week(args.bot_id, start, end)
    if len(week_items) < 3:
        print(f"({iso_week}: only {len(week_items)} items in week — recap would be suppressed)")
        return 0
    patterns = _detect_patterns(week_items)
    final = _rank_per_bucket(week_items, args.max_items_per_bucket, now)
    print(composer.compose_recap(final, patterns, args.detail, start, end))
    return 0


def cmd_status(args) -> int:
    rd = _recap_dir(args.bot_id)
    if not rd.exists():
        print("no recaps posted yet")
        return 0
    posted = sorted(rd.glob("*.md"))
    if not posted:
        print("no recaps posted yet")
        return 0
    latest = posted[-1]
    first_line = latest.read_text(encoding="utf-8").splitlines()[0]
    print(f"latest recap: {latest.stem} — {first_line}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas weekly recap")
    parser.add_argument("mode", choices=["send", "preview", "status"])
    parser.add_argument("--bot-id", default=os.environ.get("BOT_ID") or "atlas")
    parser.add_argument("--chat-id", default=os.environ.get("ATLAS_CHAT_ID", ""))
    parser.add_argument("--week", default="",
                        help="ISO week (e.g. 2026-W20). Empty = previous 7 days.")
    parser.add_argument("--detail", default="standard", choices=["concise", "standard", "detailed"])
    parser.add_argument("--max-items-per-bucket", type=int, default=5)
    args = parser.parse_args()

    if args.mode == "send":
        return cmd_send(args)
    if args.mode == "preview":
        return cmd_preview(args)
    if args.mode == "status":
        return cmd_status(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
