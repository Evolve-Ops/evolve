"""generators.engagement_amplifier — Pattern-amplification proposer.

Spec: internal/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".

Consumes ``engagement_amplification_opportunity`` Signals from the
engagement_amplifier_monitor and emits Phase A operator-first
Proposals on ``surface=improvement``. The proposal narrative
distinguishes ``confirmed`` patterns (already in the bot's stated
scope; "deepen what's working") from ``emergent`` patterns (users
have organically converged; "consider whether to embrace this").

The companion to ``app_suggester``:
  - ``app_suggester``        — capability gaps (missing functionality)
  - ``engagement_amplifier`` — capability amplification (working patterns)

Together they cover both directions of objective-aware RSI on the
Recommendations page.
"""

from generators.engagement_amplifier.observe import (
    EngagementAmplifierContext,
    observe,
)

__all__ = ["EngagementAmplifierContext", "observe"]
