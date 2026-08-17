"""generators.engagement_amplifier.observe — Signal → Proposal builder.

Reads ``engagement_amplification_opportunity`` Signals from the store
and emits one Phase A operator-first Proposal per (bot, noun, verb)
opportunity. The proposal frames the pattern as either:

  - **confirmed** alignment — "your bot is delivering on its stated
    purpose; here's what's working most. Consider deepening this
    direction."
  - **emergent** alignment — "users have organically converged on
    this even though it isn't in the bot's stated scope. Consider
    whether to formally embrace it."

Both shapes are RSI-shaped under the 4-criteria test:
  1. Forward-looking — "consider [way to deepen]"
  2. Pattern-grounded — multi-session, multi-day cluster
  3. Objective-aware — confirmed vs. emergent categorization
  4. Material — operator decides cron / app / persona deepening

Action kind is ``Investigation`` (Tier 2 in the protocol — "no
automated action; operator decides which lever"). v1 doesn't auto-
apply anything because "deepen a working pattern" is intrinsically a
purpose-shaped decision that needs operator judgment.
"""
from __future__ import annotations

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


GENERATOR_ID = "engagement_amplifier"
DIMENSION = "capabilities"

# Signal types this generator grounds on. Matches the producer at
# engagement_amplifier_monitor.SIGNAL_TYPE.
_GROUNDING_SIGNAL_TYPES = ("engagement_amplification_opportunity",)


@dataclass
class EngagementAmplifierContext:
    bot_ids: list[str]
    shared_dir: Path
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Cap proposals per cycle so the Recommendations queue doesn't get
    # flooded if every bot has multiple amplifiable patterns. The
    # monitor's per-bot cap (MAX_SIGNALS_PER_BOT=3) already keeps
    # Signal volume bounded; this is a second safety net at proposal
    # creation. Default permissive — most pods will have far fewer.
    max_per_run: int = 10


def _dismiss_signature(bot_id: str, noun: str, verb: str) -> str:
    """Per-(bot, noun, verb) dismiss so dismissing 'tracking workouts'
    doesn't suppress 'planning workouts'."""
    return f"engagement_amplifier:{bot_id}:{noun}:{verb}"


def _grounding_signals(shared_dir: Path, bot_id: str) -> list:
    """Return firing engagement_amplification_opportunity Signals
    scoped to this bot. Empty list when no producer has emitted."""
    try:
        from signals import store as signals_store
    except ImportError:
        return []
    out: list = []
    for sig in signals_store.iter_active(
        shared_dir, bot_id=bot_id, state="firing"
    ):
        if getattr(sig, "type", None) in _GROUNDING_SIGNAL_TYPES:
            out.append(sig)
    return out


