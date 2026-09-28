#!/usr/bin/env python3
"""
evolve/slack_signals.py — Slack reaction and thread signal ingestion

Reads the bot's Slack message history, extracts quality signals, and writes
them into the annotation pipeline as slack_signal records. These feed
into analyze.py detectors as a second opinion on application quality —
separate from and complementary to session-level signals from TurnObserver.

Signal types collected:
  reaction_summary: per-message reaction counts (👍/👎/🎉 and total)
  thread_depth:     reply count on a Team_bot_a output (deep thread = confusion or interest)
  no_reaction:      Team_bot_a message with no reaction after 24h (neutral/friction signal)

Data model:
  A slack_signal annotation record is written to:
    annotations/<bot_id>/slack-signals-<date>.jsonl

  Schema:
    {
      "schema_version": 1,
      "type": "slack_signal",
      "ts": "2026-04-05T...",
      "bot_id": "team_bot_a",
      "channel": "#project-x",
      "message_ts": "...",          // Slack message timestamp
      "application": "evolve-ops", // inferred from message content
      "signal_type": "reaction_summary" | "thread_depth" | "no_reaction",
      "positive_reactions": 2,
      "negative_reactions": 0,
      "neutral_reactions": 1,
      "thread_depth": 0,
      "no_reaction_hours": null,    // hours since post with no reaction
      "quality_score": 0.85         // 0-1 derived score for this message
    }

The quality_score derivation:
  - reaction_summary: (positive - negative) / max(total, 1), normalized to [0,1]
  - thread_depth: heuristic — depth 1-2 = clarifying (slight negative), depth 5+ = engaged (positive)
  - no_reaction: neutral-negative (0.40) — absence of signal is a weak friction indicator

These signals are aggregated per-application in measure.py and fed to
detect_slack_quality_drop() in analyze.py.

Slack API access:
  Requires BOT_TOKEN with channels:history and reactions:read scopes.
  Token stored in the bot's per-user auth-profiles.json under key "slack:default"
  (resolved via: openclaw auth get slack). NOT stored in shared network.json
  which is readable by all bot users. Falls back to network.json for legacy
  deployments but logs a deprecation warning.

Usage:
    python3 slack_signals.py --network config/network.json
    python3 slack_signals.py --shared-dir /Users/Shared/evolve --bot team_bot_a --days 7
    python3 slack_signals.py --shared-dir /Users/Shared/evolve --report
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Optional

from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path
from evolve_util import now_iso_micro as now_iso


SIGNAL_VERSION = 1
SLACK_API_BASE = "https://slack.com/api"

# Reaction classification
POSITIVE_REACTIONS = {"thumbsup", "+1", "white_check_mark", "tada", "rocket",
                      "fire", "100", "raised_hands", "clap", "heart", "star"}
NEGATIVE_REACTIONS = {"thumbsdown", "-1", "x", "no_entry", "warning",
                      "confused", "disappointed", "face_palm"}

# Application detection: maps Slack channel names / message keywords to applications.
# This map is intentionally generic. Deployments configure channel mappings
# via network.json slackChannelApplications:
# {"#my-channel": "my-application", "#eng": "engineering"}
# Channels not in the map default to "general".
CHANNEL_APPLICATION_MAP: dict[str, str] = {}  # populated from network.json at runtime

APPLICATION_KEYWORDS = {
    "task-management": ["task", "project", "deadline", "milestone", "status", "blocker"],
    "evolve-system": ["evolve", "proposal", "detector", "metrics", "tier"],
    "calendar":      ["schedule", "meeting", "calendar", "agenda", "event"],
    "email":         ["email", "inbox", "send", "vendor"],
}

# How many hours of no-reaction counts as a friction signal
NO_REACTION_THRESHOLD_HOURS = 24


def _resolve_slack_token(bot_id: str, network_config: dict) -> str:
    """
    Resolve Slack bot token with preference ordering:
    1. Per-bot auth-profiles.json (600 perms, not world-readable)
    2. network.json slackBotToken (legacy, deprecated, logs warning)
    3. SLACK_BOT_TOKEN env var (CI/testing)
    Exits with error if not found.
    """
    import os
    import subprocess

    # 1. Try openclaw auth (per-bot, most secure)
    try:
        # polling-bypass: one-shot auth lookup per bot (not polling)
        result = subprocess.run(
            ["openclaw", "auth", "get", "slack", "--bot", bot_id, "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            token = data.get("token") or data.get("apiKey") or data.get("value", "")
            if token:
                return token
    except Exception:
        pass

    # 2. Fallback: network.json (deprecated)
    token = network_config.get("slackBotToken", "")
    if token:
        print(
            "[slack_signals] WARNING: Using slackBotToken from network.json (world-readable). "
            "Move token to per-bot auth-profiles.json via: openclaw auth set slack"
        )
        return token

    # 3. Fallback: env var (CI)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if token:
        return token

    print(f"[slack_signals] Slack token not found for bot '{bot_id}'.")
    print("  Configure via: openclaw auth set slack --bot <id> --token xoxb-...")
    print("  Or set SLACK_BOT_TOKEN env var for testing.")
    sys.exit(1)


# ── Slack API helpers ─────────────────────────────────────────────────────────

def _slack_get(endpoint: str, params: dict, token: str) -> dict:
    """Make a GET request to the Slack API. Returns JSON dict."""
    url = f"{SLACK_API_BASE}/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
            return data
    except Exception as e:
        raise RuntimeError(f"Slack API request failed: {endpoint} — {e}")


def _paginate(endpoint: str, params: dict, token: str, key: str, limit: int = 200) -> list:
    """Paginate a Slack API endpoint, collecting all items up to limit."""
    items = []
    cursor = None
    while len(items) < limit:
        page_params = {**params, "limit": min(200, limit - len(items))}
        if cursor:
            page_params["cursor"] = cursor
        data = _slack_get(endpoint, page_params, token)
        items.extend(data.get(key, []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.5)  # rate limit: 1 req/sec tier
    return items


# ── Signal extraction ─────────────────────────────────────────────────────────

def _infer_application(text: str, channel_name: str) -> str:
    """Infer application domain from message text and channel."""
    if channel_name in CHANNEL_APPLICATION_MAP:
        app = CHANNEL_APPLICATION_MAP[channel_name]
        # Still check for overrides in message text
        text_lower = text.lower()
        for a, keywords in APPLICATION_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return a
        return app

    text_lower = text.lower()
    for app, keywords in APPLICATION_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return app
    return "general"


def _quality_score_reactions(positive: int, negative: int, total: int) -> float:
    """Derive a quality score from reaction counts. Range [0, 1]."""
    if total == 0:
        return 0.5  # neutral
    net = positive - negative
    # Scale: max positive = 1.0, max negative = 0.0, neutral = 0.5
    score = 0.5 + (net / max(total * 2, 1)) * 0.5
    return round(max(0.0, min(1.0, score)), 3)


def _quality_score_thread(depth: int) -> float:
    """
    Thread depth quality signal.
    depth 0 = no replies (neutral-positive for concise outputs)
    depth 1-2 = clarifying (slight negative — not clear enough)
    depth 3-5 = engaged discussion (positive)
    depth 6+ = potentially confused (negative)
    """
    if depth == 0:
        return 0.60
    if depth <= 2:
        return 0.45
    if depth <= 5:
        return 0.70
    return 0.35


def extract_signals_from_message(
    message: dict,
    channel_name: str,
    bot_user_id: str,
    now_ts: float,
) -> Optional[dict]:
    """
    Extract a quality signal from a single Slack message.
    Returns a signal record or None if not relevant.
    Only processes messages from this bot.
    """
    if message.get("user") != bot_user_id:
        return None

    text = message.get("text", "")
    msg_ts = float(message.get("ts", "0"))
    msg_time = datetime.fromtimestamp(msg_ts, tz=timezone.utc)
    application = _infer_application(text, channel_name)

    reactions = message.get("reactions", [])
    reply_count = int(message.get("reply_count", 0))
    hours_since_post = (now_ts - msg_ts) / 3600

    if reactions:
        positive = sum(
            r["count"] for r in reactions
            if r["name"] in POSITIVE_REACTIONS
        )
        negative = sum(
            r["count"] for r in reactions
            if r["name"] in NEGATIVE_REACTIONS
        )
        total = sum(r["count"] for r in reactions)
        quality = _quality_score_reactions(positive, negative, total)

        signal = {
            "schema_version": SIGNAL_VERSION,
            "type": "slack_signal",
            "ts": now_iso(),
            "message_time": msg_time.isoformat(),
            "message_ts": message.get("ts"),
            "channel": channel_name,
            "application": application,
            "signal_type": "reaction_summary",
            "positive_reactions": positive,
            "negative_reactions": negative,
            "neutral_reactions": total - positive - negative,
            "thread_depth": reply_count,
            "no_reaction_hours": None,
            "quality_score": quality,
        }

    elif reply_count > 0:
        quality = _quality_score_thread(reply_count)
        signal = {
            "schema_version": SIGNAL_VERSION,
            "type": "slack_signal",
            "ts": now_iso(),
            "message_time": msg_time.isoformat(),
            "message_ts": message.get("ts"),
            "channel": channel_name,
            "application": application,
            "signal_type": "thread_depth",
            "positive_reactions": 0,
            "negative_reactions": 0,
            "neutral_reactions": 0,
            "thread_depth": reply_count,
            "no_reaction_hours": None,
            "quality_score": quality,
        }

    elif hours_since_post >= NO_REACTION_THRESHOLD_HOURS:
        # No reaction after threshold — weak friction signal
        signal = {
            "schema_version": SIGNAL_VERSION,
            "type": "slack_signal",
            "ts": now_iso(),
            "message_time": msg_time.isoformat(),
            "message_ts": message.get("ts"),
            "channel": channel_name,
            "application": application,
            "signal_type": "no_reaction",
            "positive_reactions": 0,
            "negative_reactions": 0,
            "neutral_reactions": 0,
            "thread_depth": 0,
            "no_reaction_hours": round(hours_since_post, 1),
            "quality_score": 0.40,
        }

    else:
        return None  # Too recent, no signal yet

    return signal


# ── Main ingestion flow ───────────────────────────────────────────────────────

def ingest_slack_signals(
    bot_id: str,
    shared_dir: str,
    slack_token: str,
    channels: list[str],
    days: int = 7,
    dry_run: bool = False,
) -> int:
    """
    Main ingestion loop. Reads recent messages from specified Slack channels,
    extracts quality signals, deduplicates against already-ingested message_ts,
    and writes to annotations/<bot_id>/slack-signals-<date>.jsonl.

    Returns count of new signals written.
    """
    # Get bot user ID
    auth_data = _slack_get("auth.test", {}, slack_token)
    bot_user_id = auth_data.get("user_id", "")
    if not bot_user_id:
        print("[slack_signals] Could not determine bot user ID")
        return 0

    print(f"[slack_signals] Bot user ID: {bot_user_id}, channels: {len(channels)}, days: {days}")

    # Load already-ingested message timestamps (dedup)
    ingested_ts: set[str] = set()
    out_dir = Path(shared_dir) / "annotations" / bot_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("slack-signals-*.jsonl"):
        try:
            with open(f) as fp:
                for line in fp:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            if record.get("message_ts"):
                                ingested_ts.add(record["message_ts"])
                        except Exception:
                            pass
        except Exception:
            pass

    # Determine lookback window
    oldest_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    now_ts = datetime.now(timezone.utc).timestamp()

    total_written = 0
    today_str = date.today().isoformat()
    out_path = out_dir / f"slack-signals-{today_str}.jsonl"

    for channel_id_or_name in channels:
        try:
            # Resolve channel ID if a name was given
            if channel_id_or_name.startswith("#"):
                channel_name = channel_id_or_name
                # List channels to find ID
                channels_list = _paginate("conversations.list", {"types": "public_channel"}, slack_token, "channels")
                channel_entry = next((c for c in channels_list if f"#{c['name']}" == channel_id_or_name), None)
                if not channel_entry:
                    print(f"[slack_signals] Channel not found: {channel_id_or_name}")
                    continue
                channel_id = channel_entry["id"]
            else:
                channel_id = channel_id_or_name
                # Get name for capability mapping
                info = _slack_get("conversations.info", {"channel": channel_id}, slack_token)
                channel_name = f"#{info.get('channel', {}).get('name', channel_id)}"

            print(f"[slack_signals] Reading {channel_name}...")

            messages = _paginate(
                "conversations.history",
                {"channel": channel_id, "oldest": str(oldest_ts), "include_all_metadata": "true"},
                slack_token,
                "messages",
            )

            channel_signals = 0
            for msg in messages:
                msg_ts = msg.get("ts", "")
                if msg_ts in ingested_ts:
                    continue

                signal = extract_signals_from_message(msg, channel_name, bot_user_id, now_ts)
                if signal:
                    signal["bot_id"] = bot_id
                    if not dry_run:
                        with open(out_path, "a") as f:
                            f.write(json.dumps(signal) + "\n")
                    else:
                        print(f"  [dry-run] {signal['signal_type']} app={signal['application']} "
                              f"quality={signal['quality_score']}")
                    ingested_ts.add(msg_ts)
                    channel_signals += 1
                    total_written += 1

            print(f"[slack_signals] {channel_name}: {channel_signals} new signal(s)")
            time.sleep(1)  # rate limit

        except Exception as e:
            print(f"[slack_signals] Error processing {channel_id_or_name}: {e}")
            continue

    print(f"[slack_signals] Done — {total_written} signal(s) written to {out_path}")
    return total_written


def load_slack_signals(shared_dir: str, bot_id: str, days: int) -> list[dict]:
    """Load all slack_signal records for a bot across the past N days."""
    out_dir = Path(shared_dir) / "annotations" / bot_id
    signals = []
    today = date.today()
    # Also check slack-signals-*.jsonl (separate from daily annotations)
    for f in sorted(out_dir.glob("slack-signals-*.jsonl")):
        try:
            file_date_str = f.stem.replace("slack-signals-", "")
            file_date = date.fromisoformat(file_date_str)
            if (today - file_date).days > days:
                continue
            with open(f) as fp:
                for line in fp:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            if record.get("type") == "slack_signal":
                                signals.append(record)
                        except Exception:
                            pass
        except Exception:
            pass
    return signals


def print_report(shared_dir: str, bot_id: str, days: int = 7) -> None:
    """Print a summary of Slack signal quality by application."""
    signals = load_slack_signals(shared_dir, bot_id, days)
    if not signals:
        print(f"No Slack signals found for {bot_id} in past {days} days.")
        return

    # Aggregate by application
    app_stats: dict[str, dict] = {}
    for sig in signals:
        cap = sig.get("application", "general")
        if cap not in app_stats:
            app_stats[cap] = {"count": 0, "quality_sum": 0.0, "positive": 0,
                              "negative": 0, "no_reaction": 0}
        s = app_stats[cap]
        s["count"] += 1
        s["quality_sum"] += sig.get("quality_score", 0.5)
        s["positive"] += sig.get("positive_reactions", 0)
        s["negative"] += sig.get("negative_reactions", 0)
        if sig.get("signal_type") == "no_reaction":
            s["no_reaction"] += 1

    print(f"Slack signals — {bot_id} ({days}d) — {len(signals)} total")
    print(f"{'Application':<20} {'Signals':>7} {'Avg Quality':>12} {'👍':>5} {'👎':>5} {'No React':>9}")
    print("-" * 62)
    for cap, s in sorted(app_stats.items(), key=lambda x: -x[1]["count"]):
        avg_q = s["quality_sum"] / max(s["count"], 1)
        flag = " ⚠️" if avg_q < 0.45 else ""
        print(f"{cap:<20} {s['count']:>7} {avg_q:>11.2f}  {s['positive']:>4}  "
              f"{s['negative']:>4}  {s['no_reaction']:>8}{flag}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve Slack signal ingestion")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", default=str(resolve_network_path()), help="Path to network.json")
    parser.add_argument("--bot", dest="bot_id", required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    # Module enable/disable check
    try:
        from evolve_config import load_config as _lc2, is_module_enabled as _ime
        _cfg2 = _lc2(getattr(args, 'network', None))
        if not _ime(_cfg2, "slack_signals"):
            print(f"[slack_signals] slack_signals module is disabled — exiting.")
            import sys as _sys2; _sys2.exit(0)
    except Exception:
        pass

    shared_dir = args.shared_dir
    network_config: dict = {}

    if args.network:
        network_config = json.loads(Path(args.network).read_text())
        shared_dir = network_config.get("sharedDir", shared_dir)

    if args.report:
        print_report(shared_dir, args.bot_id, args.days)
        return

    # Resolve Slack token: prefer per-bot auth-profiles.json over shared network.json.
    # network.json is world-readable by all bot users; auth-profiles.json is 600.
    slack_token = _resolve_slack_token(args.bot_id, network_config)

    # Load deployment-specific channel→application mapping from network.json
    channel_app_overrides = network_config.get("slackChannelApplications", {})
    CHANNEL_APPLICATION_MAP.update(channel_app_overrides)

    channels = network_config.get("slackChannels", ["#general"])

    ingest_slack_signals(
        bot_id=args.bot_id,
        shared_dir=shared_dir,
        slack_token=slack_token,
        channels=channels,
        days=args.days,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
