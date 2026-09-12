"""generators.cache_ttl_tuner — Anthropic prompt-cache retention tuner.

Consumes Signals from the ``session_economics`` monitor:

  * ``cache_invalidation_elevated`` → autonomous-fix UpdateAgentDefaults
    Proposal flipping ``agents.defaults.models.*.params.cacheRetention``
    from ``"short"`` (5min, Anthropic default) to ``"long"`` (1h),
    closing the loop on the team-bot-a session 3d5cde22 incident
    (2026-05-29 Slack DM, $7.67, 92% prompt-cache writes). Eligibility
    resolves to ``tier_floor=auto-small`` via PR B (#1874) — per-bot,
    single enum knob, fully reversible.

  * ``cache_hit_rate_low`` → Investigation Proposal pointing at
    prompt-structure churn (no autonomous fix; the operator does the
    legwork).

Companion to ``efficiency_hawk``: same dimension (``efficiency``),
same audience (``pod_operator``), distinct subscription
(session_economics vs cost_watchdog) and distinct corner of the cost
story.
"""

from generators.cache_ttl_tuner.observe import (
    CacheTtlTunerContext,
    observe,
)

__all__ = ["CacheTtlTunerContext", "observe"]
