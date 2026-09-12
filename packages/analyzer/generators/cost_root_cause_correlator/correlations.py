"""generators.cost_root_cause_correlator.correlations — Dispatch table.

Each ``Correlation`` declares:

  * ``key`` — stable identifier (e.g. ``cache_invalidation_drives_outliers``);
    used as the proposal's ``cause_key`` and the dedup key for
    proposal_history.
  * ``chronic_producer`` / ``chronic_types`` — which chronic Signals on
    the same bot corroborate the acute one.
  * ``detect`` — pure function. Given the acute Signal + the list of
    chronic Signals + the bot's current openclaw.json, returns a
    ``CorrelationMatch`` (with the field to flip and operator-facing
    prose) or ``None`` (no synthesis warranted today).

The dispatcher loops through every registered correlation. When more
than one fires on the same acute Signal we keep the first match — these
are meant to be mutually exclusive on cost root causes (cache vs
heartbeat vs model drift); if that assumption ever bends, the
dispatcher will need a priority order, but we'll cross that bridge
when a second correlation lands.

Today there is exactly one correlation registered:
``cache_invalidation_drives_outliers``. Extending the generator to
heartbeat bloat or model-tier drift means adding a new ``Correlation``
entry here — the observe.py shell does not need to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from investigation.toolkit import CorrelatedSignal


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CorrelationMatch:
    """One synthesis decision returned by a correlation's detect()."""

    # Stable cause_key; matches the Correlation.key it came from.
    cause_key: str
    # One-line operator headline for the proposal.
    headline: str
    # Full operator-facing body (markdown, multi-paragraph). The
    # observer wraps this verbatim into ``Proposal.problem`` so the
    # admin UI shows it above the action panel.
    body: str
    # The acute Signal whose firing prompted this synthesis. Used for
    # motivating_signals + provenance.
    acute: CorrelatedSignal
    # The chronic Signal(s) that corroborated. Used for
    # motivating_signals + provenance + the headline cross-reference.
    chronic: list[CorrelatedSignal]
    # The UpdateAgentDefaults fields payload — e.g.
    # ``{"agents.defaults.models.*.params.cacheRetention": "long"}``.
    # Empty dict means "synthesize Investigation only, no autonomous fix"
    # (reserved for future correlations).
    fields: dict[str, Any]
    # Confidence in [0, 1]; flows into Provenance.
    confidence: float = 0.85
    # Free-form structured evidence for provenance.signals. Anything
    # that helps the operator (and future investigations) understand
    # why this Proposal exists.
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Correlation:
    """Static registration of one chronic↔acute pairing."""

    key: str
    chronic_producer: str
    chronic_types: tuple[str, ...]
    detect: Callable[
        [CorrelatedSignal, list[CorrelatedSignal], dict],
        CorrelationMatch | None,
    ]
    # The agents.defaults.* dotpath the correlation's fix targets.
    # Recorded here so the observer can check config_intent for an
    # operator pin on that dotpath without re-reading the fields dict.
    # None for correlations that emit Investigation-only.
    intent_field_path: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers used by individual correlation detect() functions
# ─────────────────────────────────────────────────────────────────────────────


def _current_cache_retention(openclaw_config: dict | None) -> str | None:
    """Best-effort read of the bot's current cacheRetention.

    The materializer fans the override out to every Anthropic model's
    ``params.cacheRetention``, so the effective value is the consensus
    across Anthropic catalog entries. We return:

      * ``"short"`` or ``"long"`` if every Anthropic model agrees
      * ``"mixed"`` if Anthropic models disagree (rare; operator hand-
        edited the catalog)
      * ``None`` if there are no Anthropic models in the catalog or the
        config shape is unrecognized — caller treats this as "we can't
        be sure, don't synthesize."

    Pure on ``openclaw_config``; the caller hands in whatever they read
    off disk.
    """
    if not isinstance(openclaw_config, dict):
        return None
    agents = openclaw_config.get("agents")
    if not isinstance(agents, dict):
        return None
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return None
    models = defaults.get("models")
    if not isinstance(models, dict):
        return None
    seen: set[str] = set()
    for model_id, model_cfg in models.items():
        # Match the same Anthropic prefix the materializer uses so this
        # function tracks fan-out semantics exactly.
        if not (isinstance(model_id, str) and model_id.startswith("anthropic/")):
            continue
        if not isinstance(model_cfg, dict):
            continue
        params = model_cfg.get("params")
        if not isinstance(params, dict):
            continue
        val = params.get("cacheRetention")
        if isinstance(val, str):
            seen.add(val)
    if not seen:
        return None
    if len(seen) == 1:
        return next(iter(seen))
    return "mixed"


