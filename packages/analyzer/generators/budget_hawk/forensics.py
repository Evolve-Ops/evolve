"""generators.budget_hawk.forensics — Cost-lineage-backed detectors.

Week-1 MVP:
  * ``detect_summarizer_on_trivial`` — session summarizer LLM calls on
    sessions with fewer than N turns. Proposes bumping ``summarizerMinTurns``.
  * ``detect_classifier_noise`` — high-volume LLM tier-classifier calls when
    keyword confidence would have been sufficient. Proposes bumping
    ``classifierKeywordConfidenceFloor``.

Future weeks (stub here, land later):
  * cache_invalidation_event
  * heartbeat_over_cadence
  * fallback_chain_leak
  * session_sprawl
  * background_dominance

The detectors are pure functions over an iterable of cost_event records +
the equivalent stream of session_summary / turn_annotation records when they
need turn-count context. They produce Proposal objects (via the factories in
this module's ``proposals.py``) and are tolerant of empty / partial data —
returning ``[]`` when a bot hasn't accumulated enough signal.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from generators.budget_hawk.proposals import (
    make_app_cost_imbalance,
    make_classifier_threshold_patch,
    make_summarizer_min_turns_patch,
)
from schema.proposal import Proposal


# ─────────────────────────────────────────────────────────────────────────────
# Context / dependency injection
# ─────────────────────────────────────────────────────────────────────────────


# ledger_reader: (bot_id, days_back) -> Iterable of cost_event dicts
LedgerReader = Callable[[str, int], Iterable[dict]]

# session_reader: (bot_id, days_back) -> Iterable of session_summary dicts
SessionReader = Callable[[str, int], Iterable[dict]]


@dataclass
class ForensicsContext:
    """Bundles the dependencies every detector needs.

    Kept small on purpose — the detectors decide what to ask for. Tests
    inject fakes for ``ledger_reader`` and ``session_reader``; production
    wires them to ``cost_ledger.read_events`` and
    ``observations.access.window(...).session_summaries()``.
    """

    bot_id: str
    ledger_reader: LedgerReader
    session_reader: SessionReader
    lookback_days: int = 7
    # Tunables — pulled into the dataclass so tests can vary them independently
    # of whatever the calling generator's evolvable config happens to contain.
    summarizer_trivial_turn_threshold: int = 2
    classifier_daily_call_threshold: int = 20
    classifier_keyword_confident_share: float = 0.70
    # app_cost_imbalance: fires when one application tag dominates >= this
    # share of attributed spend AND absolute spend is above the floor.
    app_dominance_share: float = 0.50
    app_imbalance_min_usd: float = 1.00
    # When unattributed share of cost exceeds this, suggest extending
    # applicationPatterns — we can't see where the money is going.
    app_unattributed_share_threshold: float = 0.60
    now: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────────────
# Detector: summarizer_on_trivial
# ─────────────────────────────────────────────────────────────────────────────


def detect_summarizer_on_trivial(ctx: ForensicsContext) -> list[Proposal]:
    """Fire when the session summarizer is making LLM calls on trivial sessions.

    Signal:
        count of cost_events with trigger_kind=summarizer whose corresponding
        session_summary had turn_count < ``summarizer_trivial_turn_threshold``.

    Proposal:
        ConfigPatch to set the bot's ``plugins.entries.evolve.config.summarizerMinTurns``
        to ``summarizer_trivial_turn_threshold``.

    Returns ``[]`` when there's <2 offending events — one-off misfires aren't
    worth a proposal; we wait for a pattern.
    """
    # Build session_id → turn_count map from session summaries.
    session_turn_count: dict[str, int] = {}
    for summary in ctx.session_reader(ctx.bot_id, ctx.lookback_days):
        sid = summary.get("session_id")
        tc = summary.get("turn_count")
        if sid and isinstance(tc, int):
            session_turn_count[sid] = tc

    # Scan cost events for summarizer-triggered calls on trivial sessions.
    offending_events = 0
    total_offending_cost_usd = 0.0
    example_session_ids: list[str] = []
    for event in ctx.ledger_reader(ctx.bot_id, ctx.lookback_days):
        if event.get("trigger_kind") != "summarizer":
            continue
        sid = event.get("session_id")
        if not sid:
            continue
        turns = session_turn_count.get(sid)
        if turns is None:
            # No summary record found — can't prove this was trivial. Skip.
            continue
        if turns < ctx.summarizer_trivial_turn_threshold:
            offending_events += 1
            total_offending_cost_usd += float(event.get("cost_usd", 0.0) or 0.0)
            if len(example_session_ids) < 5:
                example_session_ids.append(sid)

    if offending_events < 2:
        return []

    return [
        make_summarizer_min_turns_patch(
            bot_id=ctx.bot_id,
            new_value=ctx.summarizer_trivial_turn_threshold,
            offending_events=offending_events,
            cost_waste_usd=round(total_offending_cost_usd, 6),
            lookback_days=ctx.lookback_days,
            example_session_ids=example_session_ids,
        )
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Detector: classifier_noise
# ─────────────────────────────────────────────────────────────────────────────


def detect_classifier_noise(ctx: ForensicsContext) -> list[Proposal]:
    """Fire when the tier classifier is making too many LLM calls.

    Signal:
        * ≥``classifier_daily_call_threshold`` classifier events per day on
          average over the lookback window,
        * AND the *session*'s ultimate classification has signals that
          indicate the keyword classifier was confident enough — we can't
          re-run the keyword classifier from cost events alone, so we use
          ``session_class`` + class_confidence from session summaries as a
          proxy. If ≥``classifier_keyword_confident_share`` of sessions
          classify at confidence ≥0.80, the LLM call was probably noise.

    Proposal:
        ConfigPatch to raise the bot's
        ``plugins.entries.evolve.config.classifierKeywordConfidenceFloor``
        to 0.80.

    Returns ``[]`` when the bot is quiet (few sessions) or the classifier
    is actually earning its keep.
    """
    # Count classifier events
    classifier_event_count = 0
    total_classifier_cost = 0.0
    distinct_days: set[str] = set()
    for event in ctx.ledger_reader(ctx.bot_id, ctx.lookback_days):
        if event.get("trigger_kind") != "classifier":
            continue
        classifier_event_count += 1
        total_classifier_cost += float(event.get("cost_usd", 0.0) or 0.0)
        ts = event.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            distinct_days.add(ts[:10])

    if classifier_event_count == 0:
        return []

    # Normalize to a per-day rate
    days_observed = max(len(distinct_days), 1)
    per_day = classifier_event_count / days_observed
    if per_day < ctx.classifier_daily_call_threshold:
        return []

    # Count sessions that classified with high confidence (our proxy for
    # "keyword classifier alone would have been sufficient").
    high_conf = 0
    total_sessions = 0
    for summary in ctx.session_reader(ctx.bot_id, ctx.lookback_days):
        total_sessions += 1
        conf = summary.get("tier_confidence") or summary.get("class_confidence") or 0.0
        try:
            if float(conf) >= 0.80:
                high_conf += 1
        except (TypeError, ValueError):
            continue

    if total_sessions == 0:
        return []
    confident_share = high_conf / total_sessions
    if confident_share < ctx.classifier_keyword_confident_share:
        return []

    return [
        make_classifier_threshold_patch(
            bot_id=ctx.bot_id,
            new_value=0.80,
            classifier_events=classifier_event_count,
            per_day=round(per_day, 1),
            confident_share=round(confident_share, 2),
            total_cost_usd=round(total_classifier_cost, 6),
            lookback_days=ctx.lookback_days,
        )
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Detector: app_cost_imbalance
# ─────────────────────────────────────────────────────────────────────────────


def detect_app_cost_imbalance(ctx: ForensicsContext) -> list[Proposal]:
    """Fire when spend is concentrated on a single application, or when most
    spend can't be attributed to any application at all.

    Two shapes of Investigation proposal:

    * **Dominance**: one app accounts for ≥ ``app_dominance_share`` of
      attributed cost AND total spend is ≥ ``app_imbalance_min_usd``. The
      proposal asks the operator whether that concentration matches intent
      (e.g. "team_bot_a burned $3.14 on health-nutrition — is that expected?").
      This isn't a ConfigPatch; attribution is a question of user priorities,
      not a knob to auto-tune.

    * **Coverage**: ``(unattributed)`` bucket exceeds
      ``app_unattributed_share_threshold`` of total cost. The proposal
      suggests extending ``applicationPatterns`` in ``network.json`` so the
      rollup gets more signal.

    Requires a ``cost_ledger.rollup_per_application``-equivalent — the context
    carries the readers, the detector does the join + thresholding in-process.
    Imports are local so ``forensics.py`` doesn't bring ``cost_ledger.py``
    into modules that only want the summarizer/classifier detectors.
    """
    # Lazy import to avoid adding cost_ledger as a hard dep of forensics.
    from cost_ledger import rollup_per_application

    events = list(ctx.ledger_reader(ctx.bot_id, ctx.lookback_days))
    summaries = list(ctx.session_reader(ctx.bot_id, ctx.lookback_days))
    if not events:
        return []

    rollup = rollup_per_application(events, summaries)
    if not rollup:
        return []

    total_cost = sum(v.get("cost_usd", 0.0) for v in rollup.values())
    if total_cost <= 0:
        return []

    proposals: list[Proposal] = []

    # Coverage proposal — gets emitted first because it's the prerequisite:
    # if we can't attribute most of spend, dominance claims are unreliable.
    unattr_cost = rollup.get("(unattributed)", {}).get("cost_usd", 0.0)
    unattr_share = unattr_cost / total_cost if total_cost > 0 else 0.0
    if (
        unattr_share >= ctx.app_unattributed_share_threshold
        and total_cost >= ctx.app_imbalance_min_usd
    ):
        proposals.append(
            make_app_cost_imbalance(
                bot_id=ctx.bot_id,
                kind="coverage",
                dominant_app=None,
                dominant_cost_usd=unattr_cost,
                dominant_share=unattr_share,
                total_cost_usd=total_cost,
                lookback_days=ctx.lookback_days,
            )
        )
        # Return early — dominance detection on a majority-unattributed
        # rollup would blame "(unattributed)" which is meaningless.
        return proposals

    # Dominance proposal — pick the largest app (excluding unattributed)
    # and check if it dominates.
    attributed = {
        app: v for app, v in rollup.items() if app != "(unattributed)"
    }
    if not attributed:
        return proposals
    top_app, top_slot = max(
        attributed.items(), key=lambda kv: kv[1].get("cost_usd", 0.0)
    )
    top_cost = top_slot.get("cost_usd", 0.0)
    attributed_total = sum(v.get("cost_usd", 0.0) for v in attributed.values())
    if attributed_total <= 0:
        return proposals
    top_share = top_cost / attributed_total
    if (
        top_share >= ctx.app_dominance_share
        and top_cost >= ctx.app_imbalance_min_usd
    ):
        proposals.append(
            make_app_cost_imbalance(
                bot_id=ctx.bot_id,
                kind="dominance",
                dominant_app=top_app,
                dominant_cost_usd=top_cost,
                dominant_share=top_share,
                total_cost_usd=attributed_total,
                lookback_days=ctx.lookback_days,
            )
        )

    return proposals


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — run all Week-1 detectors for a single bot
# ─────────────────────────────────────────────────────────────────────────────


def run_all_detectors(ctx: ForensicsContext) -> list[Proposal]:
    """Run every forensics detector and flatten the results."""
    out: list[Proposal] = []
    out.extend(detect_summarizer_on_trivial(ctx))
    out.extend(detect_classifier_noise(ctx))
    out.extend(detect_app_cost_imbalance(ctx))
    return out
