"""proposal_synthesizer.emit — Helpers for emitting CandidateProposals
in parallel with Proposal emission.

Spec: docs/spec-proposal-synthesizer-2026-05-10.md §9 (Phase 1 migration).

In Phase 1 generators continue to write Proposals as before. They ALSO
call one of the helpers in this module to emit a CandidateProposal
into ``candidates/pending/``. The gate (run_once) sweeps that dir on a
cadence and routes candidates per §4 — but no candidate is yet
promoted to a real Proposal. The candidate store is observational
during Phase 1.

Two helpers:

  - :func:`emit_from_signal_proposal` — for the seven signal-driven
    factories in ``signal_proposals.py``. Variant comes from the
    Signal type; magnitude comes from a per-type extractor.
  - :func:`emit_from_proposal` — for the cost-ledger detectors and
    the cluster-outlier path. The caller passes variant + magnitude
    explicitly.

Both swallow exceptions: emission is a side effect; a broken write
must not torpedo the Proposal pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from schema.candidate_proposal import (
    CandidateProposal,
    Magnitude,
    new_candidate_id,
)
from schema.proposal import Proposal
from proposal_synthesizer.store import write_candidate


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Magnitude extractors — one per signal type
# ─────────────────────────────────────────────────────────────────────────────


def _mag_daily_spend_high(details: dict) -> Magnitude:
    cost = float(details.get("cost_usd") or 0.0)
    threshold = float(details.get("threshold_usd") or 0.0)
    ratio = cost / threshold if threshold > 0 else 0.0
    return Magnitude(unit="ratio_over_cap", value=ratio)


def _mag_automation_dominance(details: dict) -> Magnitude:
    return Magnitude(
        unit="pct/share",
        value=float(details.get("automation_ratio") or 0.0),
    )


def _mag_cron_wakes_agent(details: dict) -> Magnitude:
    # Approximate wakes-per-week from the cadence string. Conservative
    # fallback when the cadence isn't parseable: 1 wake/week (so the
    # candidate passes the sessions/week floor of 5 only when cadence
    # is genuinely frequent).
    cadence = str(details.get("cadence") or "")
    wakes_per_week = _parse_cadence_to_wpw(cadence)
    return Magnitude(unit="sessions/week", value=wakes_per_week)


def _mag_cron_overactive(details: dict) -> Magnitude:
    actual = float(details.get("actual_fires") or 0.0)
    expected = float(details.get("expected_fires") or 0.0)
    ratio = actual / expected if expected > 0 else 0.0
    return Magnitude(unit="ratio_over_declared", value=ratio)


def _mag_context_bloat(details: dict) -> Magnitude:
    return Magnitude(unit="kb", value=float(details.get("size_kb") or 0.0))


def _mag_session_token_outlier(details: dict) -> Magnitude:
    return Magnitude(
        unit="usd/session", value=float(details.get("cost_usd") or 0.0)
    )


def _mag_heartbeat_no_model_override(details: dict) -> Magnitude:
    # Approximate sessions/week from the heartbeat cadence.
    every = str(details.get("heartbeat_every") or "")
    wakes_per_week = _parse_cadence_to_wpw(every)
    return Magnitude(unit="sessions/week", value=wakes_per_week)


def _parse_cadence_to_wpw(s: str) -> float:
    """Best-effort cadence string → wakes per week.

    Recognized: ``Nm`` / ``Nmin`` (minutes), ``Nh`` / ``Nhr`` (hours),
    ``Nd`` (days). Unrecognized strings return 0.0.
    """
    s = s.strip().lower()
    if not s:
        return 0.0
    # Strip trailing alpha tail to get number + unit
    import re

    m = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)?$", s)
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = m.group(2) or ""
    if unit.startswith("min") or unit == "m":
        return (60.0 / n) * 24 * 7 if n > 0 else 0.0
    if unit.startswith("h") or unit == "hr":
        return (24.0 / n) * 7 if n > 0 else 0.0
    if unit.startswith("d"):
        return 7.0 / n if n > 0 else 0.0
    return 0.0


_MAGNITUDE_EXTRACTORS: dict[str, Callable[[dict], Magnitude]] = {
    "daily_spend_high": _mag_daily_spend_high,
    "automation_dominance": _mag_automation_dominance,
    "cron_wakes_agent": _mag_cron_wakes_agent,
    "cron_overactive": _mag_cron_overactive,
    "context_bloat": _mag_context_bloat,
    "session_token_outlier": _mag_session_token_outlier,
    "heartbeat_no_model_override": _mag_heartbeat_no_model_override,
}


def _signal_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict (test fixtures use both)."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


# ─────────────────────────────────────────────────────────────────────────────
# Derivers
# ─────────────────────────────────────────────────────────────────────────────


def _candidate_from_proposal(
    proposal: Proposal,
    *,
    variant: str,
    magnitude: Magnitude | None,
) -> CandidateProposal:
    """Project a Proposal into a CandidateProposal."""
    return CandidateProposal(
        id=new_candidate_id(),
        bot_id=proposal.bot_id,
        state="pending",
        generator_id=proposal.generator_id,
        dimension=proposal.dimension,
        variant=variant,
        trigger_observations=list(proposal.trigger_observations),
        provenance=proposal.provenance,
        motivating_signals=list(proposal.motivating_signals),
        magnitude=magnitude,
        draft_problem=proposal.problem,
        draft_headline=proposal.admin_surface_summary or proposal.problem[:120],
        draft_action=proposal.action,
        draft_claim=proposal.claim,
        draft_risk_tag=proposal.risk_tag,
        draft_urgency=proposal.urgency,
        draft_approval_audience=proposal.approval_audience,
        confidence=float(getattr(proposal.provenance, "confidence", 0.0) or 0.0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public emit functions
# ─────────────────────────────────────────────────────────────────────────────


def emit_from_signal_proposal(
    proposal: Proposal,
    signal: Any,
    *,
    shared_dir: Path | None,
) -> CandidateProposal | None:
    """Emit a CandidateProposal derived from a (Proposal, Signal) pair.

    Writes to ``{shared_dir}/candidates/pending/<id>.json``. Returns
    the candidate, or None if ``shared_dir`` is falsy or any step
    fails. Phase 1 shadow-mode: never raises into the Proposal flow.
    """
    if shared_dir is None:
        return None
    try:
        signal_type = _signal_get(signal, "type") or ""
        extractor = _MAGNITUDE_EXTRACTORS.get(signal_type)
        details = _signal_get(signal, "details") or {}
        magnitude = extractor(details) if extractor else None
        candidate = _candidate_from_proposal(
            proposal, variant=signal_type, magnitude=magnitude
        )
        write_candidate(candidate, Path(shared_dir))
        return candidate
    except Exception:
        log.warning(
            "candidate emit failed for proposal %s (signal-driven path)",
            getattr(proposal, "id", "?"),
            exc_info=True,
        )
        return None


def emit_from_proposal(
    proposal: Proposal,
    *,
    variant: str,
    magnitude: Magnitude | None,
    shared_dir: Path | None,
) -> CandidateProposal | None:
    """Emit a CandidateProposal from a Proposal whose magnitude the
    caller knows (cost-ledger detectors, cluster-outlier path)."""
    if shared_dir is None:
        return None
    try:
        candidate = _candidate_from_proposal(
            proposal, variant=variant, magnitude=magnitude
        )
        write_candidate(candidate, Path(shared_dir))
        return candidate
    except Exception:
        log.warning(
            "candidate emit failed for proposal %s (variant=%s)",
            getattr(proposal, "id", "?"),
            variant,
            exc_info=True,
        )
        return None
