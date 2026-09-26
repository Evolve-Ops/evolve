"""generators.budget_hawk.observe — Cost-threshold + anomaly detectors.

Budget Hawk reads cost data from up to three optional sources:
  1. An injected ``spend_reader`` callable that returns daily cost data
     for a bot. Tests mock this; production wires to measure.py's metrics
     files. Pure pattern matching — no LLM.
  2. An injected ``ledger_reader`` + ``session_reader`` pair used by the
     v2 forensics detectors in ``forensics.py``. Both are optional — when
     unset the forensics pass is skipped and we only run the aggregate
     threshold logic below.
  3. The Better Engine config to find the bot's per-bot_daily_warn_usd
     and per_bot_daily_hard_usd thresholds.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from better_engine_config import BetterEngineConfig, load as load_config
from generators.budget_hawk.forensics import ForensicsContext, run_all_detectors
from generators.budget_hawk.proposals import (
    make_tier_downgrade,
    make_warn_pattern_investigation,
)
from generators.budget_hawk.signals import (
    cost_anomaly_signal_kwargs,
    warn_cap_signal_kwargs,
)
from proposal_synthesizer.emit import emit_from_proposal
from schema.candidate_proposal import Magnitude
from schema.proposal import Proposal


# spend_reader:   (bot_id, days_back) -> list of (date_iso, usd_amount)
# ledger_reader:  (bot_id, days_back) -> iterable of cost_event dicts
# session_reader: (bot_id, days_back) -> iterable of session_summary dicts
SpendReader = Callable[[str, int], list[tuple[str, float]]]
LedgerReader = Callable[[str, int], Iterable[dict]]
SessionReader = Callable[[str, int], Iterable[dict]]


@dataclass
class BudgetHawkContext:
    bot_id: str
    config: BetterEngineConfig
    spend_reader: SpendReader
    # v2 forensics readers — optional so legacy tests + callers that only
    # want threshold/anomaly detection don't have to wire ledger access.
    ledger_reader: Optional[LedgerReader] = None
    session_reader: Optional[SessionReader] = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Tunable defaults live in the generator's evolvable config (not charter).
    # For tests, set directly.
    anomaly_stdev_threshold: float = 2.0
    anomaly_min_absolute_usd: float = 0.50
    lookback_days: int = 7
    # Prior observation counts for threshold-crossing signals, keyed by signal
    # type (e.g. "warn_cap_crossed"). Injected by the runner from the signal
    # store BEFORE observe_signals() bumps the counts, so observe() can detect
    # recurring patterns and escalate to a proposal.
    active_cap_signal_observations: dict = field(default_factory=dict)
    # Minimum prior observation count before observe() emits a pattern
    # investigation proposal. Default 3 means: propose on the 4th+ firing.
    pattern_analysis_threshold: int = 3
    # Optional — when set, observe() also emits CandidateProposals to
    # ``candidates/pending/`` for the substantiveness gate. The legacy
    # direct-write path stays active until cutover lands in a follow-up.
    shared_dir: Path | None = None


def _spend_split(
    ctx: BudgetHawkContext,
) -> tuple[float | None, list[float]]:
    """Read spend data and split into today's value and historical list."""
    spend = ctx.spend_reader(ctx.bot_id, ctx.lookback_days)
    if not spend:
        return None, []
    today_iso = ctx.now.date().isoformat()
    today_usd: float | None = None
    history: list[float] = []
    for date_iso, usd in spend:
        if date_iso == today_iso:
            today_usd = usd
        else:
            history.append(usd)
    return today_usd, history


