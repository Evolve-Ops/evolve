"""arbiter.dedup — Fingerprint similarity check (L1: pass-through).

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §5.3.

In L1, this module computes a stable fingerprint for every incoming proposal
and records it, but performs no actual deduplication. The L3 merge judge
fills in the collision-handling logic when multiple generators first exist.

Shipping the hook in L1 (as a pass-through that at least records the
fingerprint) means L2+ can fill in the collision logic without touching
arbiter.ingest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

from schema.proposal import Proposal


@dataclass
class DedupResult:
    """Outcome of the dedup hook for a single proposal."""

    proposal_id: str
    fingerprint: str
    collisions: list[str]  # IDs of open proposals with matching fingerprint
    merged: bool = False  # L3+ will flip this when the merge judge fires


def _stable_string(parts: Iterable[str]) -> str:
    """Join parts with '|' after sorting for order-independence."""
    return "|".join(sorted(p for p in parts if p))


def compute_fingerprint(proposal: Proposal) -> str:
    """Compute a stable fingerprint for similarity matching.

    Fingerprint inputs (spec §5.3):
      - trigger_observations (intersection sorted)
      - action.kind
      - action target surface (derived from action fields)
      - target metric (from claim, if any)

    Returns a hex SHA-256 digest.
    """
    triggers = _stable_string(proposal.trigger_observations)

    action = proposal.action
    action_kind = getattr(action, "kind", type(action).__name__)

    # Derive a "target surface" string from action. This is best-effort;
    # for unknown action shapes we fall back to the kind alone.
    target_surface = ""
    if hasattr(action, "app_id"):
        target_surface = f"app:{getattr(action, 'app_id')}"
    elif hasattr(action, "target_path"):
        target_surface = f"path:{getattr(action, 'target_path')}"
    elif hasattr(action, "path"):
        target_surface = f"path:{getattr(action, 'path')}"
    elif hasattr(action, "generator_id"):
        target_surface = f"generator:{getattr(action, 'generator_id')}"
    elif hasattr(action, "bot_id"):
        target_surface = f"bot:{getattr(action, 'bot_id')}"

    target_metric = proposal.claim.metric if proposal.claim else ""

    fingerprint_input = f"{proposal.bot_id}::{action_kind}::{target_surface}::{target_metric}::{triggers}"
    return hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()


def find_collisions(
    fingerprint: str,
    open_proposals: Iterable[Proposal],
    *,
    exclude_id: str | None = None,
) -> list[str]:
    """Return IDs of open proposals with matching fingerprint.

    ``open_proposals`` should be the set of proposals in pending/snoozed/applied
    states — states where merging or deduping is still meaningful.
    """
    matches: list[str] = []
    for other in open_proposals:
        if exclude_id is not None and other.id == exclude_id:
            continue
        other_fp = compute_fingerprint(other)
        if other_fp == fingerprint:
            matches.append(other.id)
    return matches


def run_dedup_hook(
    proposal: Proposal,
    open_proposals: Iterable[Proposal] | None = None,
) -> DedupResult:
    """Run the L1 dedup hook against a proposal.

    L1 behavior: compute the fingerprint, record collisions, pass through.
    The L2+ merge judge fills in collision handling later.

    Returns the fingerprint and the list of collision IDs. Does not modify
    the proposal's status; the ingest module decides what to do with the
    result (L1: nothing beyond logging).
    """
    fingerprint = compute_fingerprint(proposal)
    collisions: list[str] = []
    if open_proposals is not None:
        collisions = find_collisions(
            fingerprint, open_proposals, exclude_id=proposal.id
        )
    return DedupResult(
        proposal_id=proposal.id,
        fingerprint=fingerprint,
        collisions=collisions,
        merged=False,
    )
