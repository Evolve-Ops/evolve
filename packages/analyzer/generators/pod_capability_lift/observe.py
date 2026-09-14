"""generators.pod_capability_lift.observe — Cross-bot Signal aggregator.

Walks every firing ``app_suggester_gap`` and
``engagement_amplification_opportunity`` Signal in the store. Groups
by the cross-bot key (catalog category for gaps; (noun, verb) for
amplifications). When a group has ``>= MIN_BOTS_FOR_LIFT`` distinct
bots, emits one pod-wide Investigation Proposal naming every bot.

Coexists with per-bot proposals from the underlying generators —
different scope, different operator-readable framing:

  - app_suggester                 → "should team-bot-X have this?"
  - engagement_amplifier          → "should team-bot-X deepen this?"
  - pod_capability_lift (this)    → "should the whole pod have this?"

Dedup: dismiss_signature is per-key (per-category for gaps; per
(noun, verb) for amplifications). Dismissing the pod-wide
fitness_tracking proposal won't suppress pod-wide finance proposals
or per-bot fitness proposals.

Determinism: bots iterated in name-sort order; categories /
(noun, verb) keys iterated in sort order so the same Signal-store
state produces the same Proposals.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "pod_capability_lift"
DIMENSION = "capabilities"

# Signal types this generator aggregates.
_GAP_SIGNAL_TYPE = "app_suggester_gap"
_AMP_SIGNAL_TYPE = "engagement_amplification_opportunity"
_GROUNDING_SIGNAL_TYPES = (_GAP_SIGNAL_TYPE, _AMP_SIGNAL_TYPE)

# Pod-wide threshold. 3 is the smallest number that defensibly
# distinguishes a real pod-wide pattern from two coincidental bots.
# On a 9-bot pod this fires when a third of the pod converges; on a
# larger deployment the threshold could climb to N/4 or similar
# (tracked as Phase 2 follow-up — leaving the constant configurable
# via ctx.min_bots so the dev pod can tune without code change).
MIN_BOTS_FOR_LIFT = 3

# Catalog path — same JSON the app_suggester generator reads, so the
# lifted proposal can quote the same {title, description, example_apps}
# the per-bot proposals already use.
_CATALOG_PATH = (
    Path(__file__).parent.parent / "app_suggester" / "catalog.json"
)


@dataclass
class PodCapabilityLiftContext:
    bot_ids: list[str]
    shared_dir: Path
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Operator-tunable threshold. Defaults to MIN_BOTS_FOR_LIFT but the
    # runner can override (e.g. lower for tests / larger pods).
    min_bots: int = MIN_BOTS_FOR_LIFT
    # Cap how many pod-wide proposals fire per cycle. Same posture as
    # the per-bot generators: small queue beats a flood.
    max_per_run: int = 5


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_catalog(shared_dir: Path | None = None) -> list[dict]:
    """Read static catalog + dynamic catalog seeds (Layer 2). The
    observe() entry point passes ``shared_dir`` so dynamic seeds
    added via ``vocab_add`` reach the cross-bot synthesizer
    end-to-end. Tolerant of missing / malformed files — gap
    proposals fall back to the bare category slug if the catalog
    entry is unavailable."""
    try:
        # Lazy import keeps this module loadable in test fixtures
        # where _merged_vocabulary isn't importable.
        import _merged_vocabulary as mv

        return mv.effective_catalog(_CATALOG_PATH, shared_dir)
    except ImportError:
        # Pure static fallback.
        import json
        try:
            data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict)]


def _catalog_entry(catalog: list[dict], category: str) -> dict | None:
    for e in catalog:
        if e.get("category") == category:
            return e
    return None


def _iter_firing_signals(shared_dir: Path):
    """Yield every firing Signal of one of the types we aggregate.

    Lazy import keeps this module loadable in analyzer-only envs
    where signals.store isn't importable."""
    try:
        from signals import store as signals_store
    except ImportError:
        return
    for sig in signals_store.iter_active(shared_dir, state="firing"):
        if getattr(sig, "type", None) in _GROUNDING_SIGNAL_TYPES:
            yield sig


