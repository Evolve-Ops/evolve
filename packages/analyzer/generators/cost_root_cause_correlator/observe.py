"""generators.cost_root_cause_correlator.observe — Synthesize.

The synthesis pattern in one paragraph: when an acute cost signal
fires on a bot, investigate whether a chronic pattern on the SAME bot
explains the spike, and whether there is a typed fix the system knows
how to apply. When all three line up — acute + chronic + typed fix —
emit one Proposal that names both findings and dispatches the fix.
Otherwise stay silent; the existing per-signal Proposals from
cost_spike, cache_ttl_tuner, etc. continue to surface the individual
findings.

The closure (closing the RSI loop on the motivating team-bot-a session
3d5cde22 incident) is the synthesis Proposal itself: previously the
operator had to manually correlate "acute outlier on bot X" + "chronic
cache invalidation on bot X" + "we have a knob, PR A added it, PR B
made it autonomous" — now the system does the correlation and ships a
single Proposal with the typed fix and motivating_signals pointing
back to both originals.

Extending this generator to other correlations (heartbeat bloat,
model-tier drift, …) means appending a new ``Correlation`` entry to
``correlations.REGISTRY``. The observer here is the dispatcher shell;
it never needs to grow.

Phase 2 reference patterns it borrows:

  * Investigate-before-propose (smarter-generators sprint, PR #1700
    series) — gather evidence via the investigation toolkit before
    deciding to emit.
  * proposal_history dedupe — re-emitting the same correlation when
    an open or recently-applied Proposal already covers it is noise;
    suppress.
  * config_intent respect — an operator who deliberately pinned
    ``cacheRetention=short`` for a bot should not see the generator
    repeatedly proposing the inverse. Skip when intent is recorded
    even when the chronic signal is still firing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from generators.cost_root_cause_correlator.correlations import (
    CorrelationMatch,
    REGISTRY,
    dispatch,
)
from investigation.proposal_history import (
    operator_already_declined,
    proposal_history,
    summarize_history,
)
from investigation.toolkit import (
    CorrelatedSignal,
    config_intent,
    correlated_signals,
)
from schema.proposal import (
    Claim,
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    UpdateAgentDefaults,
    new_proposal_id,
)


GENERATOR_ID = "cost_root_cause_correlator"
DIMENSION = "cost"

COST_WATCHDOG_PRODUCER = "cost_watchdog"
SESSION_ECONOMICS_PRODUCER = "session_economics"

# Acute Signal types we look at. The synthesis runs whenever any of
# these fires on a bot, then asks the correlations registry which (if
# any) chronic pattern explains it.
#
# session_token_outlier: the motivating example — one session priced
#   far above the bot's median. The biggest "this is fixable" trigger
#   in the team-bot-a 3d5cde22 incident.
# cost_spike: 7-day cost step-change. Often correlated with the same
#   chronic patterns (cache invalidation accumulates over the window).
# daily_spend_high: today's absolute spend over threshold. Same logic.
ACUTE_SIGNAL_TYPES: tuple[str, ...] = (
    "session_token_outlier",
    "cost_spike",
    "daily_spend_high",
)


# How fresh a prior Proposal must be for us to treat it as "already
# covered" (skip re-emission). 7 days mirrors the bot's cache-policy
# decision horizon — if the operator approved a flip a week ago we
# should not be re-proposing the same flip even if a new acute signal
# fires.
DEDUP_WINDOW_DAYS = 7


@dataclass
class CostRootCauseCorrelatorContext:
    """Per-bot run context. ``shared_dir`` locates the Signal + Proposal
    + config_intent stores; ``bot_id`` filters Signals."""

    bot_id: str
    shared_dir: Path
    # When True (production), proposal_history is consulted to suppress
    # repeat emissions. Tests disable so they don't have to seed history
    # for each unrelated assertion.
    consult_proposal_history: bool = True
    # Now-injection for deterministic dedup window computation in tests.
    now: datetime | None = None
    # Openclaw.json reader. Production resolves through bot_home(); tests
    # supply a synthetic dict to keep the run hermetic.
    openclaw_reader: Any | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _read_openclaw_config(
    bot_id: str, reader: Any | None,
) -> dict:
    """Read the bot's openclaw.json. Returns ``{}`` on any read failure
    so the dispatcher can decide whether the bot's config shape lets it
    synthesize — the per-correlation detector decides what to do with
    an empty/unrecognized config.

    ``reader`` is the test-injected callable taking ``bot_id`` and
    returning a dict. Production uses bot_home + the writer's sudo-cat
    fallback so an evolve-readable bot tree works directly.
    """
    if reader is not None:
        try:
            cfg = reader(bot_id)
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}
    try:
        from evolve_config import bot_home
        home = bot_home(bot_id)
    except Exception:
        return {}
    try:
        from permissions.writer import read_openclaw_json
        cfg = read_openclaw_json(home / ".openclaw" / "openclaw.json")
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _firing_acute_signals(
    shared_dir: Path, bot_id: str,
) -> list[CorrelatedSignal]:
    """Acute signals from cost_watchdog on this bot, filtered to the
    types the correlator knows how to synthesize."""
    return correlated_signals(
        shared_dir,
        bot_id,
        producer=COST_WATCHDOG_PRODUCER,
        types=list(ACUTE_SIGNAL_TYPES),
    )


def _firing_chronic_signals(
    shared_dir: Path, bot_id: str,
) -> list[CorrelatedSignal]:
    """Chronic signals from every producer the registry knows about.

    Today this collapses to session_economics + cache_invalidation_elevated,
    but we read the union from the registry so adding a new chronic
    producer to a future correlation doesn't require a code edit here.
    """
    chronic: list[CorrelatedSignal] = []
    seen_keys: set[tuple[str, str]] = set()
    for corr in REGISTRY:
        for sig in correlated_signals(
            shared_dir,
            bot_id,
            producer=corr.chronic_producer,
            types=list(corr.chronic_types),
        ):
            key = (sig.producer, sig.signature or sig.signal_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            chronic.append(sig)
    return chronic


def _operator_pinned_short(
    shared_dir: Path,
    bot_id: str,
    match: CorrelationMatch,
) -> bool:
    """True when the operator has recorded a config_intent pinning the
    correlation's target field at a value matching the *current* state
    — i.e. they've affirmed the current setting and a revert proposal
    would contradict their intent.

    For the cache-invalidation correlation this is: intent recorded
    on ``agents.defaults.models.*.params.cacheRetention`` with value
    ``"short"``. Memory note ``feedback_generators_consider_intent``:
    drift-revert generators that ignore config_intent ship contextually
    wrong proposals once deliberate deviations exist.
    """
    if match.cause_key != "cache_invalidation_drives_outliers":
        # Other correlations carry their own intent semantics. Until a
        # second correlation exists, the early return keeps the
        # operator_pinned check honest about what it knows.
        return False
    # Look up the intent on the same dotpath the applier records under.
    # See arbiter.appliers.agent_defaults._record_applier_intents: it
    # writes one intent per logical dotpath (not per fanned-out model).
    intent = config_intent(
        bot_id,
        "agents.defaults.models.*.params.cacheRetention",
        shared_dir=shared_dir,
    )
    if intent is None:
        return False
    # The recorded value is what the operator (or a prior applier run)
    # chose. We're trying to propose the inverse — if the recorded
    # value matches what we WOULD set (long), the fix already happened
    # via a different path and we have nothing to add. If it matches
    # the value we'd be flipping AWAY from (short), the operator (or
    # an earlier rejection-and-reset) deliberately picked it; respect
    # that choice.
    raw_value = (intent.raw or {}).get("value")
    if not isinstance(raw_value, str):
        return False
    return raw_value == "short"


def _build_proposal(
    bot_id: str,
    match: CorrelationMatch,
    history_summary: Any | None,
) -> Proposal:
    """Compose the synthesis Proposal.

    Body lives in ``problem`` (the admin UI's UpdateAgentDefaults
    renderer surfaces problem above the structured Action panel — see
    feedback_proposal_body_in_problem_field). When the correlation has
    no autonomous fix, we fall back to Investigation with the body in
    ``action.context`` (per the existing Investigation convention).
    """
    motivating_ids: list[str] = []
    if match.acute.signal_id:
        motivating_ids.append(match.acute.signal_id)
    for c in match.chronic:
        if c.signal_id:
            motivating_ids.append(c.signal_id)

    sig_types = sorted({s.type for s in [match.acute, *match.chronic]})

    provenance_signals: dict[str, Any] = {
        "primary_signal_types": sig_types,
        "primary_signal_signatures": sorted(
            {
                s.signature
                for s in [match.acute, *match.chronic]
                if s.signature
            }
        ),
        # root_cause_attribution mirrors the smarter-generators shape
        # (bloat_investigator, exec_outcome_investigator). The
        # smarter-generators spec calls this out as the auditable
        # explanation of why the Proposal exists.
        "root_cause_attribution": {
            "cause_key": match.cause_key,
            "headline": match.headline,
            "confidence": match.confidence,
            "primary_target": match.acute.details.get("bot_id") or bot_id,
            "evidence": match.evidence,
        },
        "acute_signal_id": match.acute.signal_id,
        "chronic_signal_ids": [c.signal_id for c in match.chronic if c.signal_id],
    }
    if history_summary is not None:
        provenance_signals["history"] = {
            "total": history_summary.total,
            "declined": history_summary.declined,
            "approved": history_summary.approved,
            "most_recent_status": history_summary.most_recent_status,
        }

    # Build the Action + risk_tag. UpdateAgentDefaults when there's a
    # typed fix the registry knows about; Investigation otherwise.
    body = match.body
    headline_short = match.headline[:120]

    if match.fields:
        action: Any = UpdateAgentDefaults(
            bot_id=bot_id,
            fields=dict(match.fields),
        )
        # Per-bot, single-enum knob: blast_radius=bot, reversibility=auto.
        # touches=[] — the agents.defaults.* surface is NOT in
        # IRREVERSIBILITY_SURFACES (auth/tools/channel_config/...).
        risk_tag = RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=[],
        )
        # Per-bot single-knob cost-down change → falsifiable. Magnitude
        # is intentionally small so the verify daemon doesn't reject
        # the apply over noisy day-to-day cost drift. baseline=0 because
        # the L2 applier captures the live reading at apply time
        # (mirrors budget_hawk's pattern).
        claim: Claim | None = Claim(
            metric="cost.daily_usd",
            direction="down",
            magnitude=0.25,
            window_days=7,
            baseline=0.0,
            fallback="revert",
        )
        # Hygiene urgency so the auto-small tier-floor rule applies
        # (see eligibility._tier_floor: low risk + magnitude≤1 →
        # auto-small). The motivating example is a bot-level
        # optimization not a security alert.
        urgency = "hygiene"
        # Problem carries the operator-facing body (the UI's
        # UpdateAgentDefaults renderer drops free-text from
        # action.context for typed actions — see
        # feedback_proposal_body_in_problem_field).
        problem_field = body
    else:
        action = Investigation(context=body)
        risk_tag = RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=[],
        )
        claim = None
        urgency = "improvement"
        problem_field = match.headline

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"{match.acute.type}:{bot_id}",
            *[f"{c.type}:{bot_id}" for c in match.chronic],
        ],
        provenance=Provenance(
            technique=f"cost_root_cause_correlator.{match.cause_key}",
            signals=provenance_signals,
            confidence=match.confidence,
        ),
        problem=problem_field,
        action=action,
        risk_tag=risk_tag,
        claim=claim,
        approval_audience="pod_operator",
        urgency=urgency,
        admin_surface_summary=headline_short,
        motivating_signals=motivating_ids,
        dismiss_signature=f"cost_root_cause_correlator:{match.cause_key}",
        dismiss_scope="kind",
    )


def _recent_proposal_exists(
    shared_dir: Path,
    bot_id: str,
    cause_key: str,
    *,
    now: datetime,
    window_days: int = DEDUP_WINDOW_DAYS,
) -> bool:
    """True when a proposal with this cause_key landed within ``window_days``
    AND is still open or recently applied.

    "Open" (pending/snoozed) means the operator hasn't decided yet —
    re-emitting wouldn't add information. "Recently applied" means the
    fix has been pushed; if the chronic signal hasn't cleared yet
    that's a verification-window issue, not a "needs another proposal"
    issue.

    Declined / rejected / dismissed proposals are intentionally NOT
    treated as covering — the operator said no, but if the conditions
    are still firing they might be ready to reconsider. The repeat-
    rejection guard (``operator_already_declined``, ≥2 declines) is the
    backstop for that "stop bothering me" case.
    """
    try:
        entries = proposal_history(
            shared_dir,
            bot_id=bot_id,
            cause_key=cause_key,
            limit=10,
        )
    except Exception:
        return False
    cutoff = now - _timedelta_days(window_days)
    fresh_statuses = {
        "pending", "snoozed", "approved_auto", "approved_human",
        "applied", "succeeded",
    }
    for e in entries:
        if e.status not in fresh_statuses:
            continue
        ts = _parse_iso(e.created_at)
        if ts is None:
            continue
        if ts >= cutoff:
            return True
    return False


def _timedelta_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)


def _parse_iso(s: str) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        # ISO with optional 'Z' suffix
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def observe(ctx: CostRootCauseCorrelatorContext) -> list[Proposal]:
    """Emit at most one Proposal per (bot, correlation) per run.

    Algorithm:
      1. Pull firing acute signals from cost_watchdog.
      2. Pull firing chronic signals from every producer the registry
         consumes.
      3. Read the bot's openclaw.json so detectors can inspect current
         config state.
      4. For each acute signal, ask the correlation dispatcher whether
         any chronic pattern + config state explains it.
      5. Dedupe via proposal_history (skip if a fresh open/applied
         Proposal already covers this cause_key on this bot).
      6. Respect config_intent (skip if operator pinned the current
         value for this correlation's target field).
      7. Respect repeat-rejection (skip if operator has declined ≥2×
         recently — same guard as bloat_investigator / exec_outcome_).
      8. Build and return the Proposal(s). De-dupe across multiple
         acute signals firing on the same correlation: at most one
         Proposal per cause_key per run.
    """
    if ctx.shared_dir is None or not ctx.bot_id:
        return []

    now = ctx.now or datetime.now(timezone.utc)

    try:
        acute_sigs = _firing_acute_signals(ctx.shared_dir, ctx.bot_id)
    except Exception:
        return []
    if not acute_sigs:
        return []

    try:
        chronic_sigs = _firing_chronic_signals(ctx.shared_dir, ctx.bot_id)
    except Exception:
        return []

    openclaw_config = _read_openclaw_config(ctx.bot_id, ctx.openclaw_reader)

    proposals: list[Proposal] = []
    emitted_cause_keys: set[str] = set()

    for acute in acute_sigs:
        try:
            match = dispatch(acute, chronic_sigs, openclaw_config)
        except Exception:
            match = None
        if match is None:
            continue
        if match.cause_key in emitted_cause_keys:
            # Same root cause already synthesized this run (likely a
            # different acute signal type firing on the same bot).
            continue

        # Dedupe window: open / recently-applied Proposal already
        # exists.
        if ctx.consult_proposal_history and _recent_proposal_exists(
            ctx.shared_dir, ctx.bot_id, match.cause_key, now=now,
        ):
            continue

        # Repeat rejection: operator has dismissed this cause 2+ times.
        if (
            ctx.consult_proposal_history
            and operator_already_declined(
                ctx.shared_dir,
                bot_id=ctx.bot_id,
                cause_key=match.cause_key,
                min_recent_declines=2,
            )
        ):
            continue

        # Respect config_intent for the correlation's target field.
        try:
            if _operator_pinned_short(ctx.shared_dir, ctx.bot_id, match):
                continue
        except Exception:
            # Fail-open: if the intent sidecar is malformed we'd rather
            # emit and let the operator dismiss than silently swallow.
            pass

        # Pull history for the body's "you've seen this before" line.
        history_summary = None
        if ctx.consult_proposal_history:
            try:
                entries = proposal_history(
                    ctx.shared_dir,
                    bot_id=ctx.bot_id,
                    cause_key=match.cause_key,
                    limit=10,
                )
                history_summary = summarize_history(
                    entries, bot_id=ctx.bot_id, cause_key=match.cause_key,
                )
            except Exception:
                history_summary = None

        try:
            proposal = _build_proposal(ctx.bot_id, match, history_summary)
        except Exception:
            continue
        proposals.append(proposal)
        emitted_cause_keys.add(match.cause_key)

    return proposals
