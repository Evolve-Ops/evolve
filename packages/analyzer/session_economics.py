"""session_economics — emit Signals for cache health + bot-engagement patterns.

Reads on-disk telemetry only (no LLM):
  - cost_event JSONL via cost_ledger.read_events

Three signal types, all under producer "session_economics":

  cache_invalidation_elevated  — share of cache-participating events whose
                                 cache_state == "invalidated" exceeds
                                 threshold. The TTL-too-short smoking gun:
                                 we paid cacheWrite cost without reaping
                                 cacheRead savings.
  cache_hit_rate_low           — realized cache hit rate (cache_read /
                                 total prompt over cache-participating
                                 events) below threshold. Catches "caching
                                 isn't engaging" — prompt churn or short
                                 TTL on long-gap sessions.
  bot_unused                   — zero user_turn events in N days. Distinct
                                 from cost_watchdog.automation_dominance,
                                 which requires turns. Informational; the
                                 operator decides whether to retire,
                                 repurpose, or accept.

Each detection becomes a Signal via signals.store.observe(). Signatures
that fired on a previous run but didn't fire today are auto-resolved via
signals.store.sweep_resolve() at the end of the run.

Distinct from cost_watchdog: that producer owns spend, automation share,
cron behavior, workspace MD bloat, and per-session cost outliers. This
producer owns cache-state economics and bot-engagement gaps — telemetry
that's net-new to the Sessions story.

Thresholds default conservatively and are tunable via network.json:

    {
      "session_economics": {
        "defaults": {
          "cache_window_days": 7,
          "cache_min_events": 50,
          "invalidation_ratio_threshold": 0.15,
          "hit_rate_threshold": 0.50,
          "unused_days": 14
        },
        "bots": {
          "evolve": { "unused_days": 9999 }
        }
      }
    }

Designed to run daily under launchd as the evolve user.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cost_ledger import read_events
from evolve_config import (
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
)
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "session_economics"

# Cache states emitted by CostLedger.ts inferCacheState():
#   warm        — cache hit (read_ratio >= 0.80 of prompt)
#   invalidated — paid cacheWrite, got <10% cacheRead benefit
#   fresh       — first turn of session; no cache participation possible
#   unknown     — malformed response or model without cache support
#
# Invalidation ratio and hit rate are both computed over the
# "cache-participating" set: events whose cache_state ∈ {warm, invalidated}.
# Fresh events are excluded because a bot with mostly fresh sessions
# (1-turn Q&A) isn't misconfigured — there's just nothing to cache.
CACHE_PARTICIPATING: frozenset[str] = frozenset({"warm", "invalidated"})

USER_TRIGGER_KIND = "user_turn"


DEFAULTS: dict[str, Any] = {
    # Window for cache-health computations. 7 days matches cost_watchdog's
    # rhythm and gives enough events for stable ratios on low-traffic bots.
    "cache_window_days": 7,
    # Minimum cache-participating events required before either cache
    # signal fires. Below this the ratio is too noisy to act on.
    "cache_min_events": 50,
    # Invalidated / (warm + invalidated). Above 15% suggests TTL is short
    # enough that a meaningful share of follow-up turns miss the window.
    "invalidation_ratio_threshold": 0.15,
    # cache_read / (cache_read + cache_write + input) over the
    # participating set. Below 50% means we're paying for caching
    # without realizing the savings — either prompt churn or wrong TTL.
    "hit_rate_threshold": 0.50,
    # Days with zero user_turn events before bot_unused fires. Default 14;
    # raise per-bot for system bots that legitimately never get DMed (e.g.
    # evolve, which talks to operators via integrations rather than direct).
    "unused_days": 14,
}


def _thresholds_for_bot(bot_id: str, config: dict[str, Any]) -> dict[str, Any]:
    se_cfg = config.get("session_economics") or {}
    out: dict[str, Any] = dict(DEFAULTS)
    out.update(se_cfg.get("defaults") or {})
    out.update((se_cfg.get("bots") or {}).get(bot_id) or {})
    return out


# ── Aggregations ─────────────────────────────────────────────────────────────


def _aggregate_cache(events: Iterable[dict]) -> dict[str, Any]:
    """Sum cache-state tallies and token totals over the participating set.

    Returns a dict with:
      participating_count:  warm + invalidated events
      invalidated_count:    invalidated events
      cache_read_tokens:    sum across participating events
      cache_write_tokens:   sum across participating events
      input_tokens:         sum across participating events
    Fresh/unknown events are not summed; they're not part of either ratio.
    """
    participating = 0
    invalidated = 0
    cache_read = 0
    cache_write = 0
    input_toks = 0
    for e in events:
        state = e.get("cache_state") or "unknown"
        if state not in CACHE_PARTICIPATING:
            continue
        participating += 1
        if state == "invalidated":
            invalidated += 1
        cache_read += int(e.get("cache_read_tokens", 0) or 0)
        cache_write += int(e.get("cache_write_tokens", 0) or 0)
        input_toks += int(e.get("input_tokens", 0) or 0)
    return {
        "participating_count": participating,
        "invalidated_count": invalidated,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "input_tokens": input_toks,
    }


def _last_user_turn_ts(events: Iterable[dict]) -> str | None:
    """Return the ISO timestamp of the most recent user_turn event, or None."""
    latest: str | None = None
    for e in events:
        if (e.get("trigger_kind") or "") != USER_TRIGGER_KIND:
            continue
        ts = e.get("ts")
        if not isinstance(ts, str):
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _count_automation_events(events: Iterable[dict]) -> int:
    return sum(
        1
        for e in events
        if (e.get("trigger_kind") or "") != USER_TRIGGER_KIND
    )


def _count_user_turn_events(events: Iterable[dict]) -> int:
    return sum(
        1
        for e in events
        if (e.get("trigger_kind") or "") == USER_TRIGGER_KIND
    )


# ── Detectors ────────────────────────────────────────────────────────────────
# Each returns a list of dicts shaped for signals_store.observe(), so the
# runner can accumulate kept_signatures and call observe() uniformly.


def detect_cache_invalidation_elevated(
    bot_id: str,
    events: list[dict],
    *,
    threshold_ratio: float,
    min_events: int,
    window_days: int,
) -> list[dict]:
    agg = _aggregate_cache(events)
    participating = agg["participating_count"]
    invalidated = agg["invalidated_count"]
    if participating < min_events:
        return []
    ratio = invalidated / participating
    if ratio < threshold_ratio:
        return []
    return [
        {
            "signature": make_signature(
                PRODUCER, "cache_invalidation_elevated", bot_id
            ),
            "producer": PRODUCER,
            "type": "cache_invalidation_elevated",
            "flavor": "maintenance",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: {ratio:.0%} of cached turns are invalidated "
                f"({invalidated}/{participating} over {window_days}d)"
            ),
            "body": (
                f"{bot_id} re-wrote prompt cache without reaping read "
                f"savings on {invalidated} of {participating} "
                f"cache-participating turns over the last {window_days} days "
                f"({ratio:.0%}, threshold {threshold_ratio:.0%}). Common "
                f"causes: TTL too short for the inter-turn gap, config "
                f"churn invalidating the cache key, or system prompt "
                f"regenerating between turns. Consider raising the prompt "
                f"cache TTL on the Cost page."
            ),
            "details": {
                "bot_id": bot_id,
                "window_days": window_days,
                "invalidated_count": invalidated,
                "participating_count": participating,
                "invalidated_ratio": round(ratio, 4),
                "threshold_ratio": threshold_ratio,
                # Severity framework: cost vector. Magnitude scales with
                # how bad the invalidation rate is — anything past ~50%
                # means most prompt-cache writes never paid off, so the
                # $ impact compounds. Spec: §2.2 cost magnitudes.
                "vector": "cost",
                "magnitude": 3 if ratio >= 0.7 else (2 if ratio >= 0.4 else 1),
                "what_it_means": (
                    f"{bot_id} re-wrote prompt cache without reaping "
                    f"read savings on {invalidated} of {participating} "
                    f"turns ({ratio:.0%}). Cache invalidation means we "
                    "paid the cache-write cost but the next turn missed "
                    "the cache anyway, so we're paying for caching "
                    "without getting the discount. Usual cause: the "
                    "TTL is shorter than the inter-turn gap (users "
                    "take longer to reply than the cache lives), or "
                    "the system prompt is regenerating between turns."
                ),
                "fix_steps": (
                    f"1. Open Cost → Bot detail for `{bot_id}` and "
                    "check the TTL setting (default 5min)\n"
                    "2. If users typically reply slower than that, "
                    "raise TTL to 1h via the Prompt cache TTL slider "
                    "on the Cost page\n"
                    "3. If TTL is already long: the system prompt is "
                    "probably churning. Check the bot's "
                    f"`/Users/{bot_id}/.openclaw/openclaw.json` for "
                    "dynamic context blocks at the top of the prompt\n"
                    "4. Verify the next batch of turns: invalidation "
                    "ratio should drop on the next session_economics run"
                ),
            },
        }
    ]


def detect_cache_hit_rate_low(
    bot_id: str,
    events: list[dict],
    *,
    threshold_ratio: float,
    min_events: int,
    window_days: int,
) -> list[dict]:
    agg = _aggregate_cache(events)
    participating = agg["participating_count"]
    if participating < min_events:
        return []
    total_prompt = (
        agg["cache_read_tokens"] + agg["cache_write_tokens"] + agg["input_tokens"]
    )
    if total_prompt <= 0:
        return []
    hit_rate = agg["cache_read_tokens"] / total_prompt
    if hit_rate >= threshold_ratio:
        return []
    return [
        {
            "signature": make_signature(PRODUCER, "cache_hit_rate_low", bot_id),
            "producer": PRODUCER,
            "type": "cache_hit_rate_low",
            "flavor": "maintenance",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: cache hit rate {hit_rate:.0%} "
                f"(threshold {threshold_ratio:.0%}, {participating} turns "
                f"over {window_days}d)"
            ),
            "body": (
                f"{bot_id} realized only {hit_rate:.0%} of its prompt "
                f"tokens from cache across {participating} "
                f"cache-participating turns over the last {window_days} "
                f"days. Either prompt structure is churning between turns "
                f"(system prompt regenerating, dynamic context block at "
                f"the top of the prompt) or TTL expires before users "
                f"reply. Investigate before bumping TTL — a low hit rate "
                f"with stable prompts often points to churn, not timing."
            ),
            "details": {
                "bot_id": bot_id,
                "window_days": window_days,
                "hit_rate": round(hit_rate, 4),
                "threshold_ratio": threshold_ratio,
                "cache_read_tokens": agg["cache_read_tokens"],
                "cache_write_tokens": agg["cache_write_tokens"],
                "input_tokens": agg["input_tokens"],
                "participating_count": participating,
                # Severity framework: cost vector. Lower hit rate = more
                # cache-write tokens paid for vs cache-read tokens
                # reaped. Magnitude scales inversely with hit_rate.
                "vector": "cost",
                "magnitude": 2 if hit_rate < 0.3 else 1,
                "what_it_means": (
                    f"{bot_id} only realized {hit_rate:.0%} of its "
                    f"prompt tokens from cache across {participating} "
                    "turns. The bot is paying full-price input tokens "
                    "instead of cheap cache-read tokens — measurable "
                    "spend leak. Two distinct causes: prompt structure "
                    "is churning between turns (system prompt or "
                    "dynamic-context block regenerating, so cache key "
                    "changes), or TTL expires before the next turn. "
                    "Distinct from cache_invalidation_elevated: that "
                    "fires when writes don't pay off; this fires when "
                    "reads never happen at all."
                ),
                "fix_steps": (
                    f"1. Open Cost → Bot detail for `{bot_id}` and "
                    "look at the cache-state breakdown\n"
                    "2. If `fresh` dominates: the prompt cache isn't "
                    "even engaging — the system prompt is rebuilding "
                    "from scratch each turn. Audit "
                    f"`/Users/{bot_id}/.openclaw/openclaw.json` for "
                    "non-deterministic blocks at prompt top\n"
                    "3. If `invalidated` dominates: TTL is too short "
                    "— raise it via the Prompt cache TTL slider on "
                    "the Cost page\n"
                    "4. Don't just bump TTL blindly: a low hit rate "
                    "with stable prompts points to churn (the real "
                    "fix is template stability), not timing"
                ),
            },
        }
    ]


def detect_bot_unused(
    bot_id: str,
    events: list[dict],
    *,
    unused_days: int,
    now: datetime,
) -> list[dict]:
    """Fire when zero user_turn events appear in the window.

    Distinct from cost_watchdog.automation_dominance, which requires that
    turns exist but are dominated by automation. ``bot_unused`` fires only
    when there's been no human-driven turn at all — the bot may still be
    running heartbeats and crons. Severity is ``info`` because the right
    response (retire, repurpose, leave alone) is a judgment call, not a
    mechanical fix.
    """
    user_turns = _count_user_turn_events(events)
    if user_turns > 0:
        return []
    automation = _count_automation_events(events)
    last_user_ts = _last_user_turn_ts(events)  # likely None given user_turns==0
    return [
        {
            "signature": make_signature(PRODUCER, "bot_unused", bot_id),
            "producer": PRODUCER,
            "type": "bot_unused",
            "flavor": "activity",
            "severity": "info",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: no user turns in {unused_days}d "
                f"({automation} automation turns recorded)"
            ),
            "body": (
                f"{bot_id} has not received a user turn in the last "
                f"{unused_days} days. Automation turns recorded in the "
                f"same window: {automation}. The bot may be deliberately "
                f"automation-only, idle pending a use case, or quietly "
                f"abandoned — the right response depends on intent. If "
                f"the bot is system-only (e.g. evolve), raise "
                f"`unused_days` in network.json under "
                f"`session_economics.bots.{bot_id}` to silence."
            ),
            "details": {
                "bot_id": bot_id,
                "unused_days": unused_days,
                "automation_event_count": automation,
                "last_user_turn_ts": last_user_ts,
                "window_end": now.isoformat(timespec="seconds"),
                # Severity framework: info-tier finding (judgment call —
                # bot may be deliberately automation-only). Quality
                # vector, magnitude 0 — never crowds the narrative.
                "vector": "quality",
                "magnitude": 0,
                "what_it_means": (
                    f"{bot_id} has received zero user turns in the "
                    f"last {unused_days} days. It logged {automation} "
                    "automation turns in the same window — heartbeats "
                    "and crons are still running and spending. This "
                    "is an info-tier finding, not a failure: the bot "
                    "may be deliberately automation-only, idle "
                    "pending a use case, or quietly abandoned. The "
                    "right response is a judgment call."
                ),
                "fix_steps": (
                    f"1. Decide whether `{bot_id}` should be retired:\n"
                    "   - If yes: remove from network.json members "
                    "and run `sudo evolve-admin retire-bot " + bot_id
                    + "` on the mini\n"
                    "   - If the bot is deliberately automation-only "
                    "(e.g. evolve itself): silence by raising "
                    f"`session_economics.bots.{bot_id}.unused_days` "
                    "to a large value (9999) in network.json\n"
                    "2. If repurposing: update the bot's onboarding "
                    "message and post it to the relevant channel\n"
                    "3. If accepting the idle state: dismiss this "
                    "signal on the Alerts page (it'll re-fire next "
                    f"cycle until you tune the {unused_days}-day "
                    "threshold)"
                ),
            },
        }
    ]


# ── Runner ───────────────────────────────────────────────────────────────────


def collect_for_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Run all detectors for one bot. Returns observe() kwargs dicts."""
    thresholds = _thresholds_for_bot(bot_id, config)
    now_dt = now or datetime.now(timezone.utc)
    cache_window = int(thresholds["cache_window_days"])
    unused_window = int(thresholds["unused_days"])

    cache_events = list(
        read_events(bot_id, days=cache_window, shared_dir=shared_dir, now=now_dt)
    )
    # Reuse cache_events for bot_unused when windows align; otherwise re-read.
    if unused_window == cache_window:
        unused_events = cache_events
    else:
        unused_events = list(
            read_events(
                bot_id, days=unused_window, shared_dir=shared_dir, now=now_dt
            )
        )

    detections: list[dict] = []
    detections += detect_cache_invalidation_elevated(
        bot_id,
        cache_events,
        threshold_ratio=float(thresholds["invalidation_ratio_threshold"]),
        min_events=int(thresholds["cache_min_events"]),
        window_days=cache_window,
    )
    detections += detect_cache_hit_rate_low(
        bot_id,
        cache_events,
        threshold_ratio=float(thresholds["hit_rate_threshold"]),
        min_events=int(thresholds["cache_min_events"]),
        window_days=cache_window,
    )
    detections += detect_bot_unused(
        bot_id,
        unused_events,
        unused_days=unused_window,
        now=now_dt,
    )
    return detections


