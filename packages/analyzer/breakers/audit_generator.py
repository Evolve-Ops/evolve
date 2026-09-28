"""breakers.audit_generator — Phase 5c: async audit-of-cause.

Spec: internal/spec-circuit-breakers-2026-05-21.md §5.4.

When a breaker trips, this module analyses recent turn activity to
produce two short prose fields written back to the breaker record:

  audit_summary         — "what happened" (one-line). The dominant
                          burn pattern that explains the trip.
  audit_recommendation  — "what to do about it" (one-line). A
                          concrete config change or investigation
                          step the operator can act on.

The UI's modal already renders both fields when present (the
"📋 Diagnosis" section). Until this generator runs, the fields are
null and the section stays hidden. After it runs, the operator sees
diagnosis + suggested remediation without leaving the breaker panel.

Pure Python — no LLM call. Patterns are derived from the incidents
documented in internal/incident-cost-audit-2026-05-21.md (heartbeat-on-
wrong-model, cache-write-no-reuse, runaway session, tier shift). An
LLM-assisted pass is a possible v2 if pattern-matching turns out to
miss nuanced cases, but the spec discipline (§ "RSI infrastructure
must be cheap") argues against routing every trip through an LLM.

Designed to run as a cron / launchd cycle every few minutes:

    python3 -m breakers.audit_generator --shared-dir … [--once | --daemon]

Idempotent: a trip whose audit fields are already populated is
skipped on subsequent runs. Re-running over a fresh trip is the
common case (cron fires every N minutes, finds today's new trips
and analyses them).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from breakers import store as _store
from breakers.classify import classify_model_tier, classify_turn, parse_ts
from evolve_util import now_iso_micro as _now_iso


log = logging.getLogger(__name__)


# Window we look back from "now" when analysing a trip. Long enough
# to cover several heartbeat cycles or a cron-scale loop; short
# enough that we're describing the trip's CAUSE rather than the
# bot's normal week-of activity.
DEFAULT_AUDIT_WINDOW_HOURS: int = 4

# Below this many auto turns in the window, we fall through to the
# "manual review" recommendation rather than claiming a pattern.
_MIN_AUTO_TURNS_FOR_PATTERN: int = 5

# Tier-share thresholds for the "wrong model" pattern.
_HIGH_TIER_SHARE_FLOOR: float = 0.30

# Single-turn concentration (internal/incident-post-mortem-2026-09-17-single-
# turn-cache-thrash.md §4.3, rec #5). Any ONE of these arms fires it. The
# share arm needs a floor: one $0.02 turn is 100% of a $0.02 window and
# explains nothing about a cost trip.
_CONCENTRATION_SHARE: float = 0.50
_CONCENTRATION_SHARE_MIN_USD: float = 1.00
_CONCENTRATION_ABS_USD: float = 5.00
_CONCENTRATION_BASELINE_MULT: float = 10.0

# User-source thresholds (item 3). One interactive turn is a person waiting,
# not a cron, so the count floors drop to ONE turn and the bar moves to size:
# a single user turn must itself write this much cache without a matching read.
_USER_CACHE_WRITE_MIN_TOKENS: int = 1_000_000
_USER_RUNAWAY_MIN_TURNS: int = 40

_FALLBACK_TOP_TURNS: int = 3


@dataclass
class AuditResult:
    """One trip's audit outcome."""

    bot_id: str
    breaker_type: str
    trip_id: str
    summary: str | None = None
    recommendation: str | None = None
    skip_reason: str = ""           # populated when we didn't write fields
    pattern: str = ""               # identifier of the detector that matched

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Pattern detectors ────────────────────────────────────────────────────────


