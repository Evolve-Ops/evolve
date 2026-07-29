"""generators.cron_caps_filler — Cron-job caps proposal author.

Consumes ``perm_cron_uncapped_agent_turn`` Signals from the
``permission_monitor`` producer and emits ``UpsertCronJob`` Proposals
that add the missing ``maxTurns`` + ``maxBudgetUsd`` caps to each
affected job. Same applier the admin UI's "Add caps" button drives,
same defaults (maxTurns=20, maxBudgetUsd=1.00) — so the proposal
queue stays consistent with the manual path.

Closes the §13 resolver-pattern gap that surfaced cron caps as the
first case where evo identified a problem in chat but had no matching
proposal to apply. With this generator, the signal-to-proposal link
exists; evo discovers the proposal via ``pod_state.proposals.pending``
and applies it via ``action.proposal.apply``.

Companion to ``permission_monitor`` (which writes the Signals) and
the ``UpsertCronJobApplier`` in ``arbiter.appliers.permissions``
(which validates + writes the resulting config). This generator
sits in the middle and produces the Proposal object.
"""

from generators.cron_caps_filler.observe import (
    CronCapsFillerContext,
    observe,
)

__all__ = ["CronCapsFillerContext", "observe"]
