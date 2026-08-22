"""breakers.classify — turn-record classification helpers.

The detector needs to bucket each turn into one of three classes:

  - "auto"   — background activity (heartbeat, cron, scheduler-spawned).
               This is the activity an L1 cost breaker would veto.
  - "human"  — user-initiated activity through a recognized chat channel.
               Always allowed; the breaker MUST NOT block this.
  - "ambiguous" — neither clearly auto nor clearly human. Fail-open
               (treated as human-equivalent for veto purposes; not
               counted toward the auto-rate spike that drives the trip).

Per spec §9 decision 2, the veto semantics for Phase 3's L1 enforcer:

  veto IF   source ∈ {heartbeat, cron, scheduler} OR channel == "unknown"
  allow IF  source ∈ {user, human} AND channel ∈ {slack, telegram, discord, web}
  otherwise allow (fail-open)

Phase 1 (detection) uses the same classification — anything the Phase 3
enforcer would veto, the Phase 1 detector counts as "auto activity"
when looking for spikes.

Source/channel enum values mirror what TurnObserver records today. See
usage_analytics.py and the audit at docs/incident-cost-audit-2026-05-21.md
for the canonical list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


# Source values recorded by TurnObserver. Keep this list in sync with
# what the gateway emits — a silently-added new source value will be
# treated as "ambiguous" (fail-open) until added here, which is the
# safe direction but worth flagging on adoption.
HUMAN_SOURCES: frozenset[str] = frozenset({"user", "human"})
AUTO_SOURCES: frozenset[str] = frozenset({"heartbeat", "cron", "scheduler", "auto"})

# Channels that confirm a turn came through a human-messaging surface.
# A turn whose channel is one of these AND whose source is human is
# unambiguously a real user interaction.
HUMAN_CHANNELS: frozenset[str] = frozenset({"slack", "telegram", "discord", "web"})

# The "channel=unknown" tell — internal scheduler-spawned turns emit
# this when TurnObserver doesn't capture a routing identifier (see
# audit drill-down on team_bot_a 2026-05-20). Treated as auto.
AUTO_CHANNEL_TELLS: frozenset[str] = frozenset({"unknown", "heartbeat", "cron"})


# Low-tier model substrings. Mirrors classify_model_tier in
# packages/analyzer/metrics/resolvers/cost_metrics.py — duplicated
# here rather than imported to keep the breakers module loadable in
# slim subprocess contexts (same constraint that drives heal.py to
# read pause-state directly).
_LOW_TIER_TOKENS: frozenset[str] = frozenset({"haiku", "mini", "flash"})
_HIGH_TIER_TOKENS: frozenset[str] = frozenset(
    {"sonnet", "opus", "gpt-4", "pro", "grok-4"}
)


@dataclass(frozen=True)
class TurnClassification:
    """Result of classifying one turn record."""

    bucket: str          # "auto" | "human" | "ambiguous"
    model_tier: str      # "high" | "low" | "unknown"
    source: str
    channel: str


def classify_model_tier(model: str | None) -> str:
    """Return "high", "low", or "unknown" for a model string.

    Low tokens win over high tokens (``gpt-4o-mini`` contains both
    ``gpt-4`` and ``mini`` — it's low).
    """
    if not model:
        return "unknown"
    name = model.lower()
    if any(tok in name for tok in _LOW_TIER_TOKENS):
        return "low"
    if any(tok in name for tok in _HIGH_TIER_TOKENS):
        return "high"
    return "unknown"


def is_auto_source(turn: dict[str, Any]) -> bool:
    """Return True if this turn would be vetoed by an L1 cost breaker.

    Matches spec §9 decision 2:
        veto IF source ∈ AUTO_SOURCES OR channel ∈ AUTO_CHANNEL_TELLS

    Note: this is intentionally broader than classify_turn's "auto"
    bucket — channel="unknown" with source="user" is veto-worthy
    (it's most likely a scheduler-spawned turn the observer
    didn't fully tag) but classify_turn calls that ambiguous.
    The Phase 3 enforcer uses is_auto_source(); the Phase 1
    detector uses classify_turn() (which doesn't double-count
    ambiguous turns toward the spike). The asymmetry is deliberate:
    detection is conservative about what counts as a spike;
    enforcement is conservative about what to allow.
    """
    source = (turn.get("source") or "").lower()
    channel = (turn.get("channel") or "").lower()
    if source in AUTO_SOURCES:
        return True
    if channel in AUTO_CHANNEL_TELLS:
        return True
    return False


def classify_turn(turn: dict[str, Any]) -> TurnClassification:
    """Bucket a turn into auto / human / ambiguous for detection.

    Detection-side classification (vs is_auto_source which is
    enforcement-side):

      - "auto"     — source in AUTO_SOURCES, or channel is an auto-tell
                     (heartbeat / cron). These drive the auto-rate
                     metric. We do NOT bucket bare channel="unknown"
                     here unless source also confirms; otherwise we'd
                     conflate misrouted human turns with real auto
                     activity in the spike detector.
      - "human"    — source ∈ HUMAN_SOURCES AND channel ∈ HUMAN_CHANNELS.
                     Counted toward the human-rate quiescence check.
      - "ambiguous"— everything else. Not counted in either rate.
                     Fails open at enforcement; ignored at detection.
    """
    source = (turn.get("source") or "").lower()
    channel = (turn.get("channel") or "").lower()
    model = turn.get("model")

    tier = classify_model_tier(model)

    if source in AUTO_SOURCES or channel in {"heartbeat", "cron"}:
        bucket = "auto"
    elif source in HUMAN_SOURCES and channel in HUMAN_CHANNELS:
        bucket = "human"
    else:
        bucket = "ambiguous"

    return TurnClassification(
        bucket=bucket,
        model_tier=tier,
        source=source or "unknown",
        channel=channel or "unknown",
    )


def parse_ts(turn: dict[str, Any]) -> datetime | None:
    """Parse a turn's timestamp. Returns None if missing/malformed.

    Accepts the ISO-8601 string format TurnObserver writes (with or
    without trailing Z). Treats naive strings as UTC.
    """
    raw = turn.get("ts")
    if not raw:
        return None
    s = str(raw)
    # Handle trailing Z (ISO 8601 zulu)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def filter_window(
    turns: Iterable[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Return turns whose ts falls in [start, end). Drops untimestamped."""
    out: list[dict[str, Any]] = []
    for t in turns:
        ts = parse_ts(t)
        if ts is None:
            continue
        if start <= ts < end:
            out.append(t)
    return out