def _detect_heartbeat_wrong_model(
    turns: list[dict], *, bot_home: Path | None = None,
) -> tuple[str, str] | None:
    """Pattern 1: heartbeat turns billing against high-tier models.

    The canonical security_bot-2026-05-20 / team_bot_a-2026-04-17 incident shape.
    Bot has an agents.defaults.heartbeat.model override (haiku-tier
    typically), but the gateway is routing heartbeat turns to the
    primary model instead.

    Returns (summary, recommendation) or None.
    """
    auto_turns = [t for t in turns if classify_turn(t).bucket == "auto"]
    if len(auto_turns) < _MIN_AUTO_TURNS_FOR_PATTERN:
        return None

    by_tier: dict[str, int] = {"high": 0, "low": 0, "unknown": 0}
    high_models: dict[str, int] = {}
    for t in auto_turns:
        tier = classify_model_tier(t.get("model"))
        by_tier[tier] = by_tier.get(tier, 0) + 1
        if tier == "high":
            m = (t.get("model") or "").split("/")[-1] or "unknown"
            high_models[m] = high_models.get(m, 0) + 1

    high_share = by_tier["high"] / max(1, len(auto_turns))
    if high_share < _HIGH_TIER_SHARE_FLOOR:
        return None

    top_models = sorted(high_models.items(), key=lambda kv: -kv[1])[:3]
    top_models_str = ", ".join(f"{m} ({n})" for m, n in top_models)

    summary = (
        f"{by_tier['high']} of {len(auto_turns)} auto-source turns "
        f"({high_share:.0%}) billed against high-tier models "
        f"({top_models_str}). This pattern matches the documented "
        f"heartbeat-on-wrong-model leak — the model override doesn't "
        f"take effect on follow-up turns inside a heartbeat session."
    )
    recommendation = (
        "Set agents.defaults.heartbeat.model to a haiku-tier model "
        "(anthropic/claude-haiku-4-5). If the override is already set, "
        "the leak is on a follow-up turn inside the heartbeat session — "
        "verify with: openclaw turns | jq '.[] | select(.source==\"heartbeat\")'"
    )
    return summary, recommendation


def _cost(t: dict) -> float:
    try:
        return float(t.get("cost") or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_user(t: dict) -> bool:
    """User-source: the "human" bucket, or a turn whose ``source`` says user
    even when its channel is not a known human channel (the 09-17 turn)."""
    c = classify_turn(t)
    return c.bucket == "human" or c.source == "user"


def _describe_turn(t: dict) -> str:
    """Name one turn with every field the row carries; a missing field is
    ``unknown``, never omitted (D-CS7)."""
    def f(key: str) -> str:
        v = t.get(key)
        if v is None or v == "":
            return "unknown"
        return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v)
    parts = [
        f"session {f('session_id')}", f"ts {f('ts')}", f"model {f('model')}",
    ]
    if "calls" in t:
        parts.append(f"calls {f('calls')}")
    parts += [
        f"cache read {f('cache_read_tokens')}",
        f"cache write {f('cache_write_tokens')}",
        f"cost ${_cost(t):.2f}",
    ]
    return ", ".join(parts)


def _detect_single_turn_concentration(
    turns: list[dict], *, baseline_per_user_turn: float | None = None,
) -> tuple[str, str] | None:
    """Pattern 0: one turn IS the trip. Any source bucket.

    Fires when the top turn by recorded cost is ≥ 50 % of the window's spend
    (and ≥ $1), or ≥ $5 absolute, or — when a $/user-turn baseline is
    supplied (``bot-baselines-and-unit-cost-lines``) — ≥ 10× it. Needs no
    shape inference, which is the point: the 2026-09-17 trip was one $22.15
    user turn of $27.37, invisible to every count-based detector."""
    if not turns:
        return None
    top = max(turns, key=_cost)
    top_cost = _cost(top)
    total = sum(_cost(t) for t in turns)
    share = top_cost / total if total > 0 else 0.0
    arms = []
    if share >= _CONCENTRATION_SHARE and top_cost >= _CONCENTRATION_SHARE_MIN_USD:
        arms.append(f"{share:.0%} of the window's ${total:.2f}")
    if top_cost >= _CONCENTRATION_ABS_USD:
        arms.append(f"≥ ${_CONCENTRATION_ABS_USD:.0f} in one turn")
    if baseline_per_user_turn and top_cost >= _CONCENTRATION_BASELINE_MULT * baseline_per_user_turn:
        arms.append(f"{top_cost / baseline_per_user_turn:.0f}× the ${baseline_per_user_turn:.2f}/user-turn baseline")
    if not arms:
        return None
    from cache_shape import is_cache_thrash  # type: ignore[import]
    thrash = ""
    if is_cache_thrash(int(top.get("cache_read_tokens") or 0),
                       int(top.get("cache_write_tokens") or 0)):
        thrash = " It wrote more prompt cache than it read (cache thrash)."
    summary = (
        f"One {classify_turn(top).source}-source turn cost ${top_cost:.2f} "
        f"({'; '.join(arms)}): {_describe_turn(top)}.{thrash}"
    )
    recommendation = (
        "Open that session's transcript at the named timestamp and count its "
        "model/tool calls — a single turn at this size is an agent loop "
        "without a ceiling, not traffic. Fix the loop's cause; the daily cap "
        "cannot see inside one turn."
    )
    return summary, recommendation


