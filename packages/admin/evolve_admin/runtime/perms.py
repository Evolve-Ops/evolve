"""Compatibility shim — the Perms seam lives in the analyzer package.

The real module is ``runtime.perms`` (packages/analyzer/runtime/, beside
the Scheduler / Isolation seams); it lives there so analyzer-side callers
never import ``evolve_admin`` — admin depends on analyzer, never the
reverse (Phase 6.1 packaging direction).

This shim holds NO state: the ``get_perms``/``set_perms`` singleton lives
in the moved module, so injection through either import path hits the
same adapter.
"""

from runtime.perms import (  # noqa: F401
    GETFACL,
    POD_READ_ACL_PERMS,
    SETFACL,
    FakePerms,
    LinuxPerms,
    MacOSPerms,
    Perms,
    get_perms,
    set_perms,
)
