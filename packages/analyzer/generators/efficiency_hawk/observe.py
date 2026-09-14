"""generators.efficiency_hawk.observe — Detector entry point.

Two detector families live here:

  1. **Cluster-engagement outliers** (this module's ``_observe_clusters``)
     — original detector. Flags ``(noun, verb)`` clusters whose
     engagement-per-session is unusually high vs. the bot's other
     clusters. Emits ``AgentsAppend`` streamline notes.

  2. **Cost-ledger detectors** (``cost_efficiency.run_all_detectors``)
     — newer family. Reads cost_event records (and session_summary
     records when ``session_reader`` is wired) and flags spend
     patterns that signal inefficient operation. Currently:
     background-trigger_kind dominance and tier-misrouting on
     maintenance sessions.

``observe(ctx)`` is the single entry point the runner invokes; it
dispatches to both families and concatenates their proposals.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from generators.efficiency_hawk.cost_efficiency import (
    CostEfficiencyContext,
    run_all_detectors as run_cost_detectors,
)
from generators.efficiency_hawk.signal_proposals import (
    SIGNAL_TYPE_TO_FACTORY,
    DISMISS_SIG_AUTOMATION_DOMINANCE,
    DISMISS_SIG_DAILY_SPEND_HIGH,
    DISMISS_SIG_HEARTBEAT_NO_OVERRIDE,
    DISMISS_SIG_SESSION_TOKEN_OUTLIER,
    dismiss_signature_for_context_bloat,
    dismiss_signature_for_cron_overactive,
    dismiss_signature_for_cron_wakes,
)
from metrics.registry import parameterized
from observations.access import ObservationWindow
from proposal_synthesizer.emit import (
    emit_from_proposal,
    emit_from_signal_proposal,
)
from schema.candidate_proposal import Magnitude
from schema.observation import ClusterSpec
from schema.proposal import (
    AgentsAppend,
    Claim,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "efficiency_hawk"
DIMENSION = "efficiency"

# Producer name on Signals emitted by the cost_watchdog monitor. This
# generator subscribes to those Signals and turns each into a Proposal.
COST_WATCHDOG_PRODUCER = "cost_watchdog"

# ledger_reader: (bot_id, days_back) -> Iterable of cost_event dicts
LedgerReader = Callable[[str, int], Iterable[dict]]
# session_reader: (bot_id, days_back) -> Iterable of session_summary dicts
SessionReader = Callable[[str, int], Iterable[dict]]

# Tunables on CostEfficiencyContext that the runner may override via
# the generator's record.config. Listed explicitly so an unrelated key
# in gen_config doesn't get silently set on the context.
_COST_OVERRIDE_FIELDS = (
    # Background-dominance tunables
    "lookback_days",
    "min_total_cost_usd",
    "min_classified_share",
    "min_distinct_days",
    "background_share_threshold",
    # Tier-misrouting tunables
    "tier_min_maintenance_sessions",
    "tier_min_classified_share",
    "tier_min_high_tier_cost_usd",
    "tier_high_tier_share_threshold",
    "tier_proposed_target_tier",
)


@dataclass
class EfficiencyHawkContext:
    bot_id: str
    window: ObservationWindow
    min_cluster_sessions: int = 5  # need enough data to compare
    stdev_threshold: float = 1.5  # flag clusters > this many stdev above mean
    max_proposals_per_run: int = 1
    audience: str = "bot_primary_user"
    # Optional: cost-ledger access. When unset the cost detectors are
    # skipped — keeps the original cluster-engagement tests working
    # without forcing them to wire ledger access.
    ledger_reader: LedgerReader | None = None
    # Optional: session_summary access. Required by tier-misrouting
    # only; background-dominance runs from the ledger alone. When
    # unset, tier-misrouting is silently skipped.
    session_reader: SessionReader | None = None
    # Hard skip for cost detectors regardless of ledger_reader. Set
    # per-bot via gen_config["cost_disabled"] when a member bot does
    # intentional background work and shouldn't be alerted on.
    cost_detectors_disabled: bool = False
    # Per-tunable overrides for the cost detector. Keys must be in
    # ``_COST_OVERRIDE_FIELDS``; missing keys mean "use the
    # CostEfficiencyContext default".
    cost_overrides: dict | None = None
    # Shared dir — required to read cost_watchdog Signals. When unset
    # the signal-driven detectors are skipped (keeps tests that don't
    # exercise them simple).
    shared_dir: Path | None = None


def observe(ctx: EfficiencyHawkContext) -> list[Proposal]:
    """Dispatch to every detector family.

    Phase 2 cutover: efficiency_hawk emits CandidateProposals to
    ``candidates/pending/`` (as a side effect of each detector) and
    no longer returns Proposals via this top-level function. The
    proposal_synthesizer gate promotes passed candidates to
    Proposals — that is the only path by which efficiency_hawk
    findings now reach the operator queue.

    Returning ``[]`` from a generator's ``observe()`` is how the
    generator opts out of the legacy Proposal-write path managed by
    generator_runner. Tests that need to inspect detector behavior
    call ``_observe_clusters`` / ``_observe_from_signals`` /
    ``run_cost_detectors`` directly.
    """
    _observe_clusters(ctx)  # side effect: writes candidates
    if ctx.shared_dir is not None:
        _observe_from_signals(ctx)  # side effect: writes candidates
    if ctx.ledger_reader is not None and not ctx.cost_detectors_disabled:
        cost_ctx = CostEfficiencyContext(
            bot_id=ctx.bot_id,
            ledger_reader=ctx.ledger_reader,
            session_reader=ctx.session_reader,
            shared_dir=ctx.shared_dir,
        )
        if ctx.cost_overrides:
            for name in _COST_OVERRIDE_FIELDS:
                if name in ctx.cost_overrides:
                    try:
                        coerced = type(getattr(cost_ctx, name))(
                            ctx.cost_overrides[name]
                        )
                        setattr(cost_ctx, name, coerced)
                    except (TypeError, ValueError):
                        # Bad config value — silently keep the default
                        # rather than crash the generator run.
                        pass
        run_cost_detectors(cost_ctx)  # side effect: writes candidates
    return []


def _observe_from_signals(ctx: EfficiencyHawkContext) -> list[Proposal]:
    """Read firing Signals from cost_watchdog and produce one Proposal each.

    Filters by ``bot_id`` so a per-bot generator run only emits proposals
    for its own bot. Signal types not in
    :data:`signal_proposals.SIGNAL_TYPE_TO_FACTORY` are silently skipped
    (keeps the generator forward-compatible with new monitor types).
    """
    if ctx.shared_dir is None:
        return []
    try:
        from signals import store as signals_store
    except ImportError:
        return []

    # Phase A.5 — preload active dismiss signatures once per observe()
    # run so per-signal checks are O(1).
    from arbiter.dismissals import preload_suppressed_signatures
    suppressed = preload_suppressed_signatures(ctx.shared_dir, ctx.bot_id)

    proposals: list[Proposal] = []
    for sig in signals_store.iter_active(
        ctx.shared_dir,
        producer=COST_WATCHDOG_PRODUCER,
        bot_id=ctx.bot_id,
        state="firing",
    ):
        factory = SIGNAL_TYPE_TO_FACTORY.get(sig.type)
        if factory is None:
            continue
        try:
            proposal = factory(sig)
        except Exception:
            # A bad payload on one signal must not torpedo the rest.
            continue
        # Phase A.5 dismiss-suppression gate. Per-signature granularity
        # (see signal_proposals.py for the kind/sub-id breakdown).
        if proposal.dismiss_signature in suppressed:
            continue
        proposals.append(proposal)
        # Phase 1 shadow emission — parallel CandidateProposal write.
        # Never raises into the proposal pipeline (helper swallows).
        emit_from_signal_proposal(proposal, sig, shared_dir=ctx.shared_dir)
    return proposals


def _observe_clusters(ctx: EfficiencyHawkContext) -> list[Proposal]:
    """Original cluster-engagement-outlier detector."""
    view = ctx.window.clusters(ClusterSpec(group_by=["noun", "verb"]))
    if len(view) < 3:
        # Not enough clusters to compute a meaningful mean/stdev
        return []

    # Compute per-cluster engagement-per-session
    ratios: list[tuple[tuple, float]] = []
    for cell in view.cells:
        if cell.distinct_sessions < ctx.min_cluster_sessions:
            continue
        ratio = cell.engagement_total / cell.distinct_sessions
        ratios.append((cell.key, ratio))

    if len(ratios) < 3:
        return []

    values = [r for _, r in ratios]
    mean = statistics.mean(values)
    try:
        stdev = statistics.stdev(values)
    except statistics.StatisticsError:
        stdev = 0.0

    if stdev <= 0:
        return []

    # Flag outliers: engagement-per-session > mean + stdev_threshold * stdev
    threshold = mean + ctx.stdev_threshold * stdev
    outliers = [
        (key, ratio)
        for key, ratio in ratios
        if ratio > threshold
    ]
    if not outliers:
        return []

    # Sort worst first, cap
    outliers.sort(key=lambda t: -t[1])
    outliers = outliers[: ctx.max_proposals_per_run]

    proposals: list[Proposal] = []
    for key, ratio in outliers:
        p = _build_streamline_proposal(ctx, key, ratio, mean, stdev)
        proposals.append(p)
        # Phase 1 shadow emission. Magnitude here is the engagement
        # multiplier — how many times the pod average this cluster
        # runs. Floor is set in proposal_synthesizer.config so a 1.5x
        # outlier doesn't churn the queue.
        emit_from_proposal(
            p,
            variant="cluster_outlier",
            magnitude=Magnitude(unit="ratio_over_mean", value=ratio / mean if mean else 0.0),
            shared_dir=ctx.shared_dir,
        )
    return proposals


def _build_streamline_proposal(
    ctx: EfficiencyHawkContext,
    cluster_key: tuple,
    ratio: float,
    mean: float,
    stdev: float,
) -> Proposal:
    noun = str(cluster_key[0]) if len(cluster_key) >= 1 else "unknown"
    verb = str(cluster_key[1]) if len(cluster_key) >= 2 else "exploring"

    summary = (
        f"Inefficient pattern in ({noun}, {verb}): "
        f"{ratio:.1f} turns/session (pod avg {mean:.1f})"
    )
    headline = (
        f"Streamline {ctx.bot_id} ({noun}, {verb}) conversations "
        f"— {ratio:.1f} turns/session vs pod avg {mean:.1f}"
    )

    guidance = (
        f"## Streamline — {noun} / {verb}\n\n"
        f"Sessions about {verb} in {noun} average "
        f"{ratio:.1f} turns versus the bot-wide mean of {mean:.1f}. "
        "Consider:\n\n"
        f"- Preloading context the bot commonly asks for during {verb} tasks\n"
        "- Checking whether a workflow doc could capture the usual sequence\n"
        "- If a specific app handles this cluster, widening its scope to "
        "reduce tool hopping\n"
    )

    # Claim: the cluster's engagement-per-session (turns/session) should drop
    # toward the bot-wide mean within two weeks. We claim against the LEVEL
    # metric — same shape as the baseline (engagement_total / distinct_sessions),
    # carrying the cell identity as metric params — so the verify daemon compares
    # like-for-like. Success bar: close at least half the gap to the mean
    # (floored at 1 turn), since the detector only flags genuine outliers.
    claim = Claim(
        metric=parameterized("cluster.engagement_level", noun=noun, verb=verb),
        direction="down",
        magnitude=round(max(1.0, 0.5 * (ratio - mean)), 4),
        window_days=14,
        baseline=round(ratio, 4),
        fallback="revert",
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Identity-only: trigger_observations feed the dedup fingerprint, so
        # measured values (ratio, stdev distance) must NOT appear here — a
        # value-bearing trigger mints a fresh proposal on every daily
        # re-measurement instead of refreshing the open one. The current
        # numbers live in provenance.signals and the Claim, which
        # ingest._refresh_existing overwrites on each re-detection.
        trigger_observations=[
            f"cluster:{noun}:{verb}",
        ],
        provenance=Provenance(
            technique="efficiency_hawk.high_turn_count",
            signals={
                "noun": noun,
                "verb": verb,
                "engagement_per_session": ratio,
                "pod_mean": mean,
                "pod_stdev": stdev,
            },
            confidence=0.75,
        ),
        problem=summary,
        action=AgentsAppend(
            bot_id=ctx.bot_id,
            section=f"Streamline — {noun}",
            content=guidance,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["agents_doc"],
        ),
        claim=claim,
        approval_audience=ctx.audience,  # type: ignore[arg-type]
        urgency="improvement",
        admin_surface_summary=headline[:120],
        conversational_pitch=(
            f"Our {verb} conversations about {noun} tend to run long. "
            "Want me to add a note to streamline them?"
        ),
    )
