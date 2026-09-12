"""investigation.proposal_history — Past-proposal lookups for generators.

Spec: internal/spec-smarter-generators-2026-05-28.md §"Standard
investigation toolkit".

Generators consult these from their investigate step to ask:
  * "Has this been proposed before? What did the operator decide?"
  * "Is the operator already rejecting proposals with this cause_key?"

A confirmed rejection is a strong signal to suppress a repeat: the
operator has already made the call, and re-emitting the same proposal
is noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Status values that count as "operator decided no" — the proposal
# reached a terminal state where the operator either dismissed it,
# rejected it, or the system flagged it failed.
_DECLINE_STATUSES: frozenset[str] = frozenset({
    "dismissed",
    "rejected",
    "failed_flagged",
    "failed_silent",
})

# Status values that count as "operator decided yes."
_APPROVE_STATUSES: frozenset[str] = frozenset({
    "approved",
    "approved_admin",
    "approved_auto",
    "applied",
    "succeeded",
})


@dataclass
class ProposalHistoryEntry:
    """One past proposal record relevant to the current investigation."""

    proposal_id: str
    bot_id: str
    generator_id: str
    status: str
    cause_key: str  # extracted from provenance.signals.root_cause_attribution.cause_key
    created_at: str
    motivating_signal_signatures: list[str]


@dataclass
class ProposalHistorySummary:
    """Aggregated view of past proposals for a (bot_id, cause_key)."""

    bot_id: str
    cause_key: str
    total: int = 0
    declined: int = 0
    approved: int = 0
    open: int = 0
    most_recent_status: str = ""
    most_recent_created_at: str = ""


def _read_cause_key(proposal_data: dict) -> str:
    """Pull cause_key out of provenance.signals.root_cause_attribution."""
    prov = proposal_data.get("provenance") or {}
    signals_blob = prov.get("signals") or {}
    rca = signals_blob.get("root_cause_attribution") or {}
    ck = rca.get("cause_key")
    return ck if isinstance(ck, str) else ""


def proposal_history(
    shared_dir: Path,
    *,
    bot_id: str,
    generator_id: str | None = None,
    cause_key: str | None = None,
    signal_signature: str | None = None,
    limit: int = 50,
) -> list[ProposalHistoryEntry]:
    """Return past proposals matching the filters, newest-first.

    Reads every proposal subdir (pending/snoozed/applied/archived).
    Filters are AND'd. Fail-open: ImportError or read errors return
    ``[]`` (better to under-report than crash).
    """
    try:
        from arbiter.store import iter_proposals
    except ImportError:
        return []
    out: list[ProposalHistoryEntry] = []
    try:
        for proposal in iter_proposals(
            shared_dir,
            subdirs=("pending", "snoozed", "applied", "archived"),
        ):
            if proposal.bot_id != bot_id:
                continue
            if generator_id is not None and proposal.generator_id != generator_id:
                continue
            data = proposal.to_dict() if hasattr(proposal, "to_dict") else {}
            ck = _read_cause_key(data)
            if cause_key is not None and ck != cause_key:
                continue
            sigs = list(getattr(proposal, "motivating_signals", []) or [])
            # Caller may pass a motivating signal signature; we filter on
            # the signature string presence in motivating_signals list.
            if signal_signature is not None and signal_signature not in sigs:
                continue
            out.append(
                ProposalHistoryEntry(
                    proposal_id=getattr(proposal, "id", ""),
                    bot_id=proposal.bot_id,
                    generator_id=proposal.generator_id,
                    status=getattr(proposal, "status", ""),
                    cause_key=ck,
                    created_at=getattr(proposal, "created_at", ""),
                    motivating_signal_signatures=sigs,
                )
            )
    except Exception:
        return out

    out.sort(key=lambda e: e.created_at, reverse=True)
    return out[:limit]


def summarize_history(
    entries: list[ProposalHistoryEntry],
    *,
    bot_id: str,
    cause_key: str,
) -> ProposalHistorySummary:
    """Roll up entries into a one-shot summary for generator decision logic."""
    summary = ProposalHistorySummary(bot_id=bot_id, cause_key=cause_key)
    summary.total = len(entries)
    if not entries:
        return summary
    summary.most_recent_status = entries[0].status
    summary.most_recent_created_at = entries[0].created_at
    for e in entries:
        if e.status in _DECLINE_STATUSES:
            summary.declined += 1
        elif e.status in _APPROVE_STATUSES:
            summary.approved += 1
        else:
            summary.open += 1
    return summary


def operator_already_declined(
    shared_dir: Path,
    *,
    bot_id: str,
    cause_key: str,
    min_recent_declines: int = 2,
) -> bool:
    """Convenience: True when operator has declined this cause N times.

    Useful guard for generators that want to fail closed on recurring
    rejection — re-emitting a proposal the operator has dismissed twice
    is noise. The generator should escalate (different message,
    investigation framing, or silence) instead.
    """
    entries = proposal_history(
        shared_dir, bot_id=bot_id, cause_key=cause_key, limit=20,
    )
    declined = sum(1 for e in entries if e.status in _DECLINE_STATUSES)
    return declined >= min_recent_declines