def observe_signals(ctx: BudgetHawkContext) -> list[dict]:
    """Return signal specs for threshold crossings.

    Threshold crossings are observations, not actions — they go to the
    Signal store (Alerts page), not the Proposal queue. The runner emits
    these via ``signals.store.observe(**spec)`` after calling this function,
    then calls ``observe()`` to get any actionable proposals.

    Detection rules:
      - today's spend >= hard cap  → no signal (TierAdjustment proposal handles it)
      - today's spend >= warn cap  → warn_cap_crossed signal
      - today's spend is a statistical anomaly vs. rolling mean → cost_anomaly signal
    """
    today_usd, history = _spend_split(ctx)
    if today_usd is None:
        return []

    warn_cap = ctx.config.budget_warn_cap_usd(ctx.bot_id)
    hard_cap = ctx.config.budget_hard_cap_usd(ctx.bot_id)

    specs: list[dict] = []

    if today_usd >= hard_cap and hard_cap > 0:
        # Hard cap → TierAdjustment proposal from observe(); no signal needed.
        pass
    elif today_usd >= warn_cap and warn_cap > 0:
        specs.append(
            warn_cap_signal_kwargs(
                ctx.bot_id, current_usd=today_usd, cap_usd=warn_cap
            )
        )

    # Statistical anomaly (independent of cap; only if cap didn't already fire)
    if not specs and len(history) >= 3 and today_usd >= ctx.anomaly_min_absolute_usd:
        mean_usd = statistics.mean(history)
        try:
            stdev = statistics.stdev(history)
        except statistics.StatisticsError:
            stdev = 0.0
        if stdev > 0 and (today_usd - mean_usd) / stdev >= ctx.anomaly_stdev_threshold:
            specs.append(
                cost_anomaly_signal_kwargs(
                    ctx.bot_id,
                    current_usd=today_usd,
                    mean_usd=mean_usd,
                    stdevs=(today_usd - mean_usd) / stdev,
                )
            )

    return specs


def observe(ctx: BudgetHawkContext) -> list[Proposal]:
    """Return proposals for a bot based on recent spend.

    Raw threshold crossings route to Signals via ``observe_signals()``; this
    function handles actionable proposals only:

      - Hard cap crossed → TierAdjustment (downgrade maintenance tier)
      - Warn cap crossed repeatedly (>= pattern_analysis_threshold prior
        observations) → Investigation proposal asking the operator to review
        cost drivers or adjust the cap
      - v2 forensics detectors (summarizer waste, classifier noise, app
        attribution) → ConfigPatch / Investigation proposals
    """
    today_usd, _ = _spend_split(ctx)
    if today_usd is None:
        return []

    warn_cap = ctx.config.budget_warn_cap_usd(ctx.bot_id)
    hard_cap = ctx.config.budget_hard_cap_usd(ctx.bot_id)

    proposals: list[Proposal] = []

    if today_usd >= hard_cap and hard_cap > 0:
        proposals.append(
            make_tier_downgrade(
                ctx.bot_id,
                target_class="maintenance",
                new_tier="haiku",
            )
        )
    elif today_usd >= warn_cap and warn_cap > 0:
        # Pattern detection: if the warn_cap signal has fired enough times
        # before this cycle, escalate from observation to actionable proposal.
        # ``active_cap_signal_observations`` holds the count *before*
        # observe_signals() bumped it this cycle.
        # (Rejection cooldown is enforced at the runner level — see
        # generator_runner.run_generators — so this code stays focused on
        # detection. A freshly-dismissed proposal won't be re-queued even
        # though we emit it here.)
        prior_obs = ctx.active_cap_signal_observations.get("warn_cap_crossed", 0)
        if prior_obs >= ctx.pattern_analysis_threshold:
            proposals.append(
                make_warn_pattern_investigation(
                    ctx.bot_id,
                    current_usd=today_usd,
                    cap_usd=warn_cap,
                    observation_count=prior_obs + 1,
                )
            )

    # ── v2 forensics ─────────────────────────────────────────────────────────
    # Cost-lineage-backed detectors complement the aggregate threshold logic:
    # aggregate catches "spent too much", forensics catches "spent on the wrong
    # thing". Run only when the caller has wired ledger access.
    if ctx.ledger_reader is not None and ctx.session_reader is not None:
        fx_ctx = ForensicsContext(
            bot_id=ctx.bot_id,
            ledger_reader=ctx.ledger_reader,
            session_reader=ctx.session_reader,
            lookback_days=ctx.lookback_days,
            now=ctx.now,
        )
        proposals.extend(run_all_detectors(fx_ctx))

    # Phase A.5 — filter dismissed signatures before emission. Per-bot
    # scoping flows through the store layer; pod-wide entries match
    # every bot. Fail-open on read failure.
    from arbiter.dismissals import filter_dismissed
    proposals = filter_dismissed(proposals, ctx.shared_dir, ctx.bot_id)

    # Phase 6c cutover: emit each Proposal as a CandidateProposal and
    # return []. The substantiveness gate (run from generator_runner)
    # promotes promotable candidates to Proposals. Variant derived
    # from the proposal's provenance.technique tail.
    for p in proposals:
        tech = (p.provenance.technique or "").split(".", 1)[-1]
        emit_from_proposal(
            p,
            variant=tech or "budget_finding",
            magnitude=Magnitude(unit="count", value=1.0),
            shared_dir=ctx.shared_dir,
        )

    return []


