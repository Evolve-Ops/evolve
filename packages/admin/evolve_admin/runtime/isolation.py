"""Compatibility shim — the IsolationProvider seam moved to the analyzer package.

The real module is ``runtime.isolation`` (packages/analyzer/runtime/,
beside the AgentRuntime seam); it moved there so analyzer-side S2
migrations never import ``evolve_admin`` — admin depends on analyzer,
never the reverse (Phase 6.1 packaging direction).

This shim re-exports the public surface so every existing
``evolve_admin.runtime`` import keeps working. It holds NO state: the
``get_isolation``/``set_isolation`` singleton lives in the moved module,
so injection through either import path hits the same adapter.
"""

from runtime.isolation import (  # noqa: F401
    DEFAULT_UID_START,
    EVOLVE_BOTS_GROUP,
    LINUX_DEFAULT_UID_START,
    SYSTEM_ACCOUNTS,
    Account,
    FakeIsolation,
    Identity,
    IsolationError,
    IsolationProvider,
    LinuxUserIsolation,
    MacOSIsolation,
    get_isolation,
    set_isolation,
)