def _detect_runaway_session(
    turns: list[dict], *, max_session_minutes: int = 60,
    min_turn_count: int = 20, source: str = "auto",
) -> tuple[str, str] | None:
    """Pattern 2: a single session with many turns in a short window —
    characteristic of a stuck agent or retry storm.

    ``source="auto"`` (default, unchanged): auto-bucket turns, ≥
    ``min_turn_count`` (20) in ≤ 60 min. ``source="user"``
    (:func:`_detect_runaway_session_user`): user-source turns, ≥ 40 in ≤ 60
    min — a person can legitimately exchange twenty messages in an hour, so
    the bar is doubled rather than shared."""
    by_session: dict[str, list[dict]] = {}
    for t in turns:
        if source == "user":
            if not _is_user(t):
                continue
        elif classify_turn(t).bucket != "auto":
            continue
        sid = t.get("session_id") or ""
        if not sid:
            continue
        by_session.setdefault(sid, []).append(t)

    for sid, sturns in by_session.items():
        if len(sturns) < min_turn_count:
            continue
        # Time-span of the session
        timestamps = [parse_ts(t) for t in sturns]
        timestamps = [t for t in timestamps if t is not None]
        if len(timestamps) < 2:
            continue
        span_seconds = (max(timestamps) - min(timestamps)).total_seconds()
        if span_seconds > max_session_minutes * 60:
            continue
        # Match — this session burned too many turns too fast.
        span_min = span_seconds / 60
        total_cost = sum(float(t.get("cost") or 0) for t in sturns)
        sid_short = sid[:8] if len(sid) > 8 else sid
        summary = (
            f"Session {sid_short} ran {len(sturns)} {source}-source turns "
            f"in {span_min:.0f} min (cost ≈ ${total_cost:.2f}). This is "
            f"consistent with a stuck agent loop, retry storm, or a "
            f"runaway sub-agent rather than legitimate activity."
        )
        recommendation = (
            f"Inspect session {sid_short} for the originating prompt and "
            f"the loop trigger. Likely culprits: a cron that fires before "
            f"its previous run finished, a tool-use loop with no exit "
            f"condition, or an agent that retries on transient errors "
            f"without backoff."
        )
        return summary, recommendation
    return None


