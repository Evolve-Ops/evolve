"""edr.bridge — the Claude Code actuator bridge (design §5).

The interface between *an approved EDR work-item* and *a dispatched
Claude Code session that produces a reviewable PR*. This package is the
novel core of EDR (design: docs/edr/design-edr-2026-06-11.md §5) and is
built in two halves:

  - **P1.3a (this code): lifecycle + brief.** The human approval gate
    (``lifecycle.approve`` — the R1 rung: a human opts every dispatch
    in), the dispatch-eligibility filter
    (``lifecycle.eligible_for_dispatch``), and the pure brief renderer
    (``brief.brief_for``) that produces the SELF-CONTAINED instruction
    text a headless worker session runs from. No dispatching happens
    here — nothing in this half launches a session.
  - **P1.3b (next bite): dispatch + result.** Launches the headless
    session in an isolated worktree from an approved Proposal, then
    lands the typed result on the Proposal
    (``provenance.signals["actuator"]``; status → applied) and checks
    the G3 never-merge contract was honored.

Trust spine (design §7, non-negotiable): the worker session is
read-only by default and its ONLY mutation channel is an open PR;
**never merge, never ``--auto``** (G3 — contractual in the brief in
v1, verified by P1.3b); every work item closes only against its
declared falsifiable proof artifact (§5.2).

CLI: ``python -m edr.bridge {list,approve,brief}`` (see ``__main__``).
"""

from edr.bridge import brief, lifecycle

__all__ = ["brief", "lifecycle"]
