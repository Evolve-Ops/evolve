"""generators.session_quality — Maintenance-ratio responder.

Consumes ``session_quality`` Signals from the ``cost_watchdog`` monitor
and emits one Investigation Proposal per firing signal. Each Proposal
surfaces the trailing-window maintenance ratio and points at the
typical causes (stuck loop, auth errors, scanner thrashing); the
operator decides what to do.

Migration note: replaces the session_quality branch in
ScoreboardAdapter, which detected the same condition and emitted a
Better Engine native rec directly. The Proposal-backed path inherits
the standard arbiter lifecycle and ``motivating_signals[]``
traceability.
"""

from generators.session_quality.observe import (
    SessionQualityContext,
    observe,
)

__all__ = ["SessionQualityContext", "observe"]
