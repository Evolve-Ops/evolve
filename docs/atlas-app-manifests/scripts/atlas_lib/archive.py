"""Atlas — archive read/write.

Layout (relative to bot workspace):
    archive/
      {bucket}/{YYYY-MM-DD}-{slug}.md   ← one item per file
      index.json                         ← append-only index (list of entries)

Index entry shape:
    {
      "id": "<stable slug>",
      "captured_at": "<ISO datetime>",
      "captured_by": "app_atlas_daily_digest" | "app_atlas_article_capture",
      "bucket": "competitive_landscape" | ...,
      "source": "<source name>",
      "title": "<headline>",
      "url": "<original url>",
      "summary": "<short summary>",
      "member_id_hash": "<hash or null>",  ← null for digest-captured items
      "telegram_message_id": "<id or null>"
    }

All writes are atomic via temp-file + os.replace.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from atlas_lib import BUCKETS
from atlas_lib.config import write_json_atomic, read_json


def _log(msg: str) -> None:
    print(f"[atlas:archive] {msg}", file=sys.stderr)


def _slug(title: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", (title or "").lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:max_len].rstrip("-") or "untitled"


def stable_id(url: str, captured_at: str) -> str:
    """Deterministic ID from URL + capture date (so same URL on same day = same ID)."""
    date = (captured_at or datetime.now(timezone.utc).isoformat())[:10]
    h = hashlib.sha1(f"{date}:{url}".encode("utf-8")).hexdigest()[:10]
    return f"{date}-{h}"


def normalize_url(url: str) -> str:
    """Strip tracking params and fragment so dedup is robust."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "ref_src"}
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if k.lower() not in drop]
    new_query = urllib.parse.urlencode(qs)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, ""))


def index_path(archive_dir: Path) -> Path:
    return archive_dir / "index.json"


def read_index(archive_dir: Path) -> list[dict]:
    path = index_path(archive_dir)
    data = read_json(path, default=[])
    if not isinstance(data, list):
        _log(f"index.json malformed (not a list) — treating as empty")
        return []
    return data


def find_duplicate(url: str, index: list[dict]) -> dict | None:
    norm = normalize_url(url)
    if not norm:
        return None
    for entry in index:
        if normalize_url(entry.get("url", "")) == norm:
            return entry
    return None


def write_item(item: dict, archive_dir: Path, captured_by: str,
               member_id_hash: str | None = None,
               telegram_message_id: str | None = None) -> dict:
    """Write one item to archive/{bucket}/<id>.md and append to index.json.

    `item` must have: title, url, source, snippet (or summary), bucket.
    Returns the index entry that was written. Caller should check for duplicates first.
    """
    bucket = item.get("bucket")
    if bucket not in BUCKETS:
        raise ValueError(f"invalid bucket: {bucket!r}")
    url = item.get("url", "")
    title = item.get("title", "untitled")
    summary = item.get("summary") or item.get("snippet") or ""
    source = item.get("source", "unknown")
    captured_at = datetime.now(timezone.utc).isoformat()
    item_id = stable_id(url, captured_at)
    slug = _slug(title)
    md_path = archive_dir / bucket / f"{captured_at[:10]}-{slug}-{item_id[-6:]}.md"

    frontmatter = (
        "---\n"
        f"id: {item_id}\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"source: {json.dumps(source, ensure_ascii=False)}\n"
        f"url: {json.dumps(url, ensure_ascii=False)}\n"
        f"bucket: {bucket}\n"
        f"captured_at: {captured_at}\n"
        f"captured_by: {captured_by}\n"
        f"member_id_hash: {member_id_hash or 'null'}\n"
        f"telegram_message_id: {telegram_message_id or 'null'}\n"
        "---\n\n"
    )
    body = f"# {title}\n\n{summary.strip()}\n\n[Source]({url})\n"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(frontmatter + body, encoding="utf-8")

    entry = {
        "id": item_id,
        "captured_at": captured_at,
        "captured_by": captured_by,
        "bucket": bucket,
        "source": source,
        "title": title,
        "url": url,
        "summary": summary[:500],
        "member_id_hash": member_id_hash,
        "telegram_message_id": telegram_message_id,
        "md_path": str(md_path.relative_to(archive_dir.parent)),
    }
    index = read_index(archive_dir)
    index.append(entry)
    write_json_atomic(index_path(archive_dir), index)
    return entry


def delete_by_url(url: str, archive_dir: Path) -> list[dict]:
    """Delete every archive entry whose URL matches (after normalization). Returns deleted entries."""
    norm = normalize_url(url)
    if not norm:
        return []
    index = read_index(archive_dir)
    deleted = []
    keep = []
    for entry in index:
        if normalize_url(entry.get("url", "")) == norm:
            md_path = archive_dir.parent / entry.get("md_path", "")
            try:
                md_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log(f"unlink failed for {md_path}: {exc}")
            deleted.append(entry)
        else:
            keep.append(entry)
    if deleted:
        write_json_atomic(index_path(archive_dir), keep)
    return deleted


def delete_by_member(member_id_hash: str, archive_dir: Path) -> list[dict]:
    """Delete every archive entry associated with a hashed member ID."""
    if not member_id_hash:
        return []
    index = read_index(archive_dir)
    deleted = []
    keep = []
    for entry in index:
        if entry.get("member_id_hash") == member_id_hash:
            md_path = archive_dir.parent / entry.get("md_path", "")
            try:
                md_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log(f"unlink failed for {md_path}: {exc}")
            deleted.append(entry)
        else:
            keep.append(entry)
    if deleted:
        write_json_atomic(index_path(archive_dir), keep)
    return deleted


def items_in_range(start: datetime, end: datetime, archive_dir: Path) -> list[dict]:
    """Return index entries with captured_at in [start, end)."""
    index = read_index(archive_dir)
    out = []
    for entry in index:
        try:
            ts = datetime.fromisoformat(entry["captured_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if start <= ts < end:
            out.append(entry)
    return out
