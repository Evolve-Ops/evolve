"""HTTP routes for the pre-flight intent router health surface.

One endpoint:

  GET /api/preflight/health?days=7
       Returns per-bot pre-flight router stats from the cascade telemetry
       spans over the requested window (default 7 days). Wraps
       ``cascade.preflight_audit.compute_preflight_stats``.

       Response shape::

           {
             "window_days": 7,
             "now": "2026-06-07T01:23:45.678+00:00",
             "by_bot": {
               "<bot_id>": {
                 "total_spans": int,
                 "preflight_ran": int,
                 "decisions": int,
                 "categories": { "agreement": int, ... },
                 "rates": { "agreement_rate": float, ... },
                 "by_layer": { "regex": {"count": int, ...}, ... },
                 "by_reason": { "regex:design_imperative": {...}, ... },
                 "haiku_call_count": int,
                 "haiku_latency_ms_p50": float | None,
                 "haiku_latency_ms_p95": float | None,
                 "haiku_estimated_cost_usd": float,
               },
               ...
             },
             "totals": {
               "spans_seen": int,
               "preflight_ran": int,
               "haiku_calls": int,
               "haiku_estimated_cost_usd": float,
             },
             "thresholds": {
               "min_decisions": 30,
               "over_escalation_pct": 15.0,
               "under_escalation_pct": 15.0,
               "cascade_corrected_pct": 10.0,
             }
           }

The UI consumes this on the Cost Optimization page to surface
pre-flight agreement rates + top misrouting reasons. The "thresholds"
block lets the UI color-code per-bot rate values consistently with
the Signal-emission thresholds in ``audit_runner``.

Pairs with the four-PR pre-flight router design:
  Phase 1 (#2334) wiring → Phase 2 (#2339) regex → Phase 3 (#2340) haiku
  → Phase 4 (#2342) disagreement detector → THIS PR's UI surface.

Without this endpoint the disagreement detector's Signals fire on the
Alerts page but the rich per-bot stats (rates by layer, top firing
reasons, haiku cost projection) sat unread on disk. Operators couldn't
SEE the data driving the Signals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, Response, request

from ..config import load_network


def register_preflight_routes(app: Flask, network_path: Path) -> None:
    """Register /api/preflight/health."""

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    @app.get("/api/preflight/health")
    def api_preflight_health() -> Response:
        # Window query param — default 7 days, clamped to [1, 30].
        # Wider than 7 starts hitting span-retention boundaries; 1 day
        # is the floor that still produces meaningful samples on busy
        # bots.
        try:
            days = int(request.args.get("days", "7"))
        except ValueError:
            days = 7
        days = max(1, min(30, days))

        shared_dir = _shared_dir()
        snap = _compute_preflight_health(shared_dir, days=days, now=datetime.now(timezone.utc))
        return jsonify(snap)


def _compute_preflight_health(
    shared_dir: Path, *, days: int, now: datetime,
) -> dict[str, Any]:
    """Read spans + compute the stats. Split from the route so tests
    can exercise it without a Flask app."""
    from cascade.preflight_audit import (  # type: ignore
        compute_preflight_stats,
        PREFLIGHT_MIN_DECISIONS,
        PREFLIGHT_OVER_ESCALATION_THRESHOLD,
        PREFLIGHT_UNDER_ESCALATION_THRESHOLD,
        PREFLIGHT_CASCADE_CORRECTED_THRESHOLD,
    )

    spans = list(_iter_recent_spans(shared_dir, now=now, days=days))
    stats = compute_preflight_stats(spans)

    return {
        "window_days": days,
        "now": now.isoformat(),
        **stats,
        "thresholds": {
            "min_decisions": PREFLIGHT_MIN_DECISIONS,
            "over_escalation_pct": PREFLIGHT_OVER_ESCALATION_THRESHOLD * 100,
            "under_escalation_pct": PREFLIGHT_UNDER_ESCALATION_THRESHOLD * 100,
            "cascade_corrected_pct": PREFLIGHT_CASCADE_CORRECTED_THRESHOLD * 100,
        },
    }


def _iter_recent_spans(shared_dir: Path, *, now: datetime, days: int):
    """Yield cascade-telemetry spans from the last ``days`` days.

    Reuses the same cross-location merge helper that routes_cascade.py
    uses — spans land in BOTH per-bot ``{bot}/spans/`` dirs (where
    CascadeTelemetry.ts writes) AND central ``observability/spans/``,
    and iter_turn_spans walks both. Lazy import so the route module
    stays importable in test contexts without the analyzer package.
    """
    try:
        from observability.session_rollup import iter_turn_spans  # type: ignore
    except ImportError:
        return
    cutoff = now - timedelta(days=days)
    try:
        for span in iter_turn_spans(
            shared_dir, since=cutoff, until=now, limit=200_000,
        ):
            # iter_turn_spans returns TurnSpan dataclass; to_dict() for
            # consistency with how preflight_audit consumes spans.
            yield span.to_dict() if hasattr(span, "to_dict") else span
    except Exception:  # noqa: BLE001
        # No spans yet (brand-new pod) or a transient read failure —
        # caller treats absent data as the empty case.
        return