def _detect_cache_write_no_reuse(
    turns: list[dict], *, min_turns: int = 5, min_write_tokens: int = 100_000,
) -> tuple[str, str] | None:
    """Pattern 3: regular-cadence auto turns with high cache_write
    and zero cache_read. Cache is being warmed but never reused —
    common for cron-spawned turns where each invocation starts a
    new context."""
    auto_turns = [t for t in turns if classify_turn(t).bucket == "auto"]
    if len(auto_turns) < min_turns:
        return None

    write_no_reuse = [
        t for t in auto_turns
        if int(t.get("cache_write_tokens") or 0) >= min_write_tokens
        and int(t.get("cache_read_tokens") or 0) == 0
    ]
    if len(write_no_reuse) < min_turns:
        return None

    # Detect regular cadence — pairwise time deltas mostly similar.
    timestamps = sorted([parse_ts(t) for t in write_no_reuse if parse_ts(t)])
    if len(timestamps) < 3:
        return None
    deltas = [
        (timestamps[i] - timestamps[i - 1]).total_seconds()
        for i in range(1, len(timestamps))
    ]
    if not deltas:
        return None
    median_delta = sorted(deltas)[len(deltas) // 2]
    regular_share = sum(
        1 for d in deltas
        if abs(d - median_delta) <= max(median_delta * 0.5, 60)
    ) / len(deltas)
    if regular_share < 0.5:
        return None

    cadence_min = median_delta / 60
    total_writes = sum(
        int(t.get("cache_write_tokens") or 0) for t in write_no_reuse
    )
    summary = (
        f"{len(write_no_reuse)} auto turns at a {cadence_min:.0f}-min "
        f"cadence wrote {total_writes:,} cache tokens but read 0 — the "
        f"cache is warming on each run and being discarded before the "
        f"next. Each invocation pays full prompt-cache cost without the "
        f"savings."
    )
    recommendation = (
        f"This is usually a cron pattern that spawns a fresh agent context "
        f"every {cadence_min:.0f} min. Options: extend cache TTL via "
        f"agents.defaults.cache.maxAgeMs to cover the cadence; reduce the "
        f"cron's frequency; or have the cron reuse the bot's primary "
        f"session instead of spawning new ones."
    )
    return summary, recommendation


def _detect_runaway_session_user(turns: list[dict]) -> tuple[str, str] | None:
    return _detect_runaway_session(
        turns, min_turn_count=_USER_RUNAWAY_MIN_TURNS, source="user",
    )


def _detect_cache_write_no_reuse_user(
    turns: list[dict], *, min_write_tokens: int = _USER_CACHE_WRITE_MIN_TOKENS,
) -> tuple[str, str] | None:
    """Pattern 3, user-source: ONE user turn is enough, no cadence test, and
    "no reuse" is :func:`cache_shape.is_cache_thrash` (wrote more than it
    read) rather than ``read == 0`` — an interactive turn aggregates many
    calls, so a later call's read never leaves the field at zero. Threshold:
    ≥ 1M tokens written by the one turn (``_USER_CACHE_WRITE_MIN_TOKENS``)."""
    from cache_shape import is_cache_thrash  # type: ignore[import]
    hits = [
        t for t in turns if _is_user(t) and is_cache_thrash(
            int(t.get("cache_read_tokens") or 0),
            int(t.get("cache_write_tokens") or 0), min_write_tokens,
        )
    ]
    if not hits:
        return None
    top = max(hits, key=lambda t: int(t.get("cache_write_tokens") or 0))
    written = sum(int(t.get("cache_write_tokens") or 0) for t in hits)
    summary = (
        f"{len(hits)} user-source turn(s) wrote {written:,} prompt-cache "
        f"tokens, more than they read — cache thrash inside a turn, not a "
        f"cron re-warm. Largest: {_describe_turn(top)}."
    )
    recommendation = (
        "The turn's context is being re-cached on every model call. Look for "
        "an agent loop re-reading the same file or image; bound the loop "
        "rather than the cache TTL, which cannot help within one turn."
    )
    return summary, recommendation


# Detectors run in priority order — first match wins. Order chosen
# so the most distinctive pattern wins when more than one could
# plausibly match.
_DETECTOR_TABLE: tuple = (
    # Called through lambdas so every detector is an actual call site.
    ("single_turn_concentration",
     lambda turns: _detect_single_turn_concentration(turns)),
    ("heartbeat_wrong_model", _detect_heartbeat_wrong_model),
    ("runaway_session", _detect_runaway_session),
    ("cache_write_no_reuse", _detect_cache_write_no_reuse),
    ("runaway_session_user", lambda turns: _detect_runaway_session_user(turns)),
    ("cache_write_no_reuse_user",
     lambda turns: _detect_cache_write_no_reuse_user(turns)),
)
_DETECTORS: tuple = tuple(fn for _name, fn in _DETECTOR_TABLE)
_DETECTOR_NAMES: dict = {fn: name for name, fn in _DETECTOR_TABLE}

# Row fields each predicate reads. A predicate with a field absent on EVERY
# turn could not be evaluated, and the fallback says so (D-CS7) rather than
# counting it among the predicates that "did not fire".
_PREDICATE_FIELDS: dict = {
    "single_turn_concentration": ("cost",),
    "heartbeat_wrong_model": ("model", "source"),
    "runaway_session": ("session_id", "ts"),
    "cache_write_no_reuse": ("cache_write_tokens", "cache_read_tokens", "ts"),
    "runaway_session_user": ("session_id", "ts"),
    "cache_write_no_reuse_user": ("cache_write_tokens", "cache_read_tokens"),
}


def _fallback_text(turns: list[dict], window_hours: int) -> str:
    """No detector fired: name the top turns and which predicates were
    evaluated vs ``unknown`` — never a bare "manual review" shrug."""
    auto_n = sum(1 for t in turns if classify_turn(t).bucket == "auto")
    total = sum(_cost(t) for t in turns)
    evaluated, unknown = [], []
    for name, fields in _PREDICATE_FIELDS.items():
        missing = [k for k in fields if all(t.get(k) in (None, "") for t in turns)]
        if missing:
            unknown.append(f"{name} (no {', '.join(missing)})")
        else:
            evaluated.append(name)
    top = sorted(turns, key=_cost, reverse=True)[:_FALLBACK_TOP_TURNS]
    lines = [
        f"{len(turns)} turns ({auto_n} auto-source) in the last {window_hours}h, "
        f"total recorded cost ${total:.2f}. No detector fired.",
        f"Top {len(top)} turns by cost:",
        *(f"  {i}. {_describe_turn(t)}" for i, t in enumerate(top, 1)),
        f"Evaluated, did not fire: {', '.join(evaluated) or 'none'}.",
        f"unknown (fields missing on every turn): {', '.join(unknown) or 'none'}.",
    ]
    return "\n".join(lines)


# ── Public API ───────────────────────────────────────────────────────────────


def analyze_trip(
    *,
    shared_dir: Path,
    bot_id: str,
    breaker_type: str,
    window_hours: int = DEFAULT_AUDIT_WINDOW_HOURS,
    now: datetime | None = None,
    # Test injection — backtest.read_turns by default
    read_turns_fn=None,
) -> tuple[str | None, str | None, str]:
    """Analyse recent activity to explain ``bot_id``'s ``breaker_type``
    trip. Returns ``(summary, recommendation, pattern_id)``.

    ``pattern_id`` is a short identifier matching the detector that
    fired (e.g. "heartbeat_wrong_model"). When no detector matches,
    returns a generic fallback summary rather than nulls — the
    operator should always see SOMETHING in the diagnosis section.
    """
    now = now or datetime.now(timezone.utc)
    until = now
    since = now - timedelta(hours=window_hours)

    if read_turns_fn is None:
        from breakers.backtest import read_turns as read_turns_fn

    try:
        turns = read_turns_fn(shared_dir, bot_id, since=since, until=until)
    except Exception as exc:  # noqa: BLE001
        log.warning("audit_generator: read_turns failed for %s: %s", bot_id, exc)
        return None, None, "read_failed"

    if not turns:
        return (
            "No turn activity recorded in the audit window — the trip "
            "may have been operator-initiated or based on out-of-band "
            "signals. Open turns/turns-<date>.jsonl to inspect manually.",
            "If the trip was operator-initiated, no remediation is "
            "needed beyond the original reason. If auto-tripped, check "
            "/api/breakers state for motivating_signals[].",
            "no_turns",
        )

    # Run each detector in priority order; first match wins.
    for detector in _DETECTORS:
        try:
            result = detector(turns)
        except Exception as exc:  # noqa: BLE001 — defensive
            log.warning("audit_generator: detector %s raised: %s", detector, exc)
            continue
        if result is not None:
            summary, recommendation = result
            return summary, recommendation, _DETECTOR_NAMES.get(detector, "unnamed")

    # Fallback when no pattern matched but turns DO exist — still names the
    # turns an operator should open first, and what could not be judged.
    summary = _fallback_text(turns, window_hours)
    recommendation = (
        "Start with the top turn listed above; if the spend is spread evenly, "
        "the breaker is tracking an absolute threshold that no single turn or "
        "session shape explains. Predicates marked unknown could not run on "
        "these rows — fix the missing fields before trusting their silence."
    )
    return summary, recommendation, "manual_review"


def process_pending_audits(
    *,
    shared_dir: Path,
    window_hours: int = DEFAULT_AUDIT_WINDOW_HOURS,
    now: datetime | None = None,
    read_turns_fn=None,
    update_fn=None,
) -> list[AuditResult]:
    """Scan all active breaker trips. For each one with unpopulated
    audit fields, run analyze_trip and write the results back via
    store.update_audit_fields.

    Idempotent: an already-populated trip is skipped via skip_reason
    'already populated'. The next run only does work on fresh trips.

    Returns one AuditResult per active breaker (including the skipped
    ones, for visibility in the runner log).
    """
    update_fn = update_fn or _store.update_audit_fields

    try:
        active = _store.list_active(shared_dir, now=now)
    except Exception as exc:  # noqa: BLE001
        log.error("audit_generator: list_active failed: %s", exc)
        return []

    results: list[AuditResult] = []
    for rec in active:
        ar = AuditResult(
            bot_id=rec.bot_id,
            breaker_type=rec.type,
            trip_id=rec.trip_id,
        )
        # Skip pod-wide scopes — they don't map to a single bot's
        # turn data. The diagnosis lives on per-bot trips that result
        # from a pod-wide trip's enforce_trip cascade, not on the
        # pod-scope record itself.
        if rec.bot_id == "pod":
            ar.skip_reason = "pod-wide scope (no per-bot turn data)"
            results.append(ar)
            continue
        # Skip already-populated trips.
        if rec.audit_summary or rec.audit_recommendation:
            ar.skip_reason = "already populated"
            ar.summary = rec.audit_summary
            ar.recommendation = rec.audit_recommendation
            results.append(ar)
            continue

        summary, recommendation, pattern = analyze_trip(
            shared_dir=shared_dir,
            bot_id=rec.bot_id,
            breaker_type=rec.type,
            window_hours=window_hours,
            now=now,
            read_turns_fn=read_turns_fn,
        )
        ar.summary = summary
        ar.recommendation = recommendation
        ar.pattern = pattern

        if summary is None and recommendation is None:
            ar.skip_reason = pattern  # "read_failed" or similar
            results.append(ar)
            continue

        # D-CS13: only a trip that armed a checkpoint can have an
        # interactive hold to evidence — appended where the operator
        # already reads the trip (the dashboard's Diagnosis section),
        # not just in the health control (see admin health.py).
        if rec.checkpoint is not None:
            hold = _store.read_earliest_hold(shared_dir, rec.bot_id, rec.trip_id)
            hold_line = (
                f"interactive hold: recorded at {hold[0]}."
                if hold else "interactive hold: not recorded."
            )
            summary = f"{summary}\n\n{hold_line}"
            ar.summary = summary

        try:
            update_fn(
                shared_dir=shared_dir,
                scope=rec.bot_id,
                breaker_type=rec.type,
                audit_summary=summary,
                audit_recommendation=recommendation,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "audit_generator: update_audit_fields failed for %s/%s: %s",
                rec.bot_id, rec.type, exc,
            )
            ar.skip_reason = f"write_failed: {exc}"
        results.append(ar)
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="breakers.audit_generator",
        description=(
            "Analyse recent activity for each active breaker trip and "
            "write audit_summary + audit_recommendation back to the "
            "breaker file. The dashboard modal's '📋 Diagnosis' section "
            "renders these fields when present."
        ),
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=Path("/Users/Shared/evolve"),
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    parser.add_argument(
        "--window-hours", type=int, default=DEFAULT_AUDIT_WINDOW_HOURS,
        help=f"Audit window in hours (default: {DEFAULT_AUDIT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single pass and exit (default).",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run continuously, sleeping --interval-seconds between cycles.",
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=300,
        help="Sleep between cycles in daemon mode (default 300 = 5 min).",
    )
    args = parser.parse_args(argv)

    def _one_cycle() -> None:
        results = process_pending_audits(
            shared_dir=args.shared_dir,
            window_hours=args.window_hours,
        )
        n_total = len(results)
        n_written = sum(
            1 for r in results
            if (r.summary or r.recommendation) and not r.skip_reason
        )
        n_skipped = sum(1 for r in results if r.skip_reason)
        print(
            f"[breakers.audit_generator] {_now_iso()} "
            f"total={n_total} written={n_written} skipped={n_skipped}",
            file=sys.stderr,
        )

    if args.daemon:
        import time
        while True:
            try:
                _one_cycle()
            except Exception as exc:  # noqa: BLE001
                print(f"[breakers.audit_generator] cycle error: {exc}", file=sys.stderr)
            time.sleep(args.interval_seconds)
    else:
        _one_cycle()
    return 0


if __name__ == "__main__":
    sys.exit(main())