def run_for_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[set[str], int]:
    """Collect detections, write Signals, return (kept_signatures, count)."""
    detections = collect_for_bot(bot_id, shared_dir, config, now=now)
    kept: set[str] = set()
    for d in detections:
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[session_economics] observe failed for {d['signature']}: {exc}",
                flush=True,
            )
    return kept, len(detections)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "session_economics — emit Signals for cache health "
            "and bot engagement"
        ),
    )
    parser.add_argument("--network", default=None)
    parser.add_argument("--bot", default=None, help="Run only for this bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    primary = get_primary(config)
    members = get_members(config)
    all_bots = ([primary] if primary and primary not in members else []) + members
    all_bots = [b for b in all_bots if b]
    if args.bot:
        all_bots = [args.bot]

    all_kept: set[str] = set()
    total = 0
    for bot in all_bots:
        kept, n = run_for_bot(bot, shared_dir, config, dry_run=args.dry_run)
        all_kept |= kept
        total += n

    if args.dry_run:
        print(
            f"[session_economics] dry-run: {len(all_bots)} bots, "
            f"{total} would-fire",
            flush=True,
        )
        return

    try:
        resolved = signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            kept_signatures=all_kept,
            reason="auto-resolve: session_economics condition cleared on next run",
        )
    except Exception as exc:
        resolved = []
        print(f"[session_economics] sweep_resolve failed: {exc}", flush=True)

    print(
        f"[session_economics] {len(all_bots)} bots, {total} firings, "
        f"{len(resolved)} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
