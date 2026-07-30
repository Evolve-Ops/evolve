"""generators.evolve_watchdog.observe — Active monitoring (L6).

Spec §7. Monitors:
  - Per-generator proposal volume (spike > 3x or trough < 0.2x of baseline)
  - Per-generator auto-revert rate (climbing > 20% over 14 days)
  - Per-generator user-rejection rate (climbing > 30% over 14 days)
  - Verification reliability (drops > 15% over 14 days)
  - Generator dominance (one > 60% of successful proposals over 30 days)
  - Calibration drift (> 30% absolute change in 14 days)
  - Observation extraction drift (tuple count per session changes > 40%)
  - Meta-layer cost (> 1.5x baseline over 7 days)

Each detector returns 0..N WatchdogEvents. The observe() aggregator
records events to disk and emits Throttle/Pause/Investigation proposals
when events cross severity thresholds.

Conservative defaults + human-tunable thresholds (§12 L6 spec). All
proposals route to pod_operator; no bot_primary_user audience.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from generators.evolve_watchdog.events import (
    WatchdogEventStore,
    signal_id_for_event,
)
from proposal_synthesizer.emit import emit_from_proposal
from schema.candidate_proposal import Magnitude
from schema.generator import GeneratorRecord
from schema.proposal import (
    Investigation,
    PauseGenerator,
    Proposal,
    Provenance,
    RiskTag,
    ThrottleGenerator,
    new_proposal_id,
)
from schema.watchdog import WatchdogEvent, new_watchdog_event_id


GENERATOR_ID = "evolve_watchdog"
DIMENSION = "meta_health"


# Phase A.5 dismiss signatures — per-target-generator for throttles,
# per-event-type for investigations.
def dismiss_signature_for_throttle(target_generator: str) -> str:
    return f"evolve_watchdog:throttle:{target_generator}"


def dismiss_signature_for_investigation(event_type: str) -> str:
    return f"evolve_watchdog:investigation:{event_type}"


# Callable: (generator_id, window_days) -> historical stats snapshot
# Shape:
#   {
#     "proposals_emitted": int,
#     "baseline_volume": float,        # rolling mean
#     "auto_revert_rate": float,
#     "auto_revert_rate_baseline": float,
#     "rejection_rate": float,
#     "rejection_rate_baseline": float,
#     "verification_reliability": float,
#     "verification_reliability_baseline": float,
#   }
HistoryReader = Callable[[str, int], dict]


def _default_history(generator_id: str, days: int) -> dict:  # noqa: ARG001
    return {}


# Callable: () -> {generator_id: share_of_recent_successes}
DominanceReader = Callable[[], dict[str, float]]


def _default_dominance() -> dict[str, float]:
    return {}


# Callable: () -> {
#   "meta_cost_7d_usd": float,
#   "meta_cost_baseline_usd": float,
#   "tuple_count_per_session_now": float,
#   "tuple_count_per_session_baseline": float,
#   "calibration_drift_pct": float,
# }
PodStatsReader = Callable[[], dict]


def _default_pod_stats() -> dict:
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Config + Context
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class EvolveWatchdogContext:
    bot_id: str | None  # None → pod-wide
    shared_dir: Path
    # Reader functions (tests inject; production wires to real data)
    history_reader: HistoryReader = _default_history
    dominance_reader: DominanceReader = _default_dominance
    pod_stats_reader: PodStatsReader = _default_pod_stats
    # List of active generator ids to scan (tests inject; production reads
    # from registry)
    active_generator_ids: list[str] = field(default_factory=list)
    # Thresholds (all tunable via config in production)
    volume_spike_multiplier: float = 3.0
    volume_trough_multiplier: float = 0.2
    auto_revert_rate_delta: float = 0.20
    rejection_rate_delta: float = 0.30
    verification_reliability_drop: float = 0.15
    dominance_threshold: float = 0.60
    calibration_drift_threshold: float = 0.30
    extraction_drift_threshold: float = 0.40
    cost_spike_multiplier: float = 1.5
    # Only emit proposals for events at this severity or above
    proposal_severity_floor: str = "warn"
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Detectors — each returns list of WatchdogEvent
# ─────────────────────────────────────────────────────────────────────────────


# Module-level clock for _event timestamps. Set by observe() before
# running detectors; reset after. Not thread-safe, but the Watchdog is
# a single-process cron job so this is fine.
_ACTIVE_CLOCK: datetime | None = None


def _event(
    event_type,
    severity,
    details,
    *,
    bot_id=None,
) -> WatchdogEvent:
    ts = (_ACTIVE_CLOCK or datetime.now(timezone.utc)).isoformat(
        timespec="seconds"
    )
    return WatchdogEvent(
        id=new_watchdog_event_id(),
        bot_id=bot_id,
        timestamp=ts,
        event_type=event_type,
        severity=severity,
        details=details,
    )


def _detect_volume_deviation(
    ctx: EvolveWatchdogContext,
) -> list[WatchdogEvent]:
    events: list[WatchdogEvent] = []
    for gen_id in ctx.active_generator_ids:
        hist = ctx.history_reader(gen_id, 14)
        baseline = hist.get("baseline_volume")
        current = hist.get("proposals_emitted")
        if baseline is None or current is None or baseline <= 0:
            continue
        ratio = current / baseline
        if ratio >= ctx.volume_spike_multiplier:
            events.append(
                _event(
                    "proposal_volume_deviation",
                    "alert",
                    {
                        "generator_id": gen_id,
                        "ratio": ratio,
                        "baseline": baseline,
                        "current": current,
                        "direction": "spike",
                    },
                )
            )
        elif ratio <= ctx.volume_trough_multiplier:
            events.append(
                _event(
                    "proposal_volume_deviation",
                    "warn",
                    {
                        "generator_id": gen_id,
                        "ratio": ratio,
                        "baseline": baseline,
                        "current": current,
                        "direction": "trough",
                    },
                )
            )
    return events


def _detect_auto_revert_spike(
    ctx: EvolveWatchdogContext,
) -> list[WatchdogEvent]:
    events: list[WatchdogEvent] = []
    for gen_id in ctx.active_generator_ids:
        hist = ctx.history_reader(gen_id, 14)
        current = hist.get("auto_revert_rate")
        baseline = hist.get("auto_revert_rate_baseline")
        if current is None or baseline is None:
            continue
        delta = current - baseline
        if delta >= ctx.auto_revert_rate_delta:
            events.append(
                _event(
                    "auto_revert_rate_spike",
                    "alert",
                    {
                        "generator_id": gen_id,
                        "current": current,
                        "baseline": baseline,
                        "delta": delta,
                    },
                )
            )
    return events


def _detect_rejection_spike(
    ctx: EvolveWatchdogContext,
) -> list[WatchdogEvent]:
    events: list[WatchdogEvent] = []
    for gen_id in ctx.active_generator_ids:
        hist = ctx.history_reader(gen_id, 14)
        current = hist.get("rejection_rate")
        baseline = hist.get("rejection_rate_baseline")
        if current is None or baseline is None:
            continue
        delta = current - baseline
        if delta >= ctx.rejection_rate_delta:
            events.append(
                _event(
                    "rejection_rate_spike",
                    "warn",
                    {
                        "generator_id": gen_id,
                        "current": current,
                        "baseline": baseline,
                        "delta": delta,
                    },
                )
            )
    return events


def _detect_verification_drop(
    ctx: EvolveWatchdogContext,
) -> list[WatchdogEvent]:
    events: list[WatchdogEvent] = []
    for gen_id in ctx.active_generator_ids:
        hist = ctx.history_reader(gen_id, 14)
        current = hist.get("verification_reliability")
        baseline = hist.get("verification_reliability_baseline")
        if current is None or baseline is None:
            continue
        drop = baseline - current
        if drop >= ctx.verification_reliability_drop:
            events.append(
                _event(
                    "verification_reliability_drop",
                    "alert",
                    {
                        "generator_id": gen_id,
                        "current": current,
                        "baseline": baseline,
                        "drop": drop,
                    },
                )
            )
    return events


def _detect_dominance(ctx: EvolveWatchdogContext) -> list[WatchdogEvent]:
    shares = ctx.dominance_reader()
    events: list[WatchdogEvent] = []
    for gen_id, share in shares.items():
        if share >= ctx.dominance_threshold:
            events.append(
                _event(
                    "generator_dominance",
                    "warn",
                    {"generator_id": gen_id, "share": share},
                )
            )
    return events


def _detect_pod_stats(ctx: EvolveWatchdogContext) -> list[WatchdogEvent]:
    stats = ctx.pod_stats_reader()
    events: list[WatchdogEvent] = []

    # Calibration drift
    drift = stats.get("calibration_drift_pct")
    if drift is not None and drift >= ctx.calibration_drift_threshold:
        events.append(
            _event(
                "calibration_drift",
                "warn",
                {"drift_pct": drift},
            )
        )

    # Observation extraction drift
    now_rate = stats.get("tuple_count_per_session_now")
    base_rate = stats.get("tuple_count_per_session_baseline")
    if now_rate is not None and base_rate and base_rate > 0:
        pct_change = abs(now_rate - base_rate) / base_rate
        if pct_change >= ctx.extraction_drift_threshold:
            events.append(
                _event(
                    "observation_extraction_drift",
                    "warn",
                    {
                        "now_rate": now_rate,
                        "baseline_rate": base_rate,
                        "pct_change": pct_change,
                    },
                )
            )

    # Meta-layer cost spike
    now_cost = stats.get("meta_cost_7d_usd")
    base_cost = stats.get("meta_cost_baseline_usd")
    if now_cost is not None and base_cost and base_cost > 0:
        ratio = now_cost / base_cost
        if ratio >= ctx.cost_spike_multiplier:
            events.append(
                _event(
                    "meta_layer_cost_spike",
                    "alert",
                    {"ratio": ratio, "now_usd": now_cost, "baseline_usd": base_cost},
                )
            )

    return events


_ALL_DETECTORS = (
    _detect_volume_deviation,
    _detect_auto_revert_spike,
    _detect_rejection_spike,
    _detect_verification_drop,
    _detect_dominance,
    _detect_pod_stats,
)


# ─────────────────────────────────────────────────────────────────────────────
# Proposal building
# ─────────────────────────────────────────────────────────────────────────────


_SEVERITY_RANK = {"info": 0, "warn": 1, "alert": 2}


def _severe_enough(event: WatchdogEvent, floor: str) -> bool:
    return _SEVERITY_RANK.get(event.severity, 0) >= _SEVERITY_RANK.get(floor, 1)


def _build_proposals_for_events(
    events: list[WatchdogEvent], ctx: EvolveWatchdogContext
) -> list[Proposal]:
    proposals: list[Proposal] = []
    # At most one proposal per (generator_id, event_type) — avoid flooding
    seen: set[tuple[str, str]] = set()
    for event in events:
        if not _severe_enough(event, ctx.proposal_severity_floor):
            continue
        gen_id = event.details.get("generator_id") or "<pod>"
        key = (gen_id, event.event_type)
        if key in seen:
            continue
        seen.add(key)

        # Reference the WatchdogEvents as provenance signals.
        proposal = _proposal_for_event(event, ctx, all_events=events)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _proposal_for_event(
    event: WatchdogEvent,
    ctx: EvolveWatchdogContext,
    *,
    all_events: list[WatchdogEvent],
) -> Proposal | None:
    """Map an event to an appropriate action.

    - verification_reliability_drop or auto_revert_rate_spike with severity
      alert → ThrottleGenerator (reduce_cadence)
    - generator_dominance with warn → ThrottleGenerator (lower_confidence)
    - calibration_drift → Investigation
    - meta_layer_cost_spike → Investigation
    - extraction drift → Investigation
    - volume deviation → Investigation
    - rejection spike → Investigation
    """
    gen_id = event.details.get("generator_id") or ""
    all_related = [e for e in all_events if e.details.get("generator_id") == gen_id]

    if event.event_type in (
        "verification_reliability_drop",
        "auto_revert_rate_spike",
    ) and event.severity == "alert":
        # Throttle: reduce cadence step-down
        return _build_throttle(
            gen_id,
            ctx,
            throttle_type="reduce_cadence",
            new_value="weekly",
            reason=(
                f"{event.event_type}: "
                f"{event.details.get('delta', event.details.get('drop', '?'))}"
            ),
            related_events=all_related,
        )

    if event.event_type == "generator_dominance":
        return _build_throttle(
            gen_id,
            ctx,
            throttle_type="lower_confidence",
            new_value="0.8",
            reason=(
                f"generator_dominance: share="
                f"{event.details.get('share', '?'):.2f}"
            ),
            related_events=all_related,
        )

    # Everything else → Investigation routed to pod_operator
    return _build_investigation(event, ctx)


def _build_throttle(
    generator_id: str,
    ctx: EvolveWatchdogContext,
    *,
    throttle_type: str,
    new_value: str,
    reason: str,
    related_events: list[WatchdogEvent],
) -> Proposal:
    summary = f"Throttle {generator_id!r} ({throttle_type}): {reason}"
    op_summary = (
        f"The generator `{generator_id}` is misbehaving — the engine "
        f"saw {reason}. Throttling its {throttle_type} for two weeks "
        f"lets the rest of the engine keep running while you decide "
        f"whether to disable, retune, or fix the generator."
    )
    op_explanation = (
        f"Evolve Watchdog watches the engine itself — auto-revert "
        f"spikes, runaway dominance, verification failures, anything "
        f"that suggests one generator's behavior is undermining the "
        f"engine's overall trustworthiness. When it sees a pattern, "
        f"it proposes a throttle to take that generator out of the "
        f"loop temporarily.\n\n"
        f"Diagnosis. `{generator_id}` tripped the watchdog with: "
        f"{reason}. Without a throttle, the generator keeps emitting "
        f"and either fills the queue with noise or makes changes "
        f"that get auto-reverted.\n\n"
        f"What this changes. Sets the generator's throttle "
        f"({throttle_type}) to {new_value} for 14 days, then "
        f"auto-expires. Other generators are unaffected. Fully "
        f"reversible — the throttle is a metadata change, not a "
        f"code change.\n\n"
        f"What could go wrong. If the underlying signals were "
        f"genuine (cost truly spiked, verifications truly failed), "
        f"throttling masks the symptom without fixing the cause. "
        f"After the 14-day window the throttle expires and the same "
        f"pattern will reappear if nothing was fixed. Use the "
        f"window to investigate, not just to silence."
    )
    motivating: list[str] = []
    seen: set[str] = set()
    for e in related_events:
        sig_id = signal_id_for_event(e, ctx.shared_dir)
        if sig_id and sig_id not in seen:
            motivating.append(sig_id)
            seen.add(sig_id)
    return Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id or "<pod>",
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"watchdog_event:{e.id}" for e in related_events[:5]
        ],
        provenance=Provenance(
            technique="evolve_watchdog.throttle",
            signals={
                "target_generator": generator_id,
                "watchdog_events": [e.id for e in related_events],
            },
            confidence=0.9,
        ),
        problem=summary,
        action=ThrottleGenerator(
            generator_id=generator_id,
            throttle_type=throttle_type,  # type: ignore[arg-type]
            new_value=new_value,
            reason=reason,
            revert_after_days=14,  # auto-expire throttle
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["generator_record"],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="operational_urgent",
        admin_surface_summary=summary[:120],
        motivating_signals=motivating,
        # ── Phase C-10 operator-first content (Tier 1 — auto-apply) ─────
        summary=op_summary,
        explanation=op_explanation,
        action_label=f"Throttle {generator_id}",
        manual_path=f"Settings → Generators → {generator_id}",
        dismiss_signature=dismiss_signature_for_throttle(generator_id),
        dismiss_scope="kind",
    )


def _build_investigation(
    event: WatchdogEvent, ctx: EvolveWatchdogContext
) -> Proposal:
    summary = f"Watchdog event: {event.event_type} ({event.severity})"
    context = (
        f"Watchdog detected {event.event_type} at severity {event.severity}. "
        f"Details: {event.details}"
    )
    op_summary = (
        f"Evolve Watchdog flagged a `{event.event_type}` event at "
        f"{event.severity} severity. The engine isn't sure what to do "
        f"automatically — operator review is the right next step."
    )
    op_explanation = (
        f"Evolve Watchdog is the engine watching the engine. When it "
        f"sees a pattern that's worth attention but doesn't fit the "
        f"throttle-this-generator template, it surfaces an "
        f"Investigation so an operator can make the call.\n\n"
        f"Diagnosis. Event type: `{event.event_type}`. Severity: "
        f"{event.severity}. Details: {event.details}\n\n"
        f"What to do. Open the Watchdog event log to see the full "
        f"context. Most events here resolve to either: (1) a "
        f"generator that needs throttling — file a manual throttle "
        f"via the Generators page; (2) a misconfiguration that "
        f"needs fixing upstream; (3) noise that can be dismissed.\n\n"
        f"What could go wrong. Dismissing a real watchdog finding "
        f"silences the engine's own self-monitoring. If you dismiss, "
        f"prefer recording an intent (\"this pattern is expected on "
        f"this bot for this reason\") so future signals know to "
        f"stay quiet."
    )
    sig_id = signal_id_for_event(event, ctx.shared_dir)
    motivating = [sig_id] if sig_id else []
    return Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id or "<pod>",
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"watchdog_event:{event.id}"],
        provenance=Provenance(
            technique=f"evolve_watchdog.{event.event_type}",
            signals={"event_id": event.id, "severity": event.severity, **event.details},
            confidence=0.85,
        ),
        problem=summary,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="platform",
            reversibility="manual",
            touches=[],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="operational_urgent" if event.severity == "alert" else "substrate_warn",
        admin_surface_summary=summary[:120],
        motivating_signals=motivating,
        # ── Phase C-10 operator-first content (Tier 2 — UI manual) ──────
        summary=op_summary,
        explanation=op_explanation,
        action_label="Open Watchdog log",
        manual_path="Settings → Watchdog",
        dismiss_signature=dismiss_signature_for_investigation(event.event_type),
        dismiss_scope="kind",
    )


# ─────────────────────────────────────────────────────────────────────────────
# observe()
# ─────────────────────────────────────────────────────────────────────────────


def observe(ctx: EvolveWatchdogContext) -> list[Proposal]:
    """Run all detectors, record events, emit proposals."""
    global _ACTIVE_CLOCK
    _ACTIVE_CLOCK = ctx.now

    all_events: list[WatchdogEvent] = []
    try:
        for detector in _ALL_DETECTORS:
            try:
                events = detector(ctx)
            except Exception:  # noqa: BLE001 — detector bugs shouldn't crash Watchdog
                events = []
            all_events.extend(events)
    finally:
        _ACTIVE_CLOCK = None

    # Record all events to disk (even info-level ones, for audit).
    # Pass ctx.now so tests and time-frozen runs write to the expected
    # daily file rather than real-time today.
    if all_events:
        store = WatchdogEventStore(shared_dir=ctx.shared_dir, day=ctx.now)
        for e in all_events:
            store.record(e)
        store.flush()

    # Phase 6c cutover: emit each Proposal as a CandidateProposal and
    # return []. The substantiveness gate (run from generator_runner)
    # promotes promotable candidates to Proposals.
    proposals = _build_proposals_for_events(all_events, ctx)
    # Phase A.5 — filter dismissed signatures before emission.
    from arbiter.dismissals import filter_dismissed
    proposals = filter_dismissed(proposals, ctx.shared_dir, ctx.bot_id)
    for p in proposals:
        tech = (p.provenance.technique or "").split(".", 1)[-1]
        event_ids = p.provenance.signals.get("event_ids") or []
        emit_from_proposal(
            p,
            variant=tech or "watchdog_finding",
            magnitude=Magnitude(unit="count", value=float(len(event_ids) or 1)),
            shared_dir=ctx.shared_dir,
        )
    return []


