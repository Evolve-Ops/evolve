"""generators.workspace_security — Misplaced-credential responder.

Consumes ``misplaced_secret`` Signals from the ``compliance_scan``
monitor and emits Investigation Proposals so the operator can review
credentials found in workspace files. Severity skews critical; the
Proposal is the surface for "look at this and decide whether to
rotate, redact, or accept" — no autonomous remediation.
"""

from generators.workspace_security.observe import (
    WorkspaceSecurityContext,
    observe,
)

__all__ = ["WorkspaceSecurityContext", "observe"]