def _details(sig) -> dict:
    d = getattr(sig, "details", None)
    return d if isinstance(d, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# Gap aggregation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _GapGroup:
    """Aggregated evidence for one (category) across multiple bots."""

    category: str
    bot_ids: list[str] = field(default_factory=list)
    grounding_ids: list[str] = field(default_factory=list)
    domain_tag: str = ""
    # Squash per-bot evidence so the pod-wide pitch can quote
    # representative numbers (e.g. "8 sessions on team-bot-X,
    # 12 sessions on team-bot-Y").
    per_bot_evidence: dict[str, dict] = field(default_factory=dict)


def _group_gaps(
    signals: list, bot_ids_allowed: set[str] | None = None
) -> dict[str, _GapGroup]:
    """Group app_suggester_gap Signals by ``details.category``.

    ``bot_ids_allowed`` restricts which bots count toward the lift —
    used by the runner to scope to the configured pod and skip stray
    Signals for retired bots that haven't been swept yet."""
    grouped: dict[str, _GapGroup] = {}
    for sig in signals:
        if getattr(sig, "type", None) != _GAP_SIGNAL_TYPE:
            continue
        details = _details(sig)
        category = details.get("category") or ""
        bot_id = details.get("bot_id") or getattr(sig, "bot_id", "")
        if not category or not bot_id:
            continue
        if bot_ids_allowed is not None and bot_id not in bot_ids_allowed:
            continue
        group = grouped.setdefault(
            category, _GapGroup(category=category)
        )
        if bot_id not in group.bot_ids:
            group.bot_ids.append(bot_id)
        group.grounding_ids.append(getattr(sig, "id", ""))
        if not group.domain_tag:
            tag = details.get("domain_tag")
            if isinstance(tag, str):
                group.domain_tag = tag
        # Stash representative per-bot evidence for the pitch.
        group.per_bot_evidence[bot_id] = {
            "example_nouns": details.get("example_nouns") or [],
            "distinct_sessions": details.get("distinct_sessions"),
            "distinct_days": details.get("distinct_days"),
            "engagement_total": details.get("engagement_total"),
        }
    return grouped


def _build_gap_proposal(
    group: _GapGroup, catalog: list[dict]
) -> Proposal:
    """One pod-wide Investigation proposal for a multi-bot category gap."""
    entry = _catalog_entry(catalog, group.category) or {}
    title_text = entry.get("title") or group.category.replace("_", " ").title()
    description = entry.get("description") or ""
    example_apps = entry.get("example_apps") or []

    bot_ids_sorted = sorted(group.bot_ids)
    n_bots = len(bot_ids_sorted)
    bots_phrase = ", ".join(bot_ids_sorted)
    domain_phrase = (
        f" ({group.domain_tag.split(':', 1)[-1]})"
        if group.domain_tag else ""
    )

    headline = (
        f"Pod-wide capability gap: {title_text} ({n_bots} bots)"
    )[:120]

    summary = (
        f"{n_bots} bots ({bots_phrase}) are all showing the "
        f"*{title_text}*{domain_phrase} capability gap. Consider a "
        f"pod-wide install or a shared app."
    )

    # Build the explanation. We cite ONE bot's evidence in detail and
    # summarize the rest, to avoid a wall of numbers per bot. The bot
    # cited is the first in sort order (deterministic) — operators can
    # check the per-bot proposals for full evidence per bot.
    exp_lines: list[str] = []
    exp_lines.append(
        f"**Which bots.** {bots_phrase} — {n_bots} of the pod's "
        f"bots are independently showing the same capability gap."
    )
    sample_bot = bot_ids_sorted[0]
    sample_ev = group.per_bot_evidence.get(sample_bot, {})
    nouns = sample_ev.get("example_nouns") or []
    sessions = sample_ev.get("distinct_sessions")
    days = sample_ev.get("distinct_days")
    if nouns and sessions and days:
        noun_phrase = ", ".join(f"*{n}*" for n in nouns[:3])
        exp_lines.append("")
        exp_lines.append(
            f"**Example evidence ({sample_bot}).** Conversations "
            f"surfaced {noun_phrase} across {sessions} session"
            f"{'s' if sessions != 1 else ''} on {days} day"
            f"{'s' if days != 1 else ''}. Similar shape on the other "
            f"{n_bots - 1} bot{'s' if n_bots > 2 else ''}."
        )
    exp_lines.append("")
    exp_lines.append(f"**{title_text}** — {description}")
    if example_apps:
        exp_lines.append("")
        exp_lines.append(
            "Example app names from the catalog: "
            + ", ".join(f"`{n}`" for n in example_apps[:5])
        )

    exp_lines.append("")
    exp_lines.append("**Ways to roll this out:**")
    exp_lines.append(
        "- **Install pod-wide**: draft a manifest for one of the example "
        "apps and install it on all listed bots. The Applications tab "
        "supports multi-bot install once a manifest exists."
    )
    exp_lines.append(
        "- **Build as a shared app**: write a single manifest scoped to "
        "the catalog category and deploy it on every bot. Updates "
        "propagate centrally rather than drifting per-bot."
    )
    exp_lines.append(
        "- **Per-bot rollout**: dismiss this pod-wide proposal and use "
        "the individual per-bot Recommendations cards from "
        "app_suggester to roll out selectively."
    )
    exp_lines.append(
        "- **Dismiss** if pod-wide isn't the right move — the per-bot "
        "proposals continue independently. The same pod-wide category "
        "won't be re-suggested for a while."
    )
    exp_lines.append("")
    exp_lines.append(
        "_Note: the per-bot Recommendations cards for each of the listed "
        "bots continue independently of this pod-wide proposal. "
        "Dismissing this one doesn't suppress the per-bot ones, and "
        "vice versa._"
    )
    explanation = "\n".join(exp_lines)

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_ids_sorted[0],  # routing target — UI groups by primary bot
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"pod_capability_lift:gap:{group.category}",
            f"bots:{n_bots}",
        ],
        provenance=Provenance(
            technique="pod_capability_lift.gap_aggregation",
            signals={
                "category": group.category,
                "bot_ids": bot_ids_sorted,
                "n_bots": n_bots,
                "domain_tag": group.domain_tag,
                "grounding_signal_ids": list(group.grounding_ids),
            },
            confidence=0.85,
        ),
        problem=(
            f"pod: {n_bots} bots gapped on {group.category} — "
            f"consider pod-wide install"
        ),
        action=Investigation(context=explanation),
        risk_tag=RiskTag(
            blast_radius="pod",
            reversibility="manual",
            touches=[],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline,
        motivating_signals=list(group.grounding_ids),
        summary=summary,
        explanation=explanation,
        action_label="Pick a pod-wide rollout path",
        manual_path="Applications → multi-bot install",
        dismiss_signature=f"pod_capability_lift:gap:{group.category}",
        dismiss_scope="kind",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Amplification aggregation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _AmpGroup:
    """Aggregated evidence for one (noun, verb) across multiple bots."""

    noun: str
    verb: str
    bot_ids: list[str] = field(default_factory=list)
    grounding_ids: list[str] = field(default_factory=list)
    alignment_mix: dict[str, int] = field(default_factory=dict)
    per_bot_engagement: dict[str, int] = field(default_factory=dict)


def _group_amps(
    signals: list, bot_ids_allowed: set[str] | None = None
) -> dict[tuple[str, str], _AmpGroup]:
    grouped: dict[tuple[str, str], _AmpGroup] = {}
    for sig in signals:
        if getattr(sig, "type", None) != _AMP_SIGNAL_TYPE:
            continue
        details = _details(sig)
        noun = details.get("noun") or ""
        verb = details.get("verb") or ""
        bot_id = details.get("bot_id") or getattr(sig, "bot_id", "")
        if not noun or not verb or not bot_id:
            continue
        if bot_ids_allowed is not None and bot_id not in bot_ids_allowed:
            continue
        key = (noun, verb)
        group = grouped.setdefault(key, _AmpGroup(noun=noun, verb=verb))
        if bot_id not in group.bot_ids:
            group.bot_ids.append(bot_id)
        group.grounding_ids.append(getattr(sig, "id", ""))
        alignment = details.get("objective_alignment") or "emergent"
        group.alignment_mix[alignment] = (
            group.alignment_mix.get(alignment, 0) + 1
        )
        eng = details.get("engagement_total")
        if isinstance(eng, (int, float)):
            group.per_bot_engagement[bot_id] = int(eng)
    return grouped


_FRIENDLY_VERB: dict[str, str] = {
    # Closed vocab matches schema.observation.VERB_VOCABULARY. Duplicated
    # from engagement_amplifier.observe to avoid a load-order coupling
    # between the two generators (either could be imported first; the
    # vocab is stable). Refactor target: a shared utils module once a
    # third consumer needs the same mapping.
    "drafting": "drafts",
    "planning": "plans",
    "troubleshooting": "troubleshoots",
    "tracking": "tracks",
    "researching": "researches",
    "summarizing": "summarizes",
    "reviewing": "reviews",
    "deciding": "decides",
    "learning": "learns",
    "explaining": "explains",
    "comparing": "compares",
    "transforming": "transforms",
    "scheduling": "schedules",
    "recording": "records",
    "reminding": "reminds",
    "searching": "searches",
    "coordinating": "coordinates",
    "celebrating": "celebrates",
    "venting": "vents",
    "exploring": "explores",
}


def _verb_friendly(verb: str) -> str:
    """Third-person-singular conjugation of the cluster verb for
    operator-facing prose."""
    if not verb:
        return ""
    return _FRIENDLY_VERB.get(verb.lower(), verb)


def _build_amp_proposal(group: _AmpGroup) -> Proposal:
    bot_ids_sorted = sorted(group.bot_ids)
    n_bots = len(bot_ids_sorted)
    bots_phrase = ", ".join(bot_ids_sorted)

    verb_v = _verb_friendly(group.verb)

    # Alignment framing. Three shapes:
    #   confirmed-majority → "stated-scope working pattern; double down"
    #   emergent-majority  → "organic cross-bot convergence; consider
    #                         formalizing"
    #   contradicted-majority → "pod-wide scope contradiction; make a
    #                            pod-wide decision"
    # Ties resolve in the order confirmed > contradicted > emergent so
    # the most-actionable framing wins when the mix is split.
    confirmed_n = group.alignment_mix.get("confirmed", 0)
    emergent_n = group.alignment_mix.get("emergent", 0)
    contradicted_n = group.alignment_mix.get("contradicted", 0)
    is_contradicted_majority = (
        contradicted_n > confirmed_n and contradicted_n > emergent_n
    )
    is_confirmed_majority = (
        confirmed_n >= contradicted_n and confirmed_n >= emergent_n
    )

    if is_contradicted_majority:
        mix_phrase = "pod-wide scope contradiction"
        why_paragraph = (
            "**Why this matters.** Multiple bots' AGENTS.md files "
            "explicitly mark this domain as out of scope, yet users "
            "across the pod keep engaging with it. That's a pod-level "
            "signal: either the household / team's actual use has "
            "drifted away from how the bots are scoped, or the "
            "exclusions were never enforced. Either way the pod-wide "
            "fragmentation (sometimes the bot answers, sometimes it "
            "deflects) is operator-readable. Make a pod-wide decision: "
            "widen the exclusion across all the listed bots, add a "
            "consistent redirect, or revise the per-bot scopes to "
            "match what users actually do."
        )
    elif is_confirmed_majority:
        mix_phrase = "stated-scope working pattern"
        why_paragraph = (
            "**Why this matters.** Multiple bots are independently "
            "delivering on a domain they each list in their AGENTS.md. "
            "Investing pod-wide — a shared app, scheduled surface, "
            "or workflow template — raises the floor across all of "
            "them at once."
        )
    else:
        mix_phrase = "organic cross-bot convergence"
        why_paragraph = (
            "**Why this matters.** Users on multiple bots have "
            "organically converged on the same conversational "
            "pattern, even though most of those bots don't list it "
            "in their AGENTS.md. A pod-wide capability or shared app "
            "would either formalize the use case (users keep showing "
            "up for it) or make explicit that this is what your "
            "household / team is actually doing."
        )

    headline = (
        f"Pod-wide pattern: {verb_v} {group.noun} ({n_bots} bots)"
    )[:120]

    summary = (
        f"{n_bots} bots ({bots_phrase}) are all engaging deeply on "
        f"*{verb_v} {group.noun}* — a {mix_phrase}. "
        + (
            "Make a pod-wide scope decision."
            if is_contradicted_majority
            else "Consider a shared app or pod-wide workflow."
        )
    )

    exp_lines: list[str] = []
    exp_lines.append(
        f"**Which bots.** {bots_phrase} — {n_bots} bots show "
        f"sustained engagement on ({group.noun}, {group.verb})."
    )
    # Engagement totals per bot for the audit trail.
    if group.per_bot_engagement:
        eng_lines = sorted(
            group.per_bot_engagement.items(),
            key=lambda kv: -kv[1],
        )
        eng_phrase = ", ".join(
            f"{b}: {e}" for b, e in eng_lines
        )
        exp_lines.append("")
        exp_lines.append(
            f"**Per-bot engagement totals.** {eng_phrase}."
        )
    # Alignment mix. Suppress the contradicted-count chunk when zero
    # so the line stays readable for the common case (all-confirmed or
    # all-emergent). Only surface contradicted when at least one bot
    # flagged it — that's when the count carries information.
    exp_lines.append("")
    mix_parts = [
        f"{confirmed_n} confirmed (in stated scope per AGENTS.md)",
        f"{emergent_n} emergent (users converged organically)",
    ]
    if contradicted_n > 0:
        mix_parts.append(
            f"{contradicted_n} contradicted (users engaging on a "
            f"domain the bot's AGENTS.md marks as out of scope)"
        )
    exp_lines.append(
        f"**Objective alignment mix.** " + "; ".join(mix_parts) + "."
    )
    exp_lines.append("")
    exp_lines.append(why_paragraph)
    exp_lines.append("")

    # Rollout / decision options. The contradicted-majority case is
    # qualitatively different from the build-or-deepen cases — the
    # options are scope-decisions, not deployment shapes.
    if is_contradicted_majority:
        exp_lines.append("**Ways to make this pod-wide decision:**")
        exp_lines.append(
            f"- **Widen scope across the pod**: remove `{group.noun}` "
            f"from the `## Out of scope` section on every listed bot's "
            f"AGENTS.md. Users get a consistent experience; the "
            f"household / team's actual use becomes legitimate."
        )
        exp_lines.append(
            f"- **Pod-wide redirect**: add a shared AGENTS.md note "
            f"across the listed bots — \"if a user asks about "
            f"`{group.noun}`, say I don't handle that — point them to "
            f"[the right place].\" Makes the boundary visible "
            f"everywhere instead of each bot silently muddling through."
        )
        exp_lines.append(
            f"- **Per-bot only**: dismiss this pod-wide and use the "
            f"individual engagement_amplifier (contradicted) cards "
            f"per bot to make the decision one bot at a time."
        )
        exp_lines.append(
            "- **Dismiss** if pod-wide scope decisions aren't "
            "appropriate — the per-bot cards continue independently."
        )
    else:
        exp_lines.append("**Ways to roll this out:**")
        exp_lines.append(
            f"- **Build a shared app**: draft a manifest scoped to "
            f"`{group.noun}` and install it on every bot showing the "
            f"pattern. Centralizes updates and surfaces the workflow "
            f"under one name across the pod."
        )
        exp_lines.append(
            f"- **Pod-wide scheduled surface**: a cron / scheduled "
            f"action that initiates a `{verb_v} {group.noun}` "
            f"conversation on each bot, matched to how often users "
            f"land on it organically."
        )
        if emergent_n > 0:
            exp_lines.append(
                f"- **Update AGENTS.md across the pod** to acknowledge "
                f"`{group.noun}` is something the pod actually does. "
                f"Doesn't change capability, but the model framing "
                f"shifts."
            )
        exp_lines.append(
            "- **Per-bot only**: dismiss this pod-wide and use the "
            "individual engagement_amplifier cards per bot."
        )
        exp_lines.append(
            "- **Dismiss** if pod-wide rollout isn't the right move. "
            "Per-bot cards continue independently."
        )
    exp_lines.append("")
    exp_lines.append(
        "_Note: the per-bot engagement_amplifier cards continue "
        "independently of this pod-wide proposal. Dismissing this "
        "one doesn't suppress the per-bot ones, and vice versa._"
    )
    explanation = "\n".join(exp_lines)

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_ids_sorted[0],  # primary routing bot
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"pod_capability_lift:amp:{group.noun}:{group.verb}",
            f"bots:{n_bots}",
        ],
        provenance=Provenance(
            technique="pod_capability_lift.amp_aggregation",
            signals={
                "noun": group.noun,
                "verb": group.verb,
                "bot_ids": bot_ids_sorted,
                "n_bots": n_bots,
                "alignment_mix": dict(group.alignment_mix),
                "per_bot_engagement": dict(group.per_bot_engagement),
                "grounding_signal_ids": list(group.grounding_ids),
            },
            confidence=0.8,
        ),
        problem=(
            f"pod: {n_bots} bots "
            + (
                f"contradicting scope on ({group.noun}, {group.verb}) "
                f"— pod-wide scope decision"
                if is_contradicted_majority
                else f"amplifying ({group.noun}, {group.verb}) — "
                f"consider shared workflow"
            )
        ),
        action=Investigation(context=explanation),
        risk_tag=RiskTag(
            blast_radius="pod",
            reversibility="manual",
            touches=[],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline,
        motivating_signals=list(group.grounding_ids),
        summary=summary,
        explanation=explanation,
        action_label=(
            "Make a pod-wide scope decision"
            if is_contradicted_majority
            else "Pick a pod-wide rollout path"
        ),
        manual_path="Applications → multi-bot install + AGENTS.md",
        dismiss_signature=(
            f"pod_capability_lift:amp:{group.noun}:{group.verb}"
        ),
        dismiss_scope="kind",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def observe(ctx: PodCapabilityLiftContext) -> list[Proposal]:
    """Walk firing app_suggester_gap + engagement_amplification
    Signals across the pod; emit pod-wide Proposals for cross-bot
    convergences.

    Determinism: bots iterated in sort order, categories /
    (noun, verb) keys iterated in sort order. Same Signal-store
    state → same Proposals across runs."""
    signals = list(_iter_firing_signals(ctx.shared_dir))
    if not signals:
        return []

    bot_filter = set(ctx.bot_ids) if ctx.bot_ids else None
    # Pass shared_dir so dynamic catalog seeds reach pod-wide
    # gap aggregation. Without this, vocab_add catalog-seed entries
    # would never surface in pod-wide proposals.
    catalog = _load_catalog(ctx.shared_dir)
    proposals: list[Proposal] = []
    limit = max(1, ctx.max_per_run)

    # Gap groups first — capability-add proposals are higher-value
    # than amplification synthesis (an operator who wants to invest
    # pod-wide engineering effort typically picks gaps before
    # amplifications).
    gap_groups = _group_gaps(signals, bot_ids_allowed=bot_filter)
    for category in sorted(gap_groups.keys()):
        if len(proposals) >= limit:
            break
        group = gap_groups[category]
        if len(group.bot_ids) < ctx.min_bots:
            continue
        proposals.append(_build_gap_proposal(group, catalog))

    # Amplification groups.
    amp_groups = _group_amps(signals, bot_ids_allowed=bot_filter)
    for key in sorted(amp_groups.keys()):
        if len(proposals) >= limit:
            break
        group = amp_groups[key]
        if len(group.bot_ids) < ctx.min_bots:
            continue
        proposals.append(_build_amp_proposal(group))

    return proposals
