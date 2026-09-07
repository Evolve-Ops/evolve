"""generators.cache_ttl_tuner.observe — Detector entry point.

Reads firing Signals from the ``session_economics`` producer and turns
each into a Proposal:

  * ``cache_invalidation_elevated`` → UpdateAgentDefaults Proposal
    flipping ``params.cacheRetention`` to ``"long"``, with two
    suppression gates first:

      1. **config-intent check.** If the operator has deliberately set
         ``cacheRetention="short"`` via the Cost-tab override (recorded
         in ``{shared_dir}/config_intents/<bot_id>.json``), don't
         re-propose the inverse. Falls through to an Investigation
         fallback so the firing signal still surfaces an explanation.

      2. **proposal_history dedup window.** If a Proposal with the
         same cause_key was emitted in the last
         ``PROPOSAL_HISTORY_WINDOW_DAYS`` and is still in
         pending/applied (or was archived dismissed within the window),
         skip — avoids spamming after the operator has either approved
         the flip (it's applied; let it bake) or dismissed it
         (respect the call).

  * ``cache_hit_rate_low`` → Investigation Proposal. No autonomous fix
    — prompt-structure churn is a code change.

Signal-consumer pattern mirrors ``efficiency_hawk._observe_from_signals``,
just pointed at a different producer with a different factory table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from generators.cache_ttl_tuner.applicability import bot_uses_anthropic
from generators.cache_ttl_tuner.signal_proposals import (
    CAUSE_KEY_TTL_TOO_SHORT,
    INTENT_FIELD_PATH,
    PROPOSAL_HISTORY_WINDOW_DAYS,
    SIGNAL_TYPE_TO_FACTORY,
    make_cache_invalidation_investigation_fallback,
)
from schema.proposal import Proposal


GENERATOR_ID = "cache_ttl_tuner"
DIMENSION = "efficiency"

# Producer name on Signals emitted by the session_economics monitor.
# This generator subscribes to those Signals and turns each into a
# Proposal.
SESSION_ECONOMICS_PRODUCER = "session_economics"


@dataclass
class CacheTtlTunerContext:
    """Per-bot run context.

    ``consult_proposal_history``, ``consult_config_intent``,
    ``check_applicability``, and ``consult_dismissals`` are test
    hooks — production runs leave them at True. ``now`` is injectable
    so dedup-window tests can pin wall time.
    """

    bot_id: str
    shared_dir: Path
    consult_proposal_history: bool = True
    consult_config_intent: bool = True
    # Phase B (2026-06-04) — applicability gate. Skip emission if the
    # bot doesn't use Anthropic models. Tests pinning fixed proposal
    # output can set False.
    check_applicability: bool = True
    # Phase A.5 — universal dismiss suppression. Skip emission if a
    # matching dismiss entry is active for this bot in the
    # dismissed_signatures store.
    consult_dismissals: bool = True
    now: datetime | None = None


def _operator_pinned_short(
    bot_id: str, shared_dir: Path,
) -> tuple[bool, str | None]:
    """Return ``(suppress, reason)`` based on config-intent state.

    Suppress when an active intent record pins
    ``agents.defaults.models.*.params.cacheRetention`` to ``"short"``
    — the operator has deliberately chosen that value and the
    autonomous fix would override the intent. We honor the intent and
    fall through to the Investigation fallback.

    Fail-open: any import / read failure returns ``(False, None)`` so a
    broken intent sidecar can't suppress a legitimate proposal.
    """
    try:
        from evolve_admin.config_intent import (
            get_intent,
            intent_still_valid,
        )
    except ImportError:
        return False, None
    try:
        intent = get_intent(bot_id, INTENT_FIELD_PATH, shared_dir=shared_dir)
    except Exception:
        return False, None
    if not isinstance(intent, dict):
        return False, None
    if not intent_still_valid(intent, shared_dir=shared_dir):
        return False, None
    if intent.get("value") != "short":
        # Operator pinned ``long`` (or some other value the applier
        # would have refused). Either way, the autonomous fix isn't
        # the inverse of an active short-intent, so no suppression.
        return False, None
    return True, "operator_intent_pinned_short"


def _load_suppressed_signatures(
    shared_dir: Path, bot_id: str, *, consult: bool,
) -> set[str]:
    """Thin wrapper around :func:`arbiter.dismissals.preload_suppressed_signatures`
    that honors the local ``consult`` test hook."""
    if not consult:
        return set()
    from arbiter.dismissals import preload_suppressed_signatures
    return preload_suppressed_signatures(shared_dir, bot_id)


def _recent_proposal_exists(
    bot_id: str,
    shared_dir: Path,
    *,
    cause_key: str,
    window_days: int,
    now: datetime,
) -> tuple[bool, str | None]:
    """Return ``(suppress, reason)`` based on proposal_history dedup.

    Suppress when a Proposal with the same ``cause_key`` was created
    within the last ``window_days`` and is in any non-terminal state
    OR was archived with a decline status (rejected / dismissed) in
    the window — re-emitting after the operator said no is noise.

    ``applied`` status counts as suppression too: the fix has landed,
    let it bake; if the signal still fires after the verify window,
    the next run after the dedup window expires will re-emit.

    Fail-open on read errors.
    """
    try:
        from investigation.proposal_history import proposal_history
    except ImportError:
        return False, None
    try:
        entries = proposal_history(
            shared_dir,
            bot_id=bot_id,
            generator_id=GENERATOR_ID,
            cause_key=cause_key,
            limit=20,
        )
    except Exception:
        return False, None
    if not entries:
        return False, None

    cutoff = now - timedelta(days=window_days)
    for entry in entries:
        created_str = entry.created_at or ""
        if not created_str:
            continue
        try:
            # created_at format on Proposal is ISO8601 with 'Z' or +00:00.
            created_norm = created_str.rstrip("Z") + "+00:00" if created_str.endswith("Z") else created_str
            created = datetime.fromisoformat(created_norm)
        except (ValueError, TypeError):
            continue
        # Normalize tz — treat naive as UTC for comparison stability.
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            return True, f"recent_proposal_{entry.status}@{created_str[:10]}"
    return False, None


def observe(ctx: CacheTtlTunerContext) -> list[Proposal]:
    """Read firing session_economics Signals and produce Proposals.

    For ``cache_invalidation_elevated`` signals, runs the suppression
    gates (config-intent + proposal_history dedup) before deciding
    between the typed UpdateAgentDefaults Proposal and the
    Investigation fallback.

    Filters by ``bot_id`` so a per-bot generator run only emits
    proposals for its own bot. Signal types not in
    :data:`signal_proposals.SIGNAL_TYPE_TO_FACTORY` are silently
    skipped. A bad payload on one signal must not torpedo the rest;
    per-signal errors are swallowed.
    """
    if ctx.shared_dir is None:
        return []
    try:
        from signals import store as signals_store
    except ImportError:
        return []

    # Phase B applicability gate. cacheRetention is Anthropic-only;
    # emitting on a non-Anthropic bot wastes operator attention. Runs
    # before signal iteration because the gate is bot-wide, not
    # per-signal. Fail-open on read failure (see applicability docstring).
    if ctx.check_applicability and not bot_uses_anthropic(ctx.bot_id):
        return []

    # Phase A.5 dismiss-suppression gate. Pre-loaded so each signal's
    # emission can check it cheaply.
    suppressed_signatures = _load_suppressed_signatures(
        ctx.shared_dir, ctx.bot_id, consult=ctx.consult_dismissals,
    )

    now = ctx.now or datetime.now(timezone.utc)

    proposals: list[Proposal] = []
    for sig in signals_store.iter_active(
        ctx.shared_dir,
        producer=SESSION_ECONOMICS_PRODUCER,
        bot_id=ctx.bot_id,
        state="firing",
    ):
        factory = SIGNAL_TYPE_TO_FACTORY.get(sig.type)
        if factory is None:
            continue

        if sig.type == "cache_invalidation_elevated":
            # Suppression gates — these run BEFORE the typed factory so
            # we don't burn the proposal_id and dedup slot on a
            # Proposal we're about to discard.
            suppress_reasons: list[str] = []

            if ctx.consult_config_intent:
                suppressed, reason = _operator_pinned_short(
                    ctx.bot_id, ctx.shared_dir,
                )
                if suppressed and reason:
                    suppress_reasons.append(reason)

            if ctx.consult_proposal_history:
                suppressed, reason = _recent_proposal_exists(
                    ctx.bot_id, ctx.shared_dir,
                    cause_key=CAUSE_KEY_TTL_TOO_SHORT,
                    window_days=PROPOSAL_HISTORY_WINDOW_DAYS,
                    now=now,
                )
                if suppressed and reason:
                    suppress_reasons.append(reason)

            if suppress_reasons:
                # Fall through to the Investigation fallback — the
                # signal is firing and the operator deserves to see
                # what was suppressed and why. The fallback itself
                # honors dismiss-signature suppression below.
                try:
                    fallback = make_cache_invalidation_investigation_fallback(sig)
                except Exception:
                    continue
                if fallback.dismiss_signature in suppressed_signatures:
                    continue  # operator dismissed this finding; respect that
                proposals.append(fallback)
                continue

        try:
            # Forward shared_dir so factories can scope cost-ledger
            # lookups (used by the savings estimator). Factories
            # accept the kwarg as optional; missing kwarg = fall back
            # to the cost_ledger default shared_dir.
            proposal = factory(sig, shared_dir=ctx.shared_dir)
        except Exception:
            continue
        # Phase A.5 — skip if this finding's signature is actively
        # dismissed for this bot. Check after construction (the
        # factory owns the signature value) so per-finding-type
        # gating is fully encapsulated in the factory's content.
        if proposal.dismiss_signature in suppressed_signatures:
            continue
        proposals.append(proposal)
    return proposals
