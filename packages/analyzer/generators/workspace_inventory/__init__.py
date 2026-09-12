"""generators.workspace_inventory — Unregistered scripts + crons.

Consumes ``unregistered_script`` and ``unregistered_cron`` Signals
from the ``compliance_scan`` monitor and emits Investigation
Proposals telling the operator about workspace assets that exist
without a manifest. Companion generator to manifest_quality
(manifest-content issues) and workspace_security (credentials).
"""

from generators.workspace_inventory.observe import (
    WorkspaceInventoryContext,
    observe,
)

__all__ = ["WorkspaceInventoryContext", "observe"]