# ─────────────────────────────────────────────────────────────────────────────
# Correlation 1: cache_invalidation_elevated chronic → cacheRetention flip
# ─────────────────────────────────────────────────────────────────────────────
#
# Motivating incident: team-bot-a session 3d5cde22 (2026-05-29, $7.67).
# cost_watchdog fired session_token_outlier on the bot at the same time
# session_economics had a firing cache_invalidation_elevated (49% over
# 7d). 92% of the outlier session's spend was prompt-cache writes that
# the 5-min default TTL invalidated before reuse on Slack-DM-paced
# turns. PR A exposed the knob, PR B added the autonomous applier, this
# generator closes the synthesis gap that left the operator to do the
# correlation by hand.


_CACHE_RETENTION_DOTPATH = "agents.defaults.models.*.params.cacheRetention"


def _format_acute_summary(acute: CorrelatedSignal) -> str:
    """Render the one-line acute-symptom summary for the proposal body.

    Handles all three acute signal types we consume. Each carries
    slightly different details; we degrade gracefully when fields are
    missing (older Signals, automation-only days).
    """
    details = acute.details or {}
    sig_type = acute.type
    if sig_type == "session_token_outlier":
        sid = str(details.get("session_id") or "")
        cost = float(details.get("cost_usd") or 0.0)
        ratio = float(details.get("ratio") or 0.0)
        user_name = details.get("user_display_name")
        channel_kind = details.get("channel_kind")
        when_local = details.get("timestamp_local")
        who_where = ""
        if user_name and channel_kind:
            who_where = f" ({user_name}, {channel_kind})"
        elif channel_kind:
            who_where = f" ({channel_kind})"
        when = f" at {when_local}" if when_local else ""
        sid_disp = sid[:8] if sid else "?"
        return (
            f"Session {sid_disp}{who_where}{when} "
            f"cost ${cost:.2f} — {ratio:.1f}× this bot's median."
        )
    if sig_type == "cost_spike":
        cur = float(details.get("cost_cur_usd") or 0.0)
        prior = float(details.get("cost_prior_usd") or 0.0)
        ratio = float(details.get("ratio") or 0.0)
        window = int(details.get("window_days") or 7)
        return (
            f"{window}d cost ${cur:.2f} is {ratio:.1f}× prior "
            f"{window}d (${prior:.2f})."
        )
    if sig_type == "daily_spend_high":
        spend = float(details.get("cost_usd") or 0.0)
        threshold = float(details.get("threshold_usd") or 0.0)
        return (
            f"Today's spend ${spend:.2f} crossed the configured "
            f"threshold ${threshold:.2f}."
        )
    return acute.title or f"Acute cost signal ({sig_type}) firing."


