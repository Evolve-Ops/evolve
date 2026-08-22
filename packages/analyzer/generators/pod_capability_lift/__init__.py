"""generators.pod_capability_lift — Cross-bot pattern aggregator.

Fourth Phase 2 RSI substrate piece per
docs/spec-rsi-proposal-eligibility-2026-06-05.md.

Reads firing per-bot Signals from the two pattern monitors:

  - ``app_suggester_gap``                     (capability_gap_monitor)
  - ``engagement_amplification_opportunity``  (engagement_amplifier_monitor)

When N >= MIN_BOTS_FOR_LIFT bots share the same gap or pattern, emits
ONE pod-wide Investigation Proposal that synthesizes the cross-bot
evidence and proposes pod-wide capability work. Coexists with the
per-bot proposals from the underlying generators — different
audiences (per-bot = "should this bot have it?"; pod-wide = "should
the whole pod?").

Pure consumer of existing producers — no new monitor needed. Pure
Python, no LLM. Same cost posture as the rest of the Phase 2 work.
"""

from generators.pod_capability_lift.observe import (
    PodCapabilityLiftContext,
    observe,
)

__all__ = ["PodCapabilityLiftContext", "observe"]
