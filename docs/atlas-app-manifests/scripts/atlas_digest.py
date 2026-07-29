#!/usr/bin/env python3
"""Atlas Daily Digest — cron-driven crawler + classifier + Telegram poster.

Usage:
    atlas_digest.py send    [--bot-id ID] [--chat-id ID] [--detail concise|standard|detailed]
    atlas_digest.py preview [--bot-id ID] [--detail ...]
    atlas_digest.py status  [--bot-id ID]

Signals (stdout):
    DIGEST_SENT:    <date> <total_items> <bucket_counts>
    DIGEST_FAILED:  <date> <reason>
    DIGEST_PARTIAL: <date> <which_source_failed>
    DIGEST_EMPTY:   <date>  (sources fetched OK but no new items survived dedup)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make atlas_lib importable when this script is run directly from the bot workspace
sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas_lib import BUCKETS  # noqa: E402
from atlas_lib import archive as arch  # noqa: E402
from atlas_lib import classifier as clf  # noqa: E402
from atlas_lib import composer  # noqa: E402
from atlas_lib import config as cfg  # noqa: E402
from atlas_lib import fetchers  # noqa: E402
from atlas_lib import telegram_api as tg  # noqa: E402

APP_ID = "app_atlas_daily_digest"


def _log(msg: str) -> None:
    print(f"[atlas_digest] {msg}", file=sys.stderr)


def _today_local(tz: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz))
    except Exception:
        return datetime.now(timezone.utc)


def _gather_candidates(bot_id: str) -> tuple[list[dict], list[str], list[dict]]:
    """Fetch from all configured sources.

    Returns ``(items, failed_sources, source_health)`` where:
      * ``items`` is the flat candidate list across all sources.
      * ``failed_sources`` is the short-name list used by the legacy
        ``DIGEST_PARTIAL`` reporting code.
      * ``source_health`` is one record per probed source for the
        ``digest_source_audit`` daemon to ingest. Shape::

            {
              "name":   str,                  # source name from sources.json
              "kind":   "rss"|"github_releases"|"brave",
              "target": str,                  # url / repo / query — the
                                              # operator-facing identifier
              "ok":     bool,                 # got at least one item
              "items":  int,                  # items returned
              "skipped_reason": str?          # e.g. "no_brave_key" — non-empty
                                              # when ok=False but the operator
                                              # explicitly disabled the source
            }

        The ``digest_source_audit`` daemon (post-2026-06-05) reads the
        per-bot ``workspace/digest/source_health-{date}.json`` file to
        track consecutive failures and emit Signals when a source goes
        persistently dark.
    """
    sources = cfg.sources_config(bot_id)
    candidates: list[dict] = []
    failed: list[str] = []
    health: list[dict] = []

    for feed in sources.get("rss", []):
        url = feed.get("url")
        name = feed.get("name", url)
        if not url:
            continue
        items = fetchers.fetch_rss(url, per_feed=10)
        health.append({
            "name":   name,
            "kind":   "rss",
            "target": url,
            "ok":     bool(items),
            "items":  len(items),
        })
        if not items:
            failed.append(name)
            continue
        for it in items:
            it["source"] = name
            candidates.append(it)

    gh_token = cfg.github_token(bot_id)
    for gh in sources.get("github_releases", []):
        repo = gh.get("repo")
        name = gh.get("name", repo)
        if not repo:
            continue
        items = fetchers.fetch_github_releases(repo, token=gh_token, since_days=14)
        # Empty release window is normal — only flag if the GitHub call
        # itself failed (the fetcher logs HTTP errors to stderr; an empty
        # list could mean either "no recent releases" or "404 on the
        # repo path"). We can't distinguish here without bigger surgery,
        # so treat "no items + no GH token" as a config gap (skipped_reason)
        # and "no items + token present" as legit no-news.
        outcome = {
            "name":   name,
            "kind":   "github_releases",
            "target": repo,
            "ok":     bool(items),
            "items":  len(items),
        }
        if not items and not gh_token:
            outcome["skipped_reason"] = "no_github_token"
        health.append(outcome)
        candidates.extend(items)

    brave_key = cfg.brave_api_key(bot_id)
    for query in sources.get("brave_queries", []):
        items = fetchers.brave_search(query, api_key=brave_key, count=5)
        outcome = {
            "name":   f"brave:{query}",
            "kind":   "brave",
            "target": query,
            "ok":     bool(items) or not brave_key,   # if we deliberately
                                                       # skipped, that's not
                                                       # a failure
            "items":  len(items),
        }
        if not brave_key:
            outcome["skipped_reason"] = "no_brave_key"
        elif not items:
            failed.append(f"brave:{query}")
        health.append(outcome)
        candidates.extend(items)

    return candidates, failed, health


def _write_source_health(
    bot_id: str, today: datetime, health: list[dict],
) -> None:
    """Persist this run's per-source outcomes for the audit daemon.

    Lives at ``workspace/digest/source_health-{YYYY-MM-DD}.json`` —
    same dir as the digest-of-day Markdown so retention can sweep
    both. Best-effort: a write failure here doesn't bubble up to the
    cron exit code (the digest itself already shipped successfully
    by the time we get here).
    """
    path = (
        cfg.workspace_root(bot_id)
        / "digest"
        / f"source_health-{today.strftime('%Y-%m-%d')}.json"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_json_atomic(path, {
            "date":    today.strftime("%Y-%m-%d"),
            "bot_id":  bot_id,
            "sources": health,
            "counts": {
                "total":   len(health),
                "ok":      sum(1 for h in health if h.get("ok")),
                "failed":  sum(1 for h in health if not h.get("ok") and "skipped_reason" not in h),
                "skipped": sum(1 for h in health if h.get("skipped_reason")),
            },
        })
    except OSError as exc:
        _log(f"source_health write failed: {exc}")


def _dedup(candidates: list[dict], archive_dir: Path) -> list[dict]:
    """Drop candidates whose URL is already in archive/index.json."""
    index = arch.read_index(archive_dir)
    seen = {arch.normalize_url(e.get("url", "")) for e in index}
    seen_in_run: set[str] = set()
    out = []
    for cand in candidates:
        url = arch.normalize_url(cand.get("url", ""))
        if not url:
            continue
        if url in seen or url in seen_in_run:
            continue
        seen_in_run.add(url)
        out.append(cand)
    return out


def _classify_all(candidates: list[dict], max_items: int) -> list[dict]:
    """Run classifier across candidates. Drop items classified as 'skip'."""
    classified: list[dict] = []
    for cand in candidates[:max_items]:
        result = clf.classify(cand)
        if result["bucket"] == "skip":
            continue
        cand["bucket"] = result["bucket"]
        cand["classification_confidence"] = result["confidence"]
        cand["classification_reason"] = result["reason"]
        cand["classification_cost_usd"] = result["cost_usd"]
        cand["summary"] = cand.get("snippet", "")[:400]
        classified.append(cand)
    return classified


def _write_archive(items: list[dict], archive_dir: Path) -> int:
    written = 0
    for item in items:
        try:
            arch.write_item(item, archive_dir, captured_by=APP_ID)
            written += 1
        except (OSError, ValueError) as exc:
            _log(f"archive write failed for {item.get('url')}: {exc}")
    return written


def _write_digest_log(digest_text: str, today: datetime, bot_id: str) -> None:
    log_path = cfg.workspace_root(bot_id) / "digest" / f"{today.strftime('%Y-%m-%d')}.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    posted_at = today.isoformat()
    log_path.write_text(f"<!-- posted_at: {posted_at} -->\n\n{digest_text}", encoding="utf-8")


def _bucket_counts(items: list[dict]) -> dict:
    counts = {b: 0 for b in BUCKETS}
    for it in items:
        b = it.get("bucket")
        if b in counts:
            counts[b] += 1
    return counts


def cmd_send(args) -> int:
    today = _today_local(args.time_zone)
    date_str = today.strftime("%Y-%m-%d")
    archive_dir = cfg.workspace_root(args.bot_id) / "archive"

    candidates, failed, health = _gather_candidates(args.bot_id)
    # Persist per-source outcomes before any downstream branch can exit —
    # otherwise an early no_candidates failure leaves the audit daemon
    # with no data and the operator can't tell which source broke.
    _write_source_health(args.bot_id, today, health)
    if not candidates:
        print(f"DIGEST_FAILED: {date_str} no_candidates")
        return 2

    candidates = _dedup(candidates, archive_dir)
    if not candidates:
        print(f"DIGEST_EMPTY: {date_str}")
        return 0

    classified = _classify_all(candidates, max_items=args.max_classify)
    if not classified:
        print(f"DIGEST_EMPTY: {date_str} all_skipped")
        return 0

    # Sort by classification confidence within each bucket, then truncate per bucket
    by_bucket: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for it in classified:
        by_bucket[it["bucket"]].append(it)
    for b in BUCKETS:
        by_bucket[b].sort(key=lambda i: -i.get("classification_confidence", 0.0))
        by_bucket[b] = by_bucket[b][:args.max_items_per_bucket]
    final_items = [it for b in BUCKETS for it in by_bucket[b]]

    digest_text = composer.compose_digest(final_items, args.detail, today)
    if not digest_text:
        print(f"DIGEST_EMPTY: {date_str}")
        return 0

    # Archive before posting — so opt-out can target what was actually posted
    _write_archive(final_items, archive_dir)

    token = cfg.telegram_token(args.bot_id)
    if not token or not args.chat_id:
        print(f"DIGEST_FAILED: {date_str} no_telegram_config")
        return 2
    result = tg.send_message(token, args.chat_id, digest_text)
    if not result:
        print(f"DIGEST_FAILED: {date_str} telegram_send_error")
        return 2

    _write_digest_log(digest_text, today, args.bot_id)
    counts = _bucket_counts(final_items)
    counts_str = ",".join(f"{b[:3]}={n}" for b, n in counts.items() if n)
    suffix = "" if not failed else f" partial_sources={','.join(failed)}"
    if failed:
        print(f"DIGEST_PARTIAL: {date_str} {len(final_items)} {counts_str}{suffix}")
    else:
        print(f"DIGEST_SENT: {date_str} {len(final_items)} {counts_str}")
    return 0


def cmd_preview(args) -> int:
    today = _today_local(args.time_zone)
    archive_dir = cfg.workspace_root(args.bot_id) / "archive"
    candidates, _, _ = _gather_candidates(args.bot_id)
    candidates = _dedup(candidates, archive_dir)
    if not candidates:
        print("(no new candidates after dedup)")
        return 0
    classified = _classify_all(candidates, max_items=min(args.max_classify, 15))
    by_bucket: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for it in classified:
        by_bucket[it["bucket"]].append(it)
    final = []
    for b in BUCKETS:
        by_bucket[b].sort(key=lambda i: -i.get("classification_confidence", 0.0))
        final.extend(by_bucket[b][:args.max_items_per_bucket])
    print(composer.compose_digest(final, args.detail, today))
    return 0


def cmd_status(args) -> int:
    today = _today_local(args.time_zone)
    log_path = cfg.workspace_root(args.bot_id) / "digest" / f"{today.strftime('%Y-%m-%d')}.md"
    if not log_path.exists():
        print("no digest sent yet today")
        return 0
    first_line = log_path.read_text(encoding="utf-8").splitlines()[0]
    print(f"digest sent — {first_line}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas daily digest")
    parser.add_argument("mode", choices=["send", "preview", "status"])
    parser.add_argument("--bot-id", default=os.environ.get("BOT_ID") or "atlas")
    parser.add_argument("--chat-id", default=os.environ.get("ATLAS_CHAT_ID", ""))
    parser.add_argument("--time-zone", default=os.environ.get("TZ", "America/Los_Angeles"))
    parser.add_argument("--detail", default="standard", choices=["concise", "standard", "detailed"])
    parser.add_argument("--max-items-per-bucket", type=int, default=5)
    parser.add_argument("--max-classify", type=int, default=40,
                        help="Max candidates to classify per run (cost ceiling)")
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