def _detect_cache_invalidation(
    acute: CorrelatedSignal,
    chronic: list[CorrelatedSignal],
    openclaw_config: dict,
) -> CorrelationMatch | None:
    """Acute cost signal + chronic cache_invalidation_elevated + short TTL
    → propose flipping cacheRetention to ``long``.

    Returns None when:
      * No chronic cache_invalidation_elevated firing on this bot.
      * The bot's current cacheRetention is already ``long`` (or mixed —
        we don't synthesize when the catalog state is unclear; let the
        operator clean it up first).
      * The Anthropic catalog is empty / unrecognized (no flippable
        field exists).
    """
    chronic_match: CorrelatedSignal | None = None
    for c in chronic:
        if (
            c.producer == "session_economics"
            and c.type == "cache_invalidation_elevated"
        ):
            chronic_match = c
            break
    if chronic_match is None:
        return None

    current_value = _current_cache_retention(openclaw_config)
    if current_value is None:
        # No Anthropic models in the catalog — there's nothing for the
        # cacheRetention knob to fan out to. The chronic signal is
        # diagnosing something other than TTL on this bot, so the right
        # call is to stay silent rather than ship a no-op fix.
        return None
    if current_value == "long":
        # The fix is already in place. The chronic signal hasn't cleared
        # yet (probably because session_economics is still chewing on
        # pre-flip turns); silence is correct until the symptom either
        # resolves or persists past the cache-warming window.
        return None
    if current_value == "mixed":
        # Per-model hand edits — synthesis can't safely flip without
        # potentially regressing a deliberate per-model choice. Stay out.
        return None

    chronic_details = chronic_match.details or {}
    rate_pct = round(
        float(chronic_details.get("invalidated_ratio") or 0.0) * 100, 1,
    )
    invalidated = int(chronic_details.get("invalidated_count") or 0)
    total = int(chronic_details.get("participating_count") or 0)
    window_days = int(chronic_details.get("window_days") or 7)
    bot_id = acute.details.get("bot_id") or "<unknown>"

    acute_summary = _format_acute_summary(acute)
    headline = (
        f"{bot_id}: chronic prompt-cache invalidation is driving "
        f"acute cost outliers"
    )

    # Operator-facing body. Lead with the acute symptom (what fired
    # right now), then the chronic root cause (the pattern), then the
    # proposed fix (the typed action with the knob name), then risk
    # framing (reversible, auto-applicable per existing eligibility
    # tables). Markdown so the admin UI's renderer surfaces the
    # structure.
    lines: list[str] = []
    lines.append(f"**{headline}**")
    lines.append("")
    lines.append("**Acute symptom:**")
    lines.append(acute_summary)
    lines.append("")
    lines.append("**Root cause (chronic pattern on this bot):**")
    lines.append(
        f"{bot_id}'s {window_days}-day cache invalidation rate is "
        f"{rate_pct:.1f}% ({invalidated} of {total} cache-participating "
        f"turns). Conversations with inter-turn gaps longer than 5 "
        f"minutes force full re-writes of the growing prompt context "
        f"every turn — Anthropic's default prompt-cache TTL is 5 "
        f"minutes, and the acute outlier's spend is dominated by "
        f"cacheWrite tokens that never get read back."
    )
    lines.append("")
    lines.append("**Proposed fix:**")
    lines.append(
        f"Flip `agents.defaults.models.*.params.cacheRetention` from "
        f"`short` to `long` (5-min TTL → 1-hour TTL). The materializer "
        f"fans this out to every Anthropic model in `{bot_id}`'s "
        f"catalog. Subsequent turns within the longer window pay only "
        f"cacheRead (typically ~10× cheaper than cacheWrite)."
    )
    lines.append("")
    lines.append("**Risk profile:**")
    lines.append(
        "- Per-bot, single-enum knob — bounded blast radius."
    )
    lines.append(
        "- Fully reversible: flipping back to `short` restores prior "
        "state with no data loss."
    )
    lines.append(
        "- Auto-applicable (`tier_floor=auto-small`) via the L2 "
        "UpdateAgentDefaults applier shipped by PR B."
    )
    lines.append(
        "- The longer TTL means a slightly larger cacheWrite for "
        "sessions that span past the previous window; this only "
        "matters for bots with very short conversational rhythms."
    )

    body = "\n".join(lines)

    evidence = {
        "acute_signal_id": acute.signal_id,
        "acute_signal_type": acute.type,
        "acute_signature": acute.signature,
        "acute_details": acute.details,
        "chronic_signal_id": chronic_match.signal_id,
        "chronic_signal_signature": chronic_match.signature,
        "chronic_rate_pct": rate_pct,
        "chronic_invalidated_count": invalidated,
        "chronic_total_count": total,
        "chronic_window_days": window_days,
        "current_cache_retention": current_value,
        "proposed_cache_retention": "long",
    }

    return CorrelationMatch(
        cause_key="cache_invalidation_drives_outliers",
        headline=headline,
        body=body,
        acute=acute,
        chronic=[chronic_match],
        fields={_CACHE_RETENTION_DOTPATH: "long"},
        confidence=0.85,
        evidence=evidence,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


# Order matters only when correlations stop being mutually exclusive. For
# v1 there is exactly one entry; future entries (heartbeat bloat, model
# drift, …) append here.
REGISTRY: tuple[Correlation, ...] = (
    Correlation(
        key="cache_invalidation_drives_outliers",
        chronic_producer="session_economics",
        chronic_types=("cache_invalidation_elevated",),
        detect=_detect_cache_invalidation,
        intent_field_path=_CACHE_RETENTION_DOTPATH,
    ),
)


def dispatch(
    acute: CorrelatedSignal,
    chronic: list[CorrelatedSignal],
    openclaw_config: dict,
) -> CorrelationMatch | None:
    """Run every registered correlation against the acute Signal; return
    the first match.

    Mutually-exclusive-on-cause invariant: each cost root cause should
    be diagnosed by at most one correlation, so first-match is a
    deliberate simplification. When that bends, this function gains a
    priority order or a multi-match return shape.
    """
    for corr in REGISTRY:
        relevant_chronic = [
            c
            for c in chronic
            if c.producer == corr.chronic_producer
            and c.type in corr.chronic_types
        ]
        if not relevant_chronic:
            continue
        try:
            match = corr.detect(acute, relevant_chronic, openclaw_config)
        except Exception:
            # Per-correlation failure must not poison the dispatcher —
            # other correlations may still match. Investigation toolkit
            # convention: fail open, return None.
            match = None
        if match is not None:
            return match
    return None
