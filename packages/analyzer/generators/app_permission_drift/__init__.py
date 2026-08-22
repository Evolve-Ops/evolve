"""generators.app_permission_drift — manifest/exec-approvals drift proposal author.

Consumes ``app_permission_drift`` Signals from the
``app_manifest_monitor`` producer and emits per-finding Proposals:

  - ``UpdateExecApproval(add)`` for declared-but-not-allowed entries
  - ``UpdateExecApproval(revoke)`` for stale app-derived entries
  - ``Investigation`` for workspace-orphan-script + declared-missing-file
    findings (operator-driven; the generator doesn't mutate manifests
    directly — that's B.2's territory)

Mirrors the auth_drift_filler shape but inverts the intent: where
auth_drift_filler pushes a bot back toward a security baseline regardless
of intent, this generator pushes toward correctness *as declared by app
manifests* — the bot's expressed intent.

Spec: docs/spec-app-permission-drift-2026-05-25.md (B.1 implementation of
spec-app-derived-permissions-2026-05-24.md §4).
"""

from generators.app_permission_drift.observe import (
    AppPermissionDriftContext,
    observe,
)

__all__ = ["AppPermissionDriftContext", "observe"]
