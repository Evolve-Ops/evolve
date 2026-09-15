"""Security Warden observe() — scans transcript snippets for safety events.

Two detectors run on every snippet:

  - Credential exposure (L3): regex + LLM verifier in
    ``scanners/credentials.py``. Critical-severity Investigation when a
    real credential leaks into a transcript.
  - Prompt injection (L6 sub-spec): regex + LLM verifier in
    ``scanners/prompt_injection.py``. Critical-severity Investigation
    when a user message attempts to override the bot's instructions.

Proposals emitted from this path NEVER quote the detected content
verbatim — the redaction invariant from the charter is enforced before
emission.

Per-run dedup: within a single ``observe()`` call we suppress duplicate
emissions for the same ``(session_id, sorted-pattern-tuple)`` pair, so a
user pasting the same injection across multiple turns produces one
proposal, not N.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

from generators.security_warden import redact
from generators.security_warden import do_not_reflag as _dnr
from generators.security_warden.scanners.credentials import (
    scan_text as scan_credentials,
)
from generators.security_warden.scanners.prompt_injection import (
    scan_text as scan_injection,
)
from proposal_synthesizer.emit import emit_from_proposal
from schema.candidate_proposal import Magnitude
from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


# Callable: (bot_id, lookback_hours) -> list of transcript snippets to scan.
# The returned dict shape per snippet:
#   {"session_id": str, "turn_index": int, "text": str}
TranscriptReader = Callable[[str, int], list[dict]]


# Match the RecentTranscriptCapture buffer (200 turns / 48h). Picking 24h
# meant any cycle gap > 24h dropped events that were still in the buffer.
DEFAULT_LOOKBACK_HOURS = 48


@dataclass
class WardenContext:
    bot_id: str
    transcript_reader: TranscriptReader
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Where the persistent do_not_reflag list lives. None disables the
    # cross-session suppression check (per-run dedup still applies).
    shared_dir: "Path | None" = None
    # Pod-wide network.json contents — needed by posture.check_multi_user_posture
    # to read pod.admins and bot's primary_user/multiUser fields. Optional
    # so unit tests that exercise only the scanners can omit it.
    network: dict | None = None
    # Per-bot openclaw.json reader: (bot_id) -> dict | None. Returns None when
    # the file is unreadable; posture checks that depend on it skip silently.
    oc_config_reader: "Callable[[str], dict | None] | None" = None


GENERATOR_ID = "security_warden"
DIMENSION = "safety"


def observe_signals(ctx: WardenContext) -> list[dict]:
    """Return Signal-spec kwargs the runner emits before observe().

    Currently sources from ``posture.check_multi_user_posture`` — the
    transcript scanners produce Proposals (with mirrored Signals via
    ``_emit_warden_signals``), not direct Signals.

    Posture findings clear automatically: the runner sweep-resolves any
    security_warden Signal whose signature isn't re-emitted this cycle
    for a bot we visited, so once the operator fixes the underlying
    config the alert disappears on the next run.
    """
    from generators.security_warden import posture

    if ctx.network is None:
        return []

    oc_config: dict | None = None
    if ctx.oc_config_reader is not None:
        try:
            oc_config = ctx.oc_config_reader(ctx.bot_id)
        except Exception:
            oc_config = None

    specs = posture.check_multi_user_posture(
        bot_id=ctx.bot_id,
        network=ctx.network,
        oc_config=oc_config,
    )

    # V1.5-3: also surface the per-bot OpenClaw version-floor check. This
    # runs for every bot (single-user too) — being below the upstream
    # floor blocks adoption of openclaw exec-policy CLI regardless of
    # how many users a bot serves.
    version_spec = posture.check_openclaw_version_floor(
        bot_id=ctx.bot_id,
        oc_config=oc_config,
    )
    if version_spec is not None:
        specs.append(version_spec)

    return specs


def observe(ctx: WardenContext) -> list[Proposal]:
    """Run every Warden scanner over recent transcript snippets."""
    snippets = ctx.transcript_reader(ctx.bot_id, ctx.lookback_hours)
    if not snippets:
        return []

    proposals: list[Proposal] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for snippet in snippets:
        text = str(snippet.get("text", ""))
        if not text:
            continue
        for proposal in _scan_one(ctx, snippet, text, seen):
            proposals.append(proposal)

    # Phase 5 of the alerts/signal-store consolidation
    # (internal/spec-alerts-signal-store-2026-05-07.md): mirror each warden
    # proposal into a conduct_violation Signal and wire motivating_signals
    # so the proposal traces back to the originating Signal. Best-effort.
    if ctx.shared_dir is not None:
        try:
            _emit_warden_signals(ctx, proposals)
        except Exception:
            pass

    # Phase 6c cutover: emit each Proposal as a CandidateProposal and
    # return []. The substantiveness gate (severity_level floor 2 by
    # default) filters low-severity findings until they recur.
    for p in proposals:
        tech = (p.provenance.technique or "").split(".", 1)[-1]
        severity = _urgency_to_severity_level(p.urgency)
        emit_from_proposal(
            p,
            variant=tech or "security_finding",
            magnitude=Magnitude(unit="severity_level", value=severity),
            shared_dir=ctx.shared_dir,
        )

    return []


def _urgency_to_severity_level(urgency: str) -> float:
    """Map Proposal.urgency to the gate's 1-4 severity_level scale."""
    return {
        "security_critical": 4.0,
        "operational_urgent": 3.0,
        "cost_alert": 2.0,
        "substrate_warn": 2.0,
        "improvement": 1.0,
        "hygiene": 1.0,
        "whimsy": 1.0,
    }.get(urgency, 2.0)