def _verb_friendly(verb: str) -> str:
    """Turn the ``-ing`` cluster verb into a readable third-person
    singular form for prose. ``tracking`` → ``tracks``; ``planning``
    → ``plans`` (doubled consonant); ``scheduling`` → ``schedules``
    (silent e). Closed-vocabulary lookup (see
    ``schema.observation.VERB_VOCABULARY``); falls back to the raw
    verb if it isn't in the table — that way a future vocabulary
    expansion can ship without blocking the proposal prose."""
    # Static map covers every entry in MOOD_VOCABULARY → its
    # third-person-singular form. Built explicitly because the simple
    # "strip -ing add -s" rule mis-conjugates doubled-consonant
    # ("planning"→"planns") and silent-e ("scheduling"→"schedules")
    # forms. Closed vocab keeps the table small.
    _FRIENDLY = {
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
    if not verb:
        return ""
    return _FRIENDLY.get(verb.lower(), verb)


def _build_proposal(sig, ctx: EngagementAmplifierContext) -> Proposal | None:
    """Build one Phase A operator-first Investigation proposal from a
    single ``engagement_amplification_opportunity`` Signal."""
    details = getattr(sig, "details", None) or {}
    if not isinstance(details, dict):
        return None
    bot_id = details.get("bot_id") or getattr(sig, "bot_id", "")
    noun = details.get("noun") or ""
    verb = details.get("verb") or ""
    if not bot_id or not noun or not verb:
        return None

    alignment = details.get("objective_alignment", "emergent")
    engagement = int(details.get("engagement_total") or 0)
    sessions = int(details.get("distinct_sessions") or 0)
    days = int(details.get("distinct_days") or 0)
    window_days = int(details.get("window_days") or 30)
    frustrated_share = float(details.get("frustrated_share") or 0.0)
    positive_share = float(details.get("positive_mood_share") or 0.0)
    domain_tags = details.get("domain_tags") or []
    if not isinstance(domain_tags, list):
        domain_tags = []

    verb_v = _verb_friendly(verb)

    # ── Headline / Summary (Tier 1) ─────────────────────────────────────────
    # Headline + summary vary by alignment. ``contradicted`` reframes
    # the pitch entirely — the operator's job isn't to deepen, it's
    # to decide whether the bot's stated scope or the user's actual
    # use should change.
    if alignment == "contradicted":
        headline = (
            f"Pattern contradicts stated scope: {bot_id} "
            f"{verb_v} {noun}"
        )[:120]
        summary = (
            f"{bot_id} users keep engaging deeply on *{noun}* — "
            f"{sessions} session{'s' if sessions != 1 else ''} across "
            f"{days} day{'s' if days != 1 else ''}, "
            f"engagement total {engagement} — but the bot's AGENTS.md "
            f"explicitly marks this domain as out of scope. Make a "
            f"decision: widen scope, redirect users, or tolerate the "
            f"gap."
        )
    else:
        headline = (
            f"Amplify what's working: {bot_id} {verb_v} {noun}"
        )[:120]
        if alignment == "confirmed":
            summary = (
                f"{bot_id} is engaging deeply on *{noun}* — {sessions} "
                f"session{'s' if sessions != 1 else ''} across "
                f"{days} day{'s' if days != 1 else ''}, "
                f"engagement total {engagement}, very low frustration "
                f"({int(round(frustrated_share*100))}%). This is the "
                f"bot's stated scope working well — consider deepening "
                f"it."
            )
        else:
            summary = (
                f"{bot_id} users are organically converging on "
                f"*{noun}* — {sessions} session"
                f"{'s' if sessions != 1 else ''} across "
                f"{days} day{'s' if days != 1 else ''}, engagement "
                f"total {engagement}. The bot's AGENTS.md doesn't "
                f"explicitly mention this; consider whether to embrace "
                f"it."
            )

    # ── Explanation (Tier 2) ───────────────────────────────────────────────
    exp_parts: list[str] = []
    # Diagnosis section.
    domain_phrase = (
        f" ({', '.join(d.split(':', 1)[-1] for d in domain_tags)})"
        if domain_tags else ""
    )
    exp_parts.append(
        f"Over the last {window_days} days, ({noun}, {verb}) "
        f"conversations{domain_phrase} have shown the strongest "
        f"sustained engagement pattern on this bot: "
        f"{sessions} distinct session{'s' if sessions != 1 else ''}, "
        f"{days} day{'s' if days != 1 else ''}, engagement total "
        f"{engagement}, frustration "
        f"{int(round(frustrated_share*100))}%."
    )
    if positive_share > 0:
        exp_parts.append(
            f"Positive-mood share (enthusiastic + reflective) was "
            f"{int(round(positive_share*100))}%."
        )

    # Alignment framing.
    exp_parts.append("")
    if alignment == "confirmed":
        exp_parts.append(
            "**Why this matters.** This domain is in the bot's stated "
            "scope per its AGENTS.md, and the data shows users are "
            "consistently engaging with it. A working pattern is "
            "worth investing in — deepening doesn't change what the "
            "bot does, it raises the floor on what it does best."
        )
    elif alignment == "contradicted":
        exp_parts.append(
            "**Why this matters.** The bot's AGENTS.md explicitly "
            "marks this domain as out of scope (look for the "
            "`## Out of scope` section or an inline "
            "`out of scope:` line). But users are still engaging "
            "deeply with the pattern — and the bot isn't redirecting "
            "them away, or they're ignoring the redirect. That's a "
            "real-world signal worth making an explicit operator "
            "decision about. The cost of letting this drift is that "
            "users get a fragmented experience: sometimes the bot "
            "answers, sometimes it deflects, neither side knows the "
            "rule."
        )
    else:
        exp_parts.append(
            "**Why this matters.** Users have organically converged on "
            "this conversational pattern even though the bot's "
            "AGENTS.md doesn't call it out. That's a signal worth "
            "noticing: either the bot's stated scope is narrower than "
            "its real use, or users have found a use the bot's "
            "designer didn't anticipate. Either way the gap is "
            "operator-readable."
        )

    # Action options — different framing per alignment.
    exp_parts.append("")
    if alignment == "contradicted":
        exp_parts.append("**Ways to make this decision:**")
        exp_parts.append(
            f"- **Widen the scope**: remove `{noun}` from the "
            f"`## Out of scope` section of AGENTS.md (and mention it "
            f"in the bot's purpose if appropriate). The user pattern "
            f"becomes a legitimate part of what the bot does."
        )
        exp_parts.append(
            f"- **Redirect users**: add an explicit AGENTS.md note "
            f"like \"if a user asks about `{noun}`, say I don't "
            f"handle that — point them to [the right place].\" Makes "
            f"the boundary visible to users instead of the bot "
            f"silently muddling through."
        )
        exp_parts.append(
            f"- **Dismiss / tolerate as-is**: if the current behavior "
            f"is acceptable — either because the pattern is a passing "
            f"fad, or because you accept the fragmentation in exchange "
            f"for keeping the bot's stated scope narrow — dismiss this "
            f"proposal. The exclusion stays in AGENTS.md, the bot "
            f"keeps doing whatever it currently does, and the same "
            f"(noun, verb) won't be re-surfaced for a while."
        )
    else:
        exp_parts.append("**Ways to deepen this pattern:**")
        exp_parts.append(
            f"- **Schedule a proactive surface**: add a cron / "
            f"scheduled action that initiates a `{verb_v} {noun}` "
            f"conversation on a cadence that matches how often users "
            f"land on it organically."
        )
        exp_parts.append(
            f"- **Formalize as an app**: draft a manifest scoped to "
            f"`{noun}` so the workflow has a name, a maintained file "
            f"set, and a discoverable surface in the app catalog."
        )
        if alignment == "emergent":
            exp_parts.append(
                f"- **Update AGENTS.md** to explicitly include "
                f"`{noun}` in the bot's stated purpose. Even just "
                f"naming it changes how the model frames future "
                f"conversations on the topic."
            )
        else:
            exp_parts.append(
                f"- **Add a depth note to AGENTS.md** — short "
                f"guidance that the bot can lean into `{noun}` "
                f"conversations more (cite a deeper follow-up "
                f"question, surface a relevant artifact, etc.) since "
                f"users keep engaging."
            )
        exp_parts.append(
            f"- **Dismiss** if amplifying isn't the right move — "
            f"e.g. the pattern is peripheral to the bot's purpose, "
            f"or you'd rather redirect users elsewhere. The same "
            f"(noun, verb) won't be re-suggested for a while."
        )

    explanation = "\n".join(exp_parts)
    context = explanation

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"engagement_amplification:{bot_id}:{noun}:{verb}",
            f"alignment:{alignment}",
            f"engagement:{engagement}",
        ],
        provenance=Provenance(
            technique="engagement_amplifier.pattern_signal",
            signals={
                "noun": noun,
                "verb": verb,
                "domain_tags": list(domain_tags),
                "objective_alignment": alignment,
                "engagement_total": engagement,
                "distinct_sessions": sessions,
                "distinct_days": days,
                "frustrated_share": frustrated_share,
                "positive_mood_share": positive_share,
                "window_days": window_days,
                "grounding_signal_ids": [getattr(sig, "id", "")],
            },
            confidence=0.8,
        ),
        problem=(
            f"{bot_id}: "
            + (
                f"decide on {verb_v} {noun} (contradicted, "
                f"engagement {engagement})"
                if alignment == "contradicted"
                else f"amplify {verb_v} {noun} ({alignment} alignment, "
                f"engagement {engagement})"
            )
        ),
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=[],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline,
        motivating_signals=[getattr(sig, "id", "")],
        # ── Phase A operator-first content ──
        summary=summary,
        explanation=explanation,
        action_label=(
            "Make a scope decision"
            if alignment == "contradicted"
            else "Pick a way to deepen"
        ),
        manual_path=f"Bots → {bot_id} → Applications + AGENTS.md",
        dismiss_signature=_dismiss_signature(bot_id, noun, verb),
        dismiss_scope="kind",
    )


def observe(ctx: EngagementAmplifierContext) -> list[Proposal]:
    """Pod-wide entry point. Reads firing
    ``engagement_amplification_opportunity`` Signals per bot and
    emits one Phase A operator-first Proposal per opportunity, up to
    ``ctx.max_per_run``.

    Deterministic ordering: iterate bots in ``ctx.bot_ids`` order;
    within each bot, iterate Signals in store order (which is
    file-system / creation-time order). Stable picks make "why this
    one?" easy to answer in operator review.
    """
    proposals: list[Proposal] = []
    limit = max(1, ctx.max_per_run)
    for bot_id in ctx.bot_ids:
        if len(proposals) >= limit:
            break
        for sig in _grounding_signals(ctx.shared_dir, bot_id):
            if len(proposals) >= limit:
                break
            p = _build_proposal(sig, ctx)
            if p is not None:
                proposals.append(p)
    return proposals
