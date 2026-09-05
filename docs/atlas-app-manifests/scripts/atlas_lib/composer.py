"""Atlas — Team_bot_a-style digest + recap composition.

Both digest and recap output are bounded by detail mode:
- concise:  max 3 items per bucket, no snippets
- standard: max 5 items per bucket, one-line snippets
- detailed: max 8 items per bucket, two-line snippets

Never quote community members. Never label items 'CRITICAL' unless they're literally
about a security incident (and even then, the warning bucket emoji is enough).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from atlas_lib import BUCKETS, BUCKET_EMOJI

BUCKET_LABEL = {
    "competitive_landscape": "Competitive landscape",
    "new_tools": "New tools",
    "use_cases": "Use cases",
    "case_studies": "Case studies",
    "warnings": "Warnings",
}

ITEMS_PER_BUCKET = {"concise": 3, "standard": 5, "detailed": 8}
SNIPPET_LINES = {"concise": 0, "standard": 1, "detailed": 2}
MAX_LINE_CHARS = 220


def _short_link(url: str, max_len: int = 60) -> str:
    if not url:
        return ""
    if len(url) <= max_len:
        return url
    return url[:max_len - 1] + "…"


def _truncate(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _group_by_bucket(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        b = item.get("bucket")
        if b in BUCKETS:
            out[b].append(item)
    return out


def compose_digest(items: list[dict], detail: str, today: datetime) -> str:
    """Compose a daily digest from a list of newly-archived items."""
    detail = detail if detail in ITEMS_PER_BUCKET else "standard"
    by_bucket = _group_by_bucket(items)
    total = sum(len(v) for v in by_bucket.values())
    if total == 0:
        return ""  # caller will emit DIGEST_EMPTY:

    weekday = today.strftime("%A")
    date = today.strftime("%Y-%m-%d")
    lines = [
        f"📰 Atlas digest — {weekday}, {date}",
        f"{total} item{'s' if total != 1 else ''} today.",
        "",
    ]

    cap = ITEMS_PER_BUCKET[detail]
    snip_lines = SNIPPET_LINES[detail]

    for bucket in BUCKETS:
        bucket_items = by_bucket.get(bucket, [])
        if not bucket_items:
            continue
        lines.append(f"{BUCKET_EMOJI[bucket]} {BUCKET_LABEL[bucket]}")
        for item in bucket_items[:cap]:
            title = _truncate(item.get("title", ""), 120)
            source = item.get("source", "")
            url = _short_link(item.get("url", ""))
            if snip_lines == 0:
                lines.append(_truncate(f"• {title} — {url}", MAX_LINE_CHARS))
            else:
                lines.append(_truncate(f"• {title} ({source})", MAX_LINE_CHARS))
                snippet = item.get("summary") or item.get("snippet") or ""
                if snippet and snip_lines >= 1:
                    lines.append(_truncate(f"  {snippet}", MAX_LINE_CHARS))
                lines.append(f"  {url}")
        extra = len(bucket_items) - cap
        if extra > 0:
            lines.append(f"  (+{extra} more — see archive)")
        lines.append("")

    lines.append("Reply with /ask <query> for focused research, or /optout <link> to remove a captured item.")
    return "\n".join(lines).rstrip() + "\n"


def compose_recap(items: list[dict], patterns: list[str], detail: str,
                  week_start: datetime, week_end: datetime) -> str:
    """Compose a weekly recap from the past week's archive entries."""
    detail = detail if detail in ITEMS_PER_BUCKET else "standard"
    by_bucket = _group_by_bucket(items)
    total = sum(len(v) for v in by_bucket.values())
    iso_week = f"{week_start.year}-W{week_start.isocalendar().week:02d}"

    lines = [
        f"📰 Atlas weekly recap — week of {week_start.strftime('%b %-d')} to {week_end.strftime('%b %-d')} ({iso_week})",
        f"{total} item{'s' if total != 1 else ''} archived this week.",
        "",
        "🧭 Patterns this week",
    ]
    if patterns:
        for p in patterns[:3]:
            lines.append(f"• {_truncate(p, MAX_LINE_CHARS)}")
    else:
        lines.append("• Quiet week — nothing emergent surfaced.")
    lines.append("")

    cap = ITEMS_PER_BUCKET[detail]
    for bucket in BUCKETS:
        bucket_items = by_bucket.get(bucket, [])
        if not bucket_items:
            continue
        lines.append(f"{BUCKET_EMOJI[bucket]} {BUCKET_LABEL[bucket]} (top {min(cap, len(bucket_items))})")
        for item in bucket_items[:cap]:
            title = _truncate(item.get("title", ""), 120)
            source = item.get("source", "")
            url = _short_link(item.get("url", ""))
            lines.append(_truncate(f"• {title} ({source}) — {url}", MAX_LINE_CHARS))
        lines.append("")

    lines.append("— Full archive available via /ask, or /optout <link> to remove an item.")
    return "\n".join(lines).rstrip() + "\n"