def _emit_warden_signals(ctx: WardenContext, proposals: list[Proposal]) -> None:
    """Emit one ``conduct_violation_*`` Signal per Warden proposal.

    Signature derives from the proposal's ``trigger`` field so the same
    incident (same session/turn/pattern set) collapses into one rolling
    Signal across runs. Each proposal's ``motivating_signals[]`` is
    back-filled with the new Signal's id, satisfying Phase 4's
    bidirectional link contract.
    """
    from signals import store as signals_store
    from schema.signal import make_signature

    for proposal in proposals:
        provenance_signals = getattr(proposal.provenance, "signals", {}) or {}
        trigger_obs = list(proposal.trigger_observations or [])
        trigger = trigger_obs[0] if trigger_obs else "unknown"

        if trigger.startswith("prompt_injection:"):
            sig_type = "conduct_violation_prompt_injection"
            scope = "bot"
            short_title = "Prompt injection attempt"
            # Active exploitation attempt — magnitude 3 (real exposure,
            # scoped to one session/bot). Producer escalates per-emit
            # to 4 only when a pattern matches a critical jailbreak corpus.
            magnitude = 3
        elif trigger.startswith("credential_exposure:"):
            sig_type = "conduct_violation_credential_leak"
            scope = "bot"
            short_title = "Credential exposure"
            # Token surface visible in chat is the canonical "catastrophic"
            # signal — magnitude 4 (security spec anchor §2.1 level 4).
            magnitude = 4
        else:
            sig_type = "conduct_violation"
            scope = "bot"
            short_title = "Conduct violation"
            magnitude = 3

        scope_key = f"{ctx.bot_id}:{trigger}"
        signature = make_signature("security_warden", sig_type, scope_key)
        try:
            sig = signals_store.observe(
                ctx.shared_dir,
                signature=signature,
                producer="security_warden",
                type=sig_type,
                flavor="maintenance",
                severity="alert",
                scope=scope,
                bot_id=ctx.bot_id,
                title=f"{short_title} on {ctx.bot_id}",
                body=proposal.problem,
                details={
                    "trigger": trigger,
                    "pattern_ids": list(provenance_signals.get("patterns") or []),
                    "session_id": provenance_signals.get("session_id"),
                    # Severity framework: security vector. Spec: §2.1.
                    # Always active — these signals fire on a fresh
                    # detection of a current session/turn.
                    "vector": "security",
                    "magnitude": magnitude,
                    "severity_active": True,
                },
            )
            if sig.id not in proposal.motivating_signals:
                proposal.motivating_signals.append(sig.id)
        except Exception:
            continue


