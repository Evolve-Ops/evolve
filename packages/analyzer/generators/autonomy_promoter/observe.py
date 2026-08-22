"""generators.autonomy_promoter.observe — streak Signal → promotion Proposal.

Spec: docs/spec-autonomy-ladder-2026-06-10.md §3.2 step 2.

Walks every firing ``autonomy_promotion_candidate`` Signal (the
permission monitor's streak producer wrote them — pure Python, ledger-
sourced) and emits one ``UpdateAutonomyPosture`` Proposal per
(bot, integration): "Asks first" → "Acts within limits", with a
conservative rules block derived from the observed volume
(``actions_per_day`` = the streak Signal's suggestion — double the
busiest observed day, floor 5).

The proposal NEVER applies itself: ``approval_audience`` is
``pod_operator``, the action's promotion direction excludes it from
every auto-approve lane (see ``autonomy.catalog.action_is_promotion``
call sites), and applying it through the normal queue IS the
deliberate operator act — the applier writes ``set_by: proposal:<id>``.

Re-validation at emit time: a Signal can outlive its premise (the
operator may have promoted/demoted via the UI between the streak
firing and this generator running — or the signal-subscriber may
dispatch us seconds after a posture write). Every candidate is checked
against the live posture file before a proposal is built; the
``expected_current_rung`` CAS witness then re-checks at apply time.

Determinism: signals iterated in (bot_id, integration_id) sort order;
same store state → same proposals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema.proposal import (
    Proposal,
    Provenance,
    RiskTag,
    UpdateAutonomyPosture,
    new_proposal_id,
)


GENERATOR_ID = "autonomy_promoter"
DIMENSION = "safety"

_CANDIDATE_SIGNAL_TYPE = "autonomy_promotion_candidate"

# Floor for the proposed daily cap when the streak Signal carries no
# usable suggestion (defensive — the producer always writes one).
_DEFAULT_ACTIONS_PER_DAY = 5


@dataclass
class AutonomyPromoterContext:
    bot_ids: list[str]
    shared_dir: Path
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Small queue beats a flood — same posture as the other generators.
    max_per_run: int = 3


def _iter_candidate_signals(shared_dir: Path):
    try:
        from signals import store as signals_store
    except ImportError:
        return
    for sig in signals_store.iter_active(shared_dir, state="firing"):
        if getattr(sig, "type", None) == _CANDIDATE_SIGNAL_TYPE:
            yield sig


def _live_posture(shared_dir: Path, bot_id: str, integration_id: str):
    from autonomy import store as _astore
    try:
        doc = _astore.load(shared_dir, bot_id)
    except ValueError:
        return None
    if doc is None:
        return None
    return doc.integrations.get(integration_id)


def _build_proposal(
    sig: Any, posture: Any, details: dict[str, Any],
) -> Proposal | None:
    from autonomy import catalog as _acatalog

    bot_id = str(details.get("bot_id") or getattr(sig, "bot_id", "") or "")
    iid = str(details.get("integration_id") or "")
    spec = _acatalog.kind_spec(posture.kind)
    binding = _acatalog.binding_for(iid)
    if spec is None or binding is None:
        return None

    noun = spec.operator_noun
    display = binding.display_name
    label = f"{noun} ({display})"
    actions = int(details.get("actions") or 0)
    span_days = int(details.get("span_days") or 0)
    suggested = details.get("suggested_actions_per_day")
    if not isinstance(suggested, int) or isinstance(suggested, bool) or suggested <= 0:
        suggested = _DEFAULT_ACTIONS_PER_DAY
    rules = {"actions_per_day": suggested}
    consequence = spec.promotion_consequences.get(_acatalog.RUNG_AUTONOMOUS, "")
    to_label = _acatalog.RUNG_LABELS[_acatalog.RUNG_AUTONOMOUS]
    from_label = _acatalog.RUNG_LABELS[_acatalog.RUNG_ACT_WITH_APPROVAL]

    headline = (
        f"Let {bot_id} use {noun} on its own, within limits"
    )[:120]
    summary = (
        f"{bot_id} has handled {noun} on {display} at \"{from_label}\" "
        f"cleanly — {actions} actions over {span_days} days, each with a "
        f"go-ahead, no incidents. Move it to \"{to_label}\" with a "
        f"limit of {suggested} actions per day?"
    )
    explanation = "\n".join([
        f"**What changes.** {consequence}",
        "",
        f"**The track record.** {actions} {noun} actions on {display} "
        f"over the last {span_days} days at \"{from_label}\" — each one "
        "after a go-ahead in conversation (that is how this level "
        "works), with no open incidents for this integration. Evolve "
        "counts the actions from the bot's own structured activity "
        "record; it never reads conversations, so the per-message "
        "go-aheads themselves are the level's operating rule rather "
        "than something Evolve verified individually.",
        "",
        f"**The proposed limits.** Up to {suggested} {noun} actions per "
        f"day (double the busiest day observed, minimum "
        f"{_DEFAULT_ACTIONS_PER_DAY}). Hitting the limit pauses outward "
        f"actions for the rest of the day and alerts you. You can "
        "tighten or widen the limits — or add allowed recipients — any "
        "time on Security → Permissions → Autonomy.",
        "",
        "**The way back.** One click on Security → Permissions → "
        "Autonomy steps it back down, no confirmation needed. Evolve "
        "also steps it down automatically (and alerts you) if it keeps "
        "pushing past its daily limit or a critical security finding "
        "names this integration.",
        "",
        "_Applying this is the deliberate decision — nothing widens "
        "until you approve it here._",
    ])

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Identity-only: trigger_observations feed the dedup fingerprint, so
        # the growing streak count must NOT appear here — a value-bearing
        # trigger mints a fresh proposal on every run instead of refreshing
        # the open one. The current count lives in provenance.signals, which
        # ingest._refresh_existing overwrites on each re-detection.
        trigger_observations=[
            f"autonomy_promoter:streak:{bot_id}:{iid}",
        ],
        provenance=Provenance(
            technique="autonomy_promoter.streak_promotion",
            signals={
                "bot_id": bot_id,
                "integration_id": iid,
                "actions": actions,
                "span_days": span_days,
                "first_day": details.get("first_day"),
                "last_day": details.get("last_day"),
                "max_actions_per_day": details.get("max_actions_per_day"),
                "grounding_signal_ids": [getattr(sig, "id", "")],
            },
            confidence=0.8,
        ),
        problem=(
            f"{bot_id}: clean approval streak on {label} — candidate for "
            "acts-within-limits"
        ),
        action=UpdateAutonomyPosture(
            bot_id=bot_id,
            integration_id=iid,
            rung=_acatalog.RUNG_AUTONOMOUS,
            rules=rules,
            # CAS witness + direction proof: must still be "Asks first"
            # at apply time or the applier fails loudly.
            expected_current_rung=_acatalog.RUNG_ACT_WITH_APPROVAL,
            note=f"promotion proposed from a {actions}-action clean streak",
        ),
        # touches=["tools"]: the render writes the bot's tools.deny
        # slice. Deliberately on the irreversibility list — one more
        # fence keeping this out of autonomous lanes.
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=["tools"],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline,
        motivating_signals=[getattr(sig, "id", "")],
        summary=summary,
        explanation=explanation,
        action_label=f"Allow more: {label}",
        manual_path="Security → Permissions → Autonomy",
        dismiss_signature=f"autonomy_promoter:{bot_id}:{iid}",
        dismiss_scope="kind",
    )


def observe(ctx: AutonomyPromoterContext) -> list[Proposal]:
    """Emit promotion proposals for firing streak Signals that still
    hold against the live posture file."""
    from autonomy import catalog as _acatalog
    from autonomy import store as _astore

    bot_filter = set(ctx.bot_ids) if ctx.bot_ids else None
    candidates: list[tuple[str, str, Any]] = []
    for sig in _iter_candidate_signals(ctx.shared_dir):
        details = sig.details if isinstance(sig.details, dict) else {}
        bot_id = str(details.get("bot_id") or getattr(sig, "bot_id", "") or "")
        iid = str(details.get("integration_id") or "")
        if not bot_id or not iid:
            continue
        if bot_filter is not None and bot_id not in bot_filter:
            continue
        candidates.append((bot_id, iid, sig))

    proposals: list[Proposal] = []
    limit = max(1, ctx.max_per_run)
    for bot_id, iid, sig in sorted(candidates, key=lambda c: (c[0], c[1])):
        if len(proposals) >= limit:
            break
        posture = _live_posture(ctx.shared_dir, bot_id, iid)
        if posture is None:
            continue
        # Re-validate the premise against the live file: still at
        # "Asks first", still a deliberate posture.
        if posture.rung != _acatalog.RUNG_ACT_WITH_APPROVAL:
            continue
        actor = (posture.set_by or {}).get("actor") or ""
        if actor == _astore.ACTOR_BACKFILL or actor.startswith(
            _astore.ACTOR_PREFIX_AUTO_DEMOTION
        ):
            continue
        details = sig.details if isinstance(sig.details, dict) else {}
        proposal = _build_proposal(sig, posture, details)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


__all__ = [
    "AutonomyPromoterContext",
    "DIMENSION",
    "GENERATOR_ID",
    "observe",
]
