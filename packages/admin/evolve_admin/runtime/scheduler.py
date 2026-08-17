"""Compatibility shim — the Scheduler seam moved to the analyzer package.

The real module is ``runtime.scheduler`` (packages/analyzer/runtime/,
beside the AgentRuntime seam); it moved there so analyzer-side S2
migrations (heal.py, oc_cli.py, …) never import ``evolve_admin`` — admin
depends on analyzer, never the reverse (Phase 6.1 packaging direction).

This shim re-exports the public surface so every existing
``evolve_admin.runtime`` import keeps working. It holds NO state: the
``get_scheduler``/``set_scheduler`` singleton lives in the moved module,
so injection through either import path hits the same adapter.
"""

# Tests patch ``scheduler_mod.subprocess.run`` to forbid real spawns —
# keep the module attribute available through the shim (it is the same
# stdlib module object the moved code calls).
import subprocess  # noqa: F401

from runtime.scheduler import (  # noqa: F401
    FakeScheduler,
    InstallResult,
    JobSpec,
    LaunchctlRunner,
    LaunchdScheduler,
    Scheduler,
    SystemdScheduler,
    get_launchd_scheduler,
    get_scheduler,
    is_sudo_escalation_error,
    label_from_unit_name,
    render_launchd_plist,
    render_systemd_units,
    set_scheduler,
    systemd_unit_name,
)
