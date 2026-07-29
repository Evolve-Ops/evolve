"""arbiter.snooze_wake — Transition expired snoozed proposals back to pending.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §3.6.

Invoked by a cron (launchd) every few minutes. Pure logic — no LLM, no
network — reads proposal JSON files in the ``snoozed/`` dir, checks
``snoozed_until``, and transitions the ones past their wake time.

The caller is responsible for persistence: this module mutates the
Proposal objects in place and returns them, letting the caller write
them back to disk or move files between status directories.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from arbiter.state_machine import (
    IllegalTransitionError,
    transition,
)
from schema.proposal import Proposal


@dataclass
class WakeResult:
    """Outcome of a single wake cycle."""

    woken: list[str] = field(default_factory=list)
    still_snoozed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (id, reason)


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def wake_expired(
    snoozed_proposals: Iterable[Proposal],
    *,
    now: datetime | None = None,
    actor: str = "snooze_wake",
) -> WakeResult:
    """Transition snoozed proposals whose wake time has passed back to pending.

    Args:
        snoozed_proposals: iterable of Proposal objects currently in the
            ``snoozed`` state.
        now: override the current time for tests. Defaults to
            ``datetime.now(timezone.utc)``.
        actor: the actor string recorded on the StatusTransition.

    Returns ``WakeResult`` with three lists:
      - ``woken``: IDs that were transitioned snoozed → pending
      - ``still_snoozed``: IDs whose wake time hasn't arrived
      - ``skipped``: (id, reason) pairs for proposals that couldn't be woken
        (e.g., missing snoozed_until, malformed timestamp, unexpected status)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    result = WakeResult()

    for proposal in snoozed_proposals:
        if proposal.status != "snoozed":
            result.skipped.append(
                (proposal.id, f"unexpected status {proposal.status!r}")
            )
            continue

        wake_at = _parse_iso(proposal.snoozed_until)
        if wake_at is None:
            result.skipped.append(
                (
                    proposal.id,
                    f"missing or malformed snoozed_until: {proposal.snoozed_until!r}",
                )
            )
            continue

        if wake_at > now:
            result.still_snoozed.append(proposal.id)
            continue

        # Time to wake
        try:
            transition(
                proposal,
                "pending",
                actor=actor,
                reason=f"snooze_until {proposal.snoozed_until} elapsed",
            )
        except IllegalTransitionError as e:
            result.skipped.append((proposal.id, f"illegal transition: {e}"))
            continue

        # Clear the snooze timestamp now that we've woken
        proposal.snoozed_until = None
        result.woken.append(proposal.id)

    return result
