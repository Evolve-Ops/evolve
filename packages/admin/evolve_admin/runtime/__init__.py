"""evolve_admin.runtime — compatibility shim for the process/scheduler seam.

The seam modules moved to the analyzer package (``runtime.scheduler`` /
``runtime.isolation``, beside ``runtime.agent_runtime``) in Phase 4.3 C
S2b0 so analyzer-side migrations never import ``evolve_admin`` — admin
depends on analyzer, never the reverse. This package re-exports the
exact public surface it always had; the singleton state lives in the
moved modules only (the ``.scheduler`` / ``.isolation`` submodules here
are themselves pure re-export shims).
"""

from .scheduler import (
    FakeScheduler,
    InstallResult,
    JobSpec,
    LaunchdScheduler,
    Scheduler,
    SystemdScheduler,
    get_launchd_scheduler,
    get_scheduler,
    is_sudo_escalation_error,
    render_launchd_plist,
    render_systemd_units,
    set_scheduler,
)
from .isolation import Identity, IsolationError, IsolationProvider, get_isolation, set_isolation  # noqa: F401  (Track I seam re-export)
from .perms import FakePerms, LinuxPerms, MacOSPerms, Perms, get_perms, set_perms  # noqa: F401  (W4a ACL seam re-export)

__all__ = [
    "JobSpec", "render_launchd_plist", "render_systemd_units",
    "Scheduler", "LaunchdScheduler", "InstallResult",
    "FakeScheduler", "SystemdScheduler",
    "get_scheduler", "set_scheduler", "get_launchd_scheduler",
    "is_sudo_escalation_error",
    "Identity", "IsolationError", "IsolationProvider", "get_isolation", "set_isolation",
    "Perms", "MacOSPerms", "LinuxPerms", "FakePerms", "get_perms", "set_perms",
]
