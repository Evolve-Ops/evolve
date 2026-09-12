"""generators.bloat_investigator — Reference investigate-before-propose generator.

Spec: internal/spec-smarter-generators-2026-05-28.md (Phase 2).

Consumes the envelope-growth Signal family from cost_watchdog
(context_bloat, workspace_growth, cache_envelope_growth,
efficiency_drift), runs pure-Python attribution rules, and emits
Investigation Proposals carrying a root_cause_attribution payload in
Provenance.signals.

This is the *reference* implementation of the pattern. New investigating
generators should mirror this shape: gather correlated evidence via
``investigation.toolkit``, run an ordered list of attribution rules,
fall through to "ambiguous" when no rule matches.
"""

from generators.bloat_investigator.observe import (
    BloatInvestigatorContext,
    observe,
)


__all__ = ["BloatInvestigatorContext", "observe"]