def _scan_one(
    ctx: WardenContext,
    snippet: dict,
    text: str,
    seen: set[tuple[str, str, tuple[str, ...]]],
) -> list[Proposal]:
    out: list[Proposal] = []

    cred_result = scan_credentials(text)
    if cred_result.has_exposure:
        pattern_ids = tuple(sorted({m.pattern_id for m in cred_result.matches}))
        key = (snippet.get("session_id", "?"), "credential", pattern_ids)
        if key not in seen:
            seen.add(key)
            cred_proposal = _build_credential_proposal(ctx, snippet, cred_result)
            if cred_proposal is not None:
                out.append(cred_proposal)

    inj_result = scan_injection(text)
    if inj_result.has_injection:
        pattern_ids = tuple(sorted({m.pattern_id for m in inj_result.matches}))
        key = (snippet.get("session_id", "?"), "injection", pattern_ids)
        if key not in seen:
            seen.add(key)
            if ctx.shared_dir is not None and _dnr.is_suppressed(
                ctx.shared_dir, ctx.bot_id, pattern_ids
            ):
                logger.info(
                    "security_warden: suppressing injection emission for %s "
                    "(patterns=%s) — entry on do_not_reflag list",
                    ctx.bot_id, list(pattern_ids),
                )
            else:
                inj_proposal = _build_injection_proposal(ctx, snippet, inj_result)
                if inj_proposal is not None:
                    out.append(inj_proposal)

    return out


def _build_warden_proposal(
    ctx: WardenContext,
    *,
    trigger: str,
    technique: str,
    signals: dict,
    confidence: float,
    problem: str,
    context: str,
    dismiss_signature: str = "",
) -> Proposal | None:
    """Construct + redact-check a Warden proposal.

    Returns None if either ``problem`` or ``context`` would leak a secret —
    the charter's ``never_reveals_secrets`` invariant must hold for every
    emitted proposal regardless of which scanner produced it.
    """
    if not redact.is_safe(problem) or not redact.is_safe(context):
        return None

    return Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[trigger],
        provenance=Provenance(
            technique=technique,
            signals=signals,
            confidence=confidence,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=[],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="security_critical",
        admin_surface_summary=problem[:120],
        # Per-finding-kind dismiss signature passed in by the caller —
        # credential scanner uses one kind, prompt-injection another.
        dismiss_signature=dismiss_signature or f"security_warden:{technique}",
        dismiss_scope="kind",
    )


def _build_credential_proposal(
    ctx: WardenContext, snippet: dict, result
) -> Proposal | None:
    session_id = snippet.get("session_id", "?")
    turn_index = snippet.get("turn_index", "?")
    problem = (
        f"Credential exposure detected in {ctx.bot_id} session "
        f"{session_id}: {result.summary}"
    )
    context = (
        f"Detected credential-like content in a recent session for "
        f"{ctx.bot_id}. Session: {session_id}, "
        f"turn index: {turn_index}. "
        f"Patterns matched: {result.summary}. "
        "Review the session transcript and rotate any exposed credentials."
    )
    return _build_warden_proposal(
        ctx,
        trigger=f"credential_exposure:{session_id}:{turn_index}",
        technique="security_warden.credential_scan",
        signals={
            "patterns": [m.pattern_id for m in result.matches],
            "match_count": len(result.matches),
        },
        confidence=0.95,
        problem=problem,
        context=context,
    )


def _build_injection_proposal(
    ctx: WardenContext, snippet: dict, result
) -> Proposal | None:
    session_id = snippet.get("session_id", "?")
    turn_index = snippet.get("turn_index", "?")
    pattern_ids = [m.pattern_id for m in result.matches]
    verifier = result.verifier
    rationale = (verifier.rationale if verifier else "").strip()
    confidence = verifier.confidence if verifier else 1.0

    problem = (
        f"Prompt injection attempt in {ctx.bot_id} session "
        f"{session_id}: {result.summary}"
    )
    context_lines = [
        f"Detected prompt-injection pattern(s) in a recent session for "
        f"{ctx.bot_id}. Session: {session_id}, turn index: {turn_index}.",
        f"Patterns matched: {result.summary}.",
        f"Verifier verdict: injection (confidence {confidence:.2f}).",
    ]
    if rationale:
        context_lines.append(f"Verifier rationale: {rationale}")
    context_lines.append(
        "Review the session transcript and decide whether to ban the source, "
        "tighten the bot's prompt, or dismiss as benign."
    )

    return _build_warden_proposal(
        ctx,
        trigger=f"prompt_injection:{session_id}:{turn_index}",
        technique="security_warden.prompt_injection_scan",
        signals={
            "patterns": pattern_ids,
            "match_count": len(result.matches),
            "verifier_confidence": confidence,
        },
        confidence=confidence,
        problem=problem,
        context="\n".join(context_lines),
    )
