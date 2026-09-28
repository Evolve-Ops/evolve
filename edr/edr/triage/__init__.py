"""edr.triage — EDR triage generators (Signals → routed Proposals).

The coordinator tier's first function (design-edr-2026-06-11.md §6;
tiered-oversight Move 1): per incoming dev Signal, write a typed,
machine-readable Proposal through the real imported ``arbiter.store`` —
classification, severity, agent-able-vs-human (DEFAULTS TO HUMAN),
target aspect, and the ``motivating_signals[]`` linkage.

Modules:
  - ``dev_issue`` — triage for ``dev_issue`` Signals (P1.2). Its charter
    lives beside it as EDR-local data (``charter.yaml``) for the future
    event loop (design Q11) — deliberately NOT registered under
    ``packages/analyzer/generators/`` (that tree ships to pods; guard G1).

CLI: ``python -m edr.triage --shared-dir <dir>`` (see ``__main__``).
"""

from edr.triage import dev_issue

__all__ = ["dev_issue"]
