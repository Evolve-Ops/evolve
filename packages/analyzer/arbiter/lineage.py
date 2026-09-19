"""arbiter.lineage — Proposal lineage by fingerprint.

When a generator emits a proposal that's already been seen before — same
fingerprint, different cycle — the operator should see that history at a
glance. "Previously dismissed twice" is decision-relevant context;
"previously approved and applied 6 weeks ago, but the issue returned" is
even more so. This is the recursive piece: the system remembers what was
done before, and surfaces it on the proposal card.

Distinct from the rejection log (which only tracks user-initiated
rejections within a cooldown window): lineage covers all terminal
outcomes — succeeded, failed_*, rejected, dismissed, superseded,
resolved_externally — across the full archive.

Performance: walking the archive is O(n) per request. The
``LineageIndex`` class caches the fingerprint→entries map so a request
that needs lineage for many proposals only walks the archive once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arbiter.dedup import compute_fingerprint
from arbiter.store import iter_proposals
from schema.proposal import Proposal


# Statuses we consider "terminal" — a proposal in any of these is
# done and contributes to lineage. Active states (pending/snoozed) are
# the present, not history.
_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "failed_reverted",
        "failed_flagged",
        "failed_revert_failed",
        "rejected",
        "dismissed",
        "superseded",
        "resolved_externally",
    }
)


@dataclass(frozen=True)
class LineageEntry:
    """One historical occurrence of a fingerprint."""

    proposal_id: str
    status: str
    terminal_at: str | None
    action_kind: str
    problem: str
    generator_id: str
    bot_id: str | None
    created_at: str

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status,
            "terminal_at": self.terminal_at,
            "action_kind": self.action_kind,
            "problem": self.problem,
            "generator_id": self.generator_id,
            "bot_id": self.bot_id,
            "created_at": self.created_at,
        }


def _terminal_at(proposal: Proposal) -> str | None:
    """Return the timestamp of the proposal's transition into a terminal
    status, or None if no terminal transition exists in history."""
    history = list(getattr(proposal, "history", []) or [])
    for entry in reversed(history):
        to_status = getattr(entry, "to_status", None) or (
            entry.get("to_status") if isinstance(entry, dict) else None
        )
        if to_status in _TERMINAL_STATUSES:
            ts = getattr(entry, "at", None) or (
                entry.get("at") if isinstance(entry, dict) else None
            )
            return ts
    return None


def _to_entry(proposal: Proposal) -> LineageEntry:
    return LineageEntry(
        proposal_id=proposal.id,
        status=proposal.status,
        terminal_at=_terminal_at(proposal),
        action_kind=getattr(proposal.action, "kind", type(proposal.action).__name__),
        problem=proposal.problem,
        generator_id=proposal.generator_id,
        bot_id=proposal.bot_id,
        created_at=getattr(proposal, "created_at", "") or "",
    )


class LineageIndex:
    """Fingerprint → list[LineageEntry] index for archived proposals.

    Build once per request, query as many times as needed. Entries within
    each fingerprint bucket are sorted newest-first by ``terminal_at``
    (falling back to ``created_at``).

    Usage::

        idx = LineageIndex.build(shared_dir)
        for proposal in pending:
            history = idx.lineage_for(proposal, max_entries=5)
    """

    def __init__(self, by_fingerprint: dict[str, list[LineageEntry]]):
        self._map = by_fingerprint

    @classmethod
    def build(cls, shared_dir: Path) -> "LineageIndex":
        by_fp: dict[str, list[LineageEntry]] = {}
        for proposal in iter_proposals(shared_dir, subdirs=("archived",)):
            if proposal.status not in _TERMINAL_STATUSES:
                # Defensive: archived/ should only contain terminal proposals,
                # but we don't trust the subdir alone (the spec says status
                # is authoritative).
                continue
            try:
                fp = compute_fingerprint(proposal)
            except Exception:
                continue
            by_fp.setdefault(fp, []).append(_to_entry(proposal))

        # Sort each bucket newest-first.
        for fp, entries in by_fp.items():
            entries.sort(
                key=lambda e: (e.terminal_at or e.created_at or ""),
                reverse=True,
            )
        return cls(by_fp)

    def lineage_for(
        self,
        proposal: Proposal,
        *,
        max_entries: int = 5,
        exclude_id: str | None = None,
    ) -> list[LineageEntry]:
        """Return up to ``max_entries`` historical entries for the given
        proposal's fingerprint. ``exclude_id`` lets the caller skip the
        proposal itself if it happens to live in the archive (e.g. when
        rendering lineage for an already-archived proposal)."""
        try:
            fp = compute_fingerprint(proposal)
        except Exception:
            return []
        entries = self._map.get(fp, [])
        if exclude_id is None:
            return entries[:max_entries]
        return [e for e in entries if e.proposal_id != exclude_id][:max_entries]

    def lineage_for_id(
        self,
        fingerprint: str,
        *,
        max_entries: int = 5,
        exclude_id: str | None = None,
    ) -> list[LineageEntry]:
        """Same as :meth:`lineage_for` but takes a precomputed fingerprint."""
        entries = self._map.get(fingerprint, [])
        if exclude_id is None:
            return entries[:max_entries]
        return [e for e in entries if e.proposal_id != exclude_id][:max_entries]
