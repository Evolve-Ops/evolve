"""generators.exec_outcome_investigator — Investigate bot exec-outcome gaps.

Spec: docs/spec-exec-outcome-watchdog-2026-05-28.md.

Second reference implementation of the investigate-before-propose
pattern. Consumes the four Signal types from exec_outcome_watchdog
(tool_error_burst, exec_denied, approval_timeout, preflight_block),
runs attribution rules, emits one Investigation Proposal per
investigated bot with a root_cause_attribution block in
Provenance.signals.

Mirrors bloat_investigator's shape — different evidence inputs, same
toolkit reuse + same attribution rule pattern.
"""

from generators.exec_outcome_investigator.observe import (
    ExecOutcomeInvestigatorContext,
    observe,
)


__all__ = ["ExecOutcomeInvestigatorContext", "observe"]
