"""HTTP routes for the tier-cascade health surface.

One endpoint:

  GET /api/cascade/health
       Returns a snapshot of the cascade pipeline's health. Reads
       on-disk artifacts (pressure_flags.json, per-day labels files,
       observability span files, active signals) and computes the
       headline metrics:

         - watchdog_heartbeat_age_seconds (None when missing)
         - shadow_disagreement_rate_pct (7-day; None when no spans)
         - ask_hint_rate_pct (7-day; None when no user-facing spans)
         - labels_today / labels_yesterday (counts)
         - signals_firing (count of active cascade_audit Signals)
         - spans_today (count of cascade telemetry spans today)
         - state: "ok" | "warn" | "alert" | "no_data" — a single
           composite verdict suitable for a UI tile's status dot

NOTE: this endpoint is currently orphaned — no UI surface consumes
it. The original Overview-page tile (PR #1662) was removed because
the labels were inscrutable to operators and its click-through went
to a private-repo URL that 404'd. The endpoint stayed because the
underlying snapshot computation is solid and ripping it out plus
rebuilding from scratch when the AI Optimization page absorbs it
(with proper labels + context) would be wasteful. If you're sure
nothing else uses it, deleting routes_cascade.py + its registration
in server.py is safe.

Pairs with the operator validation guide
(internal/cascade-validation-on-the-mini.md) — until a UI surface
returns, operators read those metrics via ssh + the doc's snippets.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, Response

from ..config import load_network


# Stale threshold for the watchdog heartbeat. Spec § pressure watchdog
# sets the controller's TTL at 180s; we mark the tile WARN at 180s
# and ALERT at 600s (10 min — monitor_coverage would have fired by
# then but the operator gets an earlier-warning on the tile).
_HEARTBEAT_WARN_SEC = 180
_HEARTBEAT_ALERT_SEC = 600

# Disagreement-rate bands. Per internal/cascade-validation-on-the-mini.md
# Phase 3 cutover criterion #1, > 50% is a hard cutover blocker, and
# the guide names "stable" without a hard number. Use 25% as a soft
# warning band; 50% is alert.
_DISAGREE_WARN_PCT = 25.0
_DISAGREE_ALERT_PCT = 50.0

# Ask-hint emission rate. Spec § 2.2 says < 5% of user-facing
# sessions; above that is "bot asks too often." Use 5% as warn, 10%
# as alert.
_ASK_HINT_WARN_PCT = 5.0
_ASK_HINT_ALERT_PCT = 10.0


def register_cascade_routes(app: Flask, network_path: Path) -> None:
    """Register /api/cascade/health."""

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    @app.get("/api/cascade/health")
    def api_cascade_health() -> Response:
        shared_dir = _shared_dir()
        snap = _compute_health(shared_dir, now=datetime.now(timezone.utc))
        return jsonify(snap)


def _compute_health(shared_dir: Path, *, now: datetime) -> dict[str, Any]:
    """Single-pass snapshot of cascade pipeline health.

    Split out from the route handler so tests can exercise the
    computation against a synthetic shared_dir without a running
    Flask app.
    """
    heartbeat_age = _read_heartbeat_age(shared_dir, now=now)
    disagreement = _compute_disagreement_rate(shared_dir, now=now, days=7)
    ask_hint = _compute_ask_hint_rate(shared_dir, now=now, days=7)
    labels = _count_labels(shared_dir, now=now)
    signals = _count_active_cascade_signals(shared_dir)
    spans_today = _count_spans_today(shared_dir, now=now)

    state = _compose_state(
        heartbeat_age=heartbeat_age,
        disagreement_pct=disagreement.get("rate_pct"),
        ask_hint_pct=ask_hint.get("rate_pct"),
        labels_today=labels["today"],
        spans_today=spans_today,
    )

    return {
        "state": state,
        "now": now.isoformat(),
        "watchdog": {
            "heartbeat_age_seconds": heartbeat_age,
            "warn_threshold_seconds": _HEARTBEAT_WARN_SEC,
            "alert_threshold_seconds": _HEARTBEAT_ALERT_SEC,
        },
        "disagreement": {
            "rate_pct": disagreement.get("rate_pct"),
            "total": disagreement["total"],
            "disagreeing": disagreement["disagreeing"],
            "warn_threshold_pct": _DISAGREE_WARN_PCT,
            "alert_threshold_pct": _DISAGREE_ALERT_PCT,
        },
        "ask_hint": {
            "rate_pct": ask_hint.get("rate_pct"),
            "user_facing": ask_hint["user_facing"],
            "asked": ask_hint["asked"],
            "warn_threshold_pct": _ASK_HINT_WARN_PCT,
            "alert_threshold_pct": _ASK_HINT_ALERT_PCT,
        },
        "labels": labels,
        "signals_firing": signals,
        "spans_today": spans_today,
    }


# ── Heartbeat ────────────────────────────────────────────────────────────────


def _read_heartbeat_age(shared_dir: Path, *, now: datetime) -> int | None:
    """Read pressure_flags.json::watchdog_heartbeat and return age in
    seconds. None when the file is missing or malformed (treated by
    callers as "no_data" — not as "dead")."""
    flags_path = shared_dir / "cascade" / "pressure_flags.json"
    if not flags_path.is_file():
        return None
    try:
        data = json.loads(flags_path.read_text(encoding="utf-8"))
        hb_str = data.get("watchdog_heartbeat")
        if not isinstance(hb_str, str):
            return None
        hb = datetime.fromisoformat(hb_str.replace("Z", "+00:00"))
        return int((now - hb).total_seconds())
    except (json.JSONDecodeError, ValueError, OSError, TypeError):
        return None


# ── Span-derived metrics (disagreement, ask-hint) ────────────────────────────


def _iter_recent_spans(shared_dir: Path, *, now: datetime, days: int):
    """Yield span dicts from BOTH the per-bot spans/ dirs (where
    ``CascadeTelemetry.ts`` writes) AND the central
    ``observability/spans/`` dir. Earlier this only read the central
    dir, so on real pods (where every plugin span is per-bot) the tile
    saw zero data.

    Uses ``observability.session_rollup.iter_turn_spans`` — the
    cross-location merge helper. Safe on a brand-new pod (returns
    nothing).
    """
    try:
        # session_rollup lives in packages/analyzer; evolve-analyzer
        # is an installed dependency of the admin server. Import
        # lazily so this module stays importable in unit-test contexts
        # that don't load the analyzer package.
        from observability.session_rollup import iter_turn_spans  # type: ignore
    except ImportError:
        return
    cutoff = now - timedelta(days=days)
    try:
        for span in iter_turn_spans(
            shared_dir, since=cutoff, until=now, limit=200_000,
        ):
            yield span.to_dict()
    except Exception:
        # Read failures (missing dir, malformed file) become "no data."
        return


def _compute_disagreement_rate(
    shared_dir: Path, *, now: datetime, days: int,
) -> dict[str, Any]:
    """Shadow verdict disagreement rate over the last N days.

    Returns counts + an optional `rate_pct` field (None when zero
    spans had shadow verdicts).

    Exclusion filter: spans where the classifier didn't drive the
    decision aren't useful for measuring controller-vs-classifier
    semantic difference. We drop:

      - spend_cap / runaway-rate forced: ModelRouter overrode the
        classifier; the cascade controller agrees (also tier3); the
        disagreement is between cascade and classifier intent, NOT
        between cascade and what actually ran. Including these
        inflates the metric on cap-tripped days.
      - user_request: the operator explicitly picked a tier; both
        cascade and classifier are constrained to match. Again,
        not a controller-vs-classifier signal.
      - user_model_override: same logic — operator drove the choice.

    What remains is the pure semantic disagreement: classifier said
    X, cascade said Y, no external constraint pulled either one.
    That's the Phase 3 cutover metric.
    """
    total = 0
    disagreeing = 0
    _EXCLUDED = {"spend_cap", "user_request", "user_model_override"}
    for span in _iter_recent_spans(shared_dir, now=now, days=days):
        attrs = span.get("attributes") or {}
        if attrs.get("cascade.shadow_verdict.tier") is None:
            continue
        if attrs.get("cascade.tier_chosen_by") in _EXCLUDED:
            continue
        total += 1
        if attrs.get("cascade.shadow_verdict.disagrees"):
            disagreeing += 1
    out: dict[str, Any] = {"total": total, "disagreeing": disagreeing}
    if total > 0:
        out["rate_pct"] = round(100.0 * disagreeing / total, 1)
    return out


def _compute_ask_hint_rate(
    shared_dir: Path, *, now: datetime, days: int,
) -> dict[str, Any]:
    """Ask-hint emission rate over user-facing spans in the last N days.

    User-facing = trigger_kind ∈ {user_turn, subagent}. Aligns with
    `isUserFacingTrigger` in the plugin and the operator-guide snippet.
    """
    asked = 0
    user_facing = 0
    for span in _iter_recent_spans(shared_dir, now=now, days=days):
        attrs = span.get("attributes") or {}
        tk = attrs.get("cascade.trigger_kind")
        if tk not in ("user_turn", "subagent"):
            continue
        user_facing += 1
        if attrs.get("cascade.shadow_verdict.ask_hint_emitted"):
            asked += 1
    out: dict[str, Any] = {"user_facing": user_facing, "asked": asked}
    if user_facing > 0:
        out["rate_pct"] = round(100.0 * asked / user_facing, 1)
    return out


# ── Labels + spans counts ────────────────────────────────────────────────────


def _count_labels(shared_dir: Path, *, now: datetime) -> dict[str, int]:
    """Line counts for today's and yesterday's labels files."""
    labels_root = shared_dir / "cascade" / "labels"
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()

    def _count(day: str) -> int:
        path = labels_root / f"{day}.jsonl"
        if not path.is_file():
            return 0
        try:
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return 0

    return {"today": _count(today), "yesterday": _count(yesterday)}


def _count_spans_today(shared_dir: Path, *, now: datetime) -> int:
    """Count cascade telemetry spans observed today across BOTH the
    per-bot dirs (where the plugin writes) AND the central
    observability dir. Earlier this read only the central dir, so a
    real pod's UI tile showed zero spans regardless of bot activity.
    """
    # Iterate the cross-location merge, narrow to today's UTC window.
    today_start = datetime.combine(
        now.date(),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    count = 0
    for span in _iter_recent_spans(shared_dir, now=now, days=1):
        end = span.get("end_time") or span.get("start_time")
        if not isinstance(end, str):
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            continue
        if end_dt >= today_start:
            count += 1
    return count


def _count_active_cascade_signals(shared_dir: Path) -> int:
    """Count currently-firing Signals from producer cascade_audit.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): goes through ``signals.store.iter_active`` when the
    analyzer signals package is importable, falling back to a raw read
    of the firing/ subdirectory otherwise (keeps this route working on
    installs where the analyzer package isn't importable).
    """
    def _raw_count() -> int:
        # Fallback: signals package unavailable / unreadable — read
        # firing/ directly (the store-access-lint annotation marks this
        # deliberate fallback).
        firing_dir = shared_dir / "signals" / "firing"  # store-access-lint: analyzer-unavailable fallback
        if not firing_dir.is_dir():
            return 0
        count = 0
        try:
            for path in firing_dir.iterdir():
                if not path.is_file() or path.suffix != ".json":
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("producer") == "cascade_audit":
                        count += 1
                except (json.JSONDecodeError, OSError):
                    continue
        except OSError:
            return 0
        return count

    try:
        from signals import store as _signals_store  # type: ignore[import-not-found]
    except Exception:
        return _raw_count()

    try:
        return sum(
            1
            for sig in _signals_store.iter_active(
                shared_dir, producer="cascade_audit", state="firing"
            )
        )
    except Exception:
        return _raw_count()


# ── Composite state ──────────────────────────────────────────────────────────


def _compose_state(
    *,
    heartbeat_age: int | None,
    disagreement_pct: float | None,
    ask_hint_pct: float | None,
    labels_today: int,
    spans_today: int,
) -> str:
    """Single-word verdict for the UI tile's status dot.

    Order matters — we surface the WORST condition.

      "no_data" — pipeline isn't running yet (no spans today and no
                  heartbeat file). The tile shows a neutral grey dot.

      "alert"   — watchdog dead, or disagreement > 50%, or ask-hint
                  > 10%. Cutover-blocker territory.

      "warn"    — watchdog stale, or disagreement > 25%, or ask-hint
                  > 5%. Look here next.

      "ok"      — pipeline is flowing and metrics are within bands.
    """
    # No-data check first — a brand-new pod or a pre-cascade install
    # has no spans, no heartbeat. Don't paint the tile red on day 0.
    if heartbeat_age is None and spans_today == 0:
        return "no_data"

    # Active plugin with NO heartbeat = dead watchdog. Earlier this
    # case fell through the `is not None` checks below and returned
    # "ok" — a dead watchdog with an active plugin showed green.
    if heartbeat_age is None and spans_today > 0:
        return "warn"

    # Alert conditions.
    if heartbeat_age is not None and heartbeat_age > _HEARTBEAT_ALERT_SEC:
        return "alert"
    if disagreement_pct is not None and disagreement_pct > _DISAGREE_ALERT_PCT:
        return "alert"
    if ask_hint_pct is not None and ask_hint_pct > _ASK_HINT_ALERT_PCT:
        return "alert"

    # Warn conditions.
    if heartbeat_age is not None and heartbeat_age > _HEARTBEAT_WARN_SEC:
        return "warn"
    if disagreement_pct is not None and disagreement_pct > _DISAGREE_WARN_PCT:
        return "warn"
    if ask_hint_pct is not None and ask_hint_pct > _ASK_HINT_WARN_PCT:
        return "warn"

    return "ok"
