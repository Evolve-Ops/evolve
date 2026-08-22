"""cascade.pressure_watchdog — pod-wide pressure flag computation.

Per docs/spec-tier-cascade-2026-05-26.md § 2.6 (watchdog-reliability
section).

Polls cascade telemetry spans, computes pod-wide pressure metrics, and
writes flags to `{shared_dir}/cascade/pressure_flags.json`. The
CascadeController reads these flags at decision time (Phase 3); for
Phase 2 the flags are computed in shadow mode — written but not
consulted.

**Pressure metrics:**
  - Concurrent tier1 sessions (count of in-flight tier1 sessions)
  - Escalations per 15-minute window (pod-wide)
  - Tier1 spend per hour (rolling)

When any threshold is exceeded, the corresponding flag is true in the
output file. CascadeController treats this as "freeze further
autonomous tier1 escalations" — explicit user picks (ui_chip Power)
still honored.

**Heartbeat contract (round-2 review cost-F1 + failure-F1+F8):**
  - Every poll writes `watchdog_heartbeat: <ISO timestamp>` and
    `watchdog_ttl_seconds: 180` regardless of whether any flags fired.
  - CascadeController checks `now - heartbeat > ttl` to detect dead
    watchdog. Dead watchdog → fail-closed (suppress autonomous tier1,
    suppress ask-hints, but UI-chip + Layer 1 caps still operate).

**Telemetry-coupled-failure defense:** the watchdog ALSO reads
per-bot `tier1_active.json` files written by each plugin process —
those are in-process counters, exact even when span emission fails.
The pod-wide tier1 count is `max(spans_count, sum_of_plugin_counts)`.
When `cascade_telemetry_lost` is active, the concurrency cap is
reduced to `max(2, default/2)` — extra conservative when partly blind.

**Phase 2 (this PR): library + CLI module.** No daemon plist yet —
the deploy.py integration ships in a follow-on. The CLI's
`run_once()` is callable from cron or a launchd KeepAlive job.

Pure-function flag computation. The I/O wrapper that reads spans +
writes flags is isolated for easy testing.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

# ─────────────────────────────────────────────────────────────────────────────
# Config (defaults from spec § 4.3)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class WatchdogConfig:
    """Pod-wide pressure thresholds. From spec § 4.3 defaults."""

    max_concurrent_tier1_sessions: int = 3
    max_escalations_per_15min: int = 5
    tier1_pod_spend_per_hour_usd: float = 10.0
    watchdog_ttl_seconds: int = 180
    # Window for "in-flight" tier1 sessions (used as a heartbeat proxy
    # since we don't have session_end timestamps in span data).
    tier1_in_flight_minutes: int = 30
    # When telemetry is detected as lost, reduce concurrency cap by this divisor.
    telemetry_lost_cap_divisor: int = 2


DEFAULT_WATCHDOG_CONFIG = WatchdogConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Pressure flag computation (pure function)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PressureFlags:
    """Output of `compute_pressure_flags`. Written to pressure_flags.json."""

    # Pod-wide tier1 concurrency
    pod_tier1_concurrency_cap: bool = False
    pod_tier1_active_sessions: int = 0

    # Escalation storm detection.
    # `escalations_in_15min` is the TOTAL escalation-event count over
    # the 15-min window (shadow + live). `live_escalations_in_15min`
    # is the cascade-drove-routing subset — the metric that gates
    # `escalation_storm`. During Phase 2 shadow mode the two diverge:
    # the controller emits escalations the classifier didn't honor.
    # Phase 3 cutover collapses the gap by definition.
    escalation_storm: bool = False
    escalations_in_15min: int = 0
    live_escalations_in_15min: int = 0

    # tier1 spend burst
    tier1_pod_spend_burst: bool = False
    tier1_pod_spend_per_hour_usd: float = 0.0

    # Pod-wide correlator for grouping pressure-family Signals
    # (round-3 finding #7). Operator UI uses this to collapse a single
    # logical event with sub-items.
    pressure_event_id: str | None = None

    # Telemetry-coupled-failure indicator
    telemetry_partially_lost: bool = False
    effective_concurrency_cap: int = 3

    # Heartbeat
    watchdog_heartbeat: str = ""    # ISO-8601 UTC
    watchdog_ttl_seconds: int = 180

    # Per-bot trip counts (Phase 4 audit input)
    by_bot_tier1_active: dict[str, int] = field(default_factory=dict)


def compute_pressure_flags(
    spans: Iterable[dict],
    *,
    now: datetime | None = None,
    config: WatchdogConfig | None = None,
    in_process_tier1_counts: dict[str, int] | None = None,
    telemetry_lost: bool = False,
) -> PressureFlags:
    """Compute pod-wide pressure flags from cascade telemetry spans.

    Pure function — no I/O. Caller is responsible for reading spans
    and writing the resulting flags.

    Args:
        spans: iterable of cascade-telemetry span dicts (already filtered
            to recent + producer == cascade_telemetry)
        now: current time (default: utcnow)
        config: watchdog config (default: spec defaults)
        in_process_tier1_counts: per-bot tier1 counts from plugin heartbeat
            files (telemetry-coupled-failure defense). When provided, the
            pod-wide tier1 count is max(spans_count, sum_of_plugin_counts).
        telemetry_lost: when True, applies the extra-conservative cap.
    """
    cfg = config or DEFAULT_WATCHDOG_CONFIG
    if now is None:
        now = datetime.now(timezone.utc)

    flags = PressureFlags(
        watchdog_heartbeat=now.isoformat(),
        watchdog_ttl_seconds=cfg.watchdog_ttl_seconds,
        effective_concurrency_cap=cfg.max_concurrent_tier1_sessions,
    )

    # Apply telemetry-lost conservatism.
    if telemetry_lost:
        flags.telemetry_partially_lost = True
        flags.effective_concurrency_cap = max(
            2, cfg.max_concurrent_tier1_sessions // cfg.telemetry_lost_cap_divisor
        )

    # Per-session most-recent tier (a session's "current tier" is the
    # tier on its latest span). Sessions that haven't emitted a span
    # within `tier1_in_flight_minutes` are considered ended.
    in_flight_cutoff = now - timedelta(minutes=cfg.tier1_in_flight_minutes)
    fifteen_min_ago = now - timedelta(minutes=15)
    one_hour_ago = now - timedelta(hours=1)

    # Per-session: keep most recent span by end_time.
    most_recent_by_session: dict[str, dict] = {}
    # Per-15-minute counters for escalations.
    #
    # Phase split: the plugin records an escalation event for EVERY
    # turn the controller decided to escalate, regardless of whether
    # the controller drove routing (shadow mode) or not (live mode).
    # During the 2-week shadow window, escalation_storm flagged off the
    # raw count would surface shadow-only escalations to the operator
    # as if cascade were actively over-escalating. Split the counter:
    #
    #   escalations_count       — total across both phases (back-compat)
    #   live_escalations_count  — only when tier_chosen_by == "cascade"
    #                             (i.e., cascade actually drove routing)
    #
    # escalation_storm fires off live_escalations_count so shadow noise
    # doesn't pollute the flag. The full count is still emitted for
    # observability — operators can see the cascade's would-have rate.
    escalations_count = 0
    live_escalations_count = 0
    tier1_cost_hour = 0.0

    for span in spans:
        attrs = span.get("attributes") or {}
        sid = attrs.get("session_id")
        bot_id = span.get("bot_id")
        end_time_str = span.get("end_time")
        if not isinstance(sid, str) or not isinstance(end_time_str, str):
            continue
        try:
            end_dt = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        # Track most-recent span per session for in-flight count.
        prior = most_recent_by_session.get(sid)
        if prior is None or _safe_end_dt(prior) < end_dt:
            most_recent_by_session[sid] = span

        # Count escalations in last 15min. Total includes shadow-mode
        # escalations; the live-only sub-count gates escalation_storm.
        escalation_event = attrs.get("cascade.escalation_event")
        if escalation_event == "escalated" and end_dt >= fifteen_min_ago:
            escalations_count += 1
            if attrs.get("cascade.tier_chosen_by") == "cascade":
                live_escalations_count += 1

        # Tier1 spend in last 1h.
        if attrs.get("cascade.tier_used") == "tier1" and end_dt >= one_hour_ago:
            cost = span.get("total_cost") or 0
            if isinstance(cost, (int, float)):
                tier1_cost_hour += float(cost)

    # Count tier1 sessions still "in flight" (most recent span < in_flight_cutoff ago).
    by_bot_tier1: dict[str, int] = defaultdict(int)
    pod_tier1_active_from_spans = 0
    for sid, span in most_recent_by_session.items():
        attrs = span.get("attributes") or {}
        if attrs.get("cascade.tier_used") != "tier1":
            continue
        end_dt = _safe_end_dt(span)
        if end_dt < in_flight_cutoff:
            continue
        bot_id = span.get("bot_id") or "<unknown>"
        by_bot_tier1[bot_id] += 1
        pod_tier1_active_from_spans += 1

    # Telemetry-coupled-failure defense: max(spans, in-process).
    pod_tier1_active = pod_tier1_active_from_spans
    if in_process_tier1_counts:
        in_proc_total = sum(in_process_tier1_counts.values())
        pod_tier1_active = max(pod_tier1_active, in_proc_total)
        # Per-bot also takes max
        for bid, n in in_process_tier1_counts.items():
            by_bot_tier1[bid] = max(by_bot_tier1.get(bid, 0), n)

    flags.pod_tier1_active_sessions = pod_tier1_active
    flags.by_bot_tier1_active = dict(by_bot_tier1)
    flags.escalations_in_15min = escalations_count
    flags.live_escalations_in_15min = live_escalations_count
    flags.tier1_pod_spend_per_hour_usd = round(tier1_cost_hour, 4)

    # Apply thresholds → flags
    cap = flags.effective_concurrency_cap
    flags.pod_tier1_concurrency_cap = pod_tier1_active > cap
    # Gate the storm flag off the LIVE count so shadow-mode escalations
    # (where cascade decided but didn't drive routing) don't trip it.
    flags.escalation_storm = live_escalations_count > cfg.max_escalations_per_15min
    flags.tier1_pod_spend_burst = tier1_cost_hour > cfg.tier1_pod_spend_per_hour_usd

    # Pressure event correlator: stable while ANY pressure flag is active.
    # Round-3 #7: collapses multi-Signal noise under a single event id.
    # telemetry_partially_lost participates because the watchdog operates
    # with reduced concurrency caps in that mode — a degraded-telemetry
    # episode IS a pressure event the operator should see correlated with
    # the cap-reduced behavior.
    if (
        flags.pod_tier1_concurrency_cap
        or flags.escalation_storm
        or flags.tier1_pod_spend_burst
        or flags.telemetry_partially_lost
    ):
        # Deterministic per-window event id (15-min granularity).
        window_start = now.replace(second=0, microsecond=0)
        window_start = window_start.replace(minute=(window_start.minute // 15) * 15)
        flags.pressure_event_id = f"pressure-{window_start.isoformat()}"

    return flags


def _safe_end_dt(span: dict) -> datetime:
    """Parse end_time defensively. Returns now() on failure."""
    s = span.get("end_time")
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# I/O wrapper
# ─────────────────────────────────────────────────────────────────────────────


def write_pressure_flags(flags: PressureFlags, shared_dir: Path) -> Path:
    """Atomic write of pressure_flags.json. Returns path."""
    cascade_dir = shared_dir / "cascade"
    cascade_dir.mkdir(parents=True, exist_ok=True)
    out_path = cascade_dir / "pressure_flags.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(asdict(flags), indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    return out_path


def read_in_process_tier1_counts(shared_dir: Path) -> dict[str, int]:
    """Aggregate per-bot tier1_active.json files written by plugins.

    Plugins write `{shared_dir}/{bot_id}/cascade/tier1_active.json` on
    every tier1 grant (telemetry-coupled-failure defense). The
    watchdog reads them here.

    For Phase 2 shadow mode the plugin hasn't started writing these
    files yet (the cascade controller doesn't drive routing). Returns
    `{}` until that wiring lands; the function exists so the daemon's
    code path is ready.
    """
    out: dict[str, int] = {}
    if not shared_dir.exists():
        return out
    for bot_dir in shared_dir.iterdir():
        if not bot_dir.is_dir():
            continue
        path = bot_dir / "cascade" / "tier1_active.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = data.get("active_count")
            if isinstance(count, int) and count >= 0:
                out[bot_dir.name] = count
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Single-pass runner (callable from cron / launchd / one-shot CLI)
# ─────────────────────────────────────────────────────────────────────────────


def run_once(
    shared_dir: Path,
    *,
    now: datetime | None = None,
    config: WatchdogConfig | None = None,
    span_window_minutes: int = 65,  # cover 1h + 5min slack
    telemetry_lost: bool = False,
) -> dict:
    """One pass: read recent spans, compute flags, write file.

    Returns a small report dict for logging: {flags_written_to, summary}.
    """
    # Use the cross-location merge helper so per-bot spans (where
    # CascadeTelemetry.ts writes) AND the central observability/spans/
    # dir (where Python producers write) are both read. JsonlBackend
    # alone only sees the central dir; this watchdog ran blind against
    # the real pod's per-bot dirs until the integration bug was caught.
    from observability.session_rollup import iter_turn_spans  # noqa: E402

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=span_window_minutes)

    spans = [s.to_dict() for s in iter_turn_spans(
        shared_dir, since=since, until=now, limit=50_000,
    )]

    # Per-bot in-process counters (defense against telemetry-lost).
    in_proc = read_in_process_tier1_counts(shared_dir)

    flags = compute_pressure_flags(
        spans, now=now, config=config,
        in_process_tier1_counts=in_proc,
        telemetry_lost=telemetry_lost,
    )

    path = write_pressure_flags(flags, shared_dir)
    return {
        "flags_written_to": str(path),
        "summary": {
            "pod_tier1_active_sessions": flags.pod_tier1_active_sessions,
            "escalations_in_15min": flags.escalations_in_15min,
            "tier1_pod_spend_per_hour_usd": flags.tier1_pod_spend_per_hour_usd,
            "any_flag_fired": bool(
                flags.pod_tier1_concurrency_cap
                or flags.escalation_storm
                or flags.tier1_pod_spend_burst
            ),
            "telemetry_partially_lost": flags.telemetry_partially_lost,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--shared-dir", type=Path, default=Path("/Users/Shared/evolve"),
        help="Shared directory root (default: /Users/Shared/evolve)",
    )
    parser.add_argument(
        "--window-minutes", type=int, default=65,
        help="Span lookback window in minutes (default: 65)",
    )
    parser.add_argument(
        "--telemetry-lost", action="store_true",
        help="Pass when cascade_telemetry_lost signal is active (applies extra-conservative caps)",
    )
    args = parser.parse_args(argv)

    report = run_once(
        args.shared_dir,
        span_window_minutes=args.window_minutes,
        telemetry_lost=args.telemetry_lost,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
