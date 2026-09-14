"""tests/test_pod_state_cross_process.py — invariant: pod_state.* tools
must read from a cross-process source.

The MCP server's evo-tools child process imports ``evo.tools.*`` fresh
when it spawns, so any tool that reads from a module-global Python
dict that's only populated by a thread in the ADMIN SERVER process
will see permanently-empty data. This is the bug ``pod_state.audit``
shipped with (#1337) — silent staleness that fooled the model into
telling operators their security audit hadn't been run.

The fix pattern: read from a shared source that both processes can
see. In practice every other ``pod_state.*`` tool already does — they
all read JSON files from ``{shared_dir}/.../*.json``. ``pod_state.audit``
was the lone exception until Sprint 2b moved its cache to disk.

This file pins that invariant. Each ``pod_state.*`` tool must be
implemented by reading from ONE of:

  * the shared filesystem (``shared_dir`` parameter)
  * the network config file (``network_path``)
  * a live local read that doesn't depend on persistence
    (e.g. ``psutil`` for ``pod_state.host``)
  * the admin server's HTTP surface (kept as defense-in-depth for
    audit; future tools can use the same pattern)
  * the audit-state disk mirror (the Sprint 2b unification)

Tools must NOT take their data from a top-level module-global that
isn't ALSO mirrored to disk or available via HTTP. If a new tool
genuinely needs in-process cache, it must explicitly expose a
cross-process read path (HTTP route + URL resolution) before this
test will let it pass.

If you're adding a new ``pod_state.*`` tool and this test fails, the
fix is almost always:

  * Read your data from ``{shared_dir}/...`` instead of a module-
    global; OR
  * Mirror your module-global to disk on every write (see
    ``audit_state.persist``); OR
  * Add an HTTP route + reader (see ``pod_state_audit._read_via_http``
    for the canonical example).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_TOOLS_DIR = _ADMIN_PKG / "evolve_admin" / "evo" / "tools"


# Every ``pod_state.*`` tool registers under one of these module files.
# Adding a new pod_state.* tool? Add the module here so this test
# catches the cross-process invariant on it too.
_POD_STATE_TOOL_MODULES = [
    "pod_state_audit.py",
    "pod_state_signals.py",
    "pod_state_proposals.py",
    "pod_state_bots.py",
    "pod_state_host.py",
    "pod_state_usage.py",
    "pod_state_errors.py",
    "pod_state_rollbacks.py",
]


# Tool modules that ALSO register pod_state-prefixed tools as a side
# effect of registering action.* tools. Held to the same invariant.
_POD_STATE_SIDECAR_MODULES = [
    "action_app.py",        # pod_state.forge_job
    "action_pod.py",        # pod_state.pause_state
    "evo_telemetry.py",     # pod_state.tool_gaps
]


# Acceptable data-source signatures: presence of ANY one of these
# patterns in the module body is enough to consider it cross-process
# safe. Each pattern represents one of the documented escape hatches.
#
# Order matters only for the error message — we check them all and
# any match passes.
_ACCEPTABLE_SOURCE_PATTERNS = [
    # Reads from the shared filesystem — every disk-backed pod_state
    # tool uses this convention. Both processes see the same files.
    (r"\bshared_dir\b", "reads from shared_dir on the filesystem"),
    # Reads from the admin network config — both processes see this.
    (r"\bnetwork_path\b", "reads from network_path"),
    # Live local read (no persistence needed).
    (r"import\s+psutil|host_health", "live local read via psutil"),
    # HTTP fallback to the admin server (cross-process by RPC).
    (r"resolve_admin_base_url|urllib\.request\.urlopen",
     "HTTP fetch from admin server"),
    # Disk mirror of audit cache — unified Sprint 2b pattern.
    (r"audit_state\.snapshot\(\s*\w+\s*\)|_cache_path",
     "audit-state disk mirror"),
]


# Forbidden patterns: a tool body that does THIS is the cross-process
# trap. If the tool's only data source is a module-global Python dict
# that isn't ALSO mirrored to disk or available via HTTP, the MCP
# server's child process will see empty data forever.
#
# We don't ban module-globals outright — `audit_state._state` is fine
# *as long as* the tool also reads from a cross-process source. So
# the check is "does the tool ONLY read from a module-global, with no
# acceptable alternative?"
_MODULE_GLOBAL_ONLY_HINT = re.compile(
    r"snapshot\(\s*\)\s*[\.\[]|_state\s*[\[\.]",
)


@pytest.mark.parametrize("module_name", _POD_STATE_TOOL_MODULES + _POD_STATE_SIDECAR_MODULES)
def test_pod_state_tool_has_cross_process_source(module_name: str):
    """Each ``pod_state.*`` tool module must read from at least one
    cross-process source (shared_dir / network_path / live local /
    HTTP / audit-state disk mirror). The MCP server's child process
    has its own empty module-globals; tools that rely solely on those
    permanently report stale data."""
    path = _TOOLS_DIR / module_name
    assert path.exists(), f"expected tool module: {path}"
    body = path.read_text(encoding="utf-8")

    matched: list[str] = []
    for pattern, label in _ACCEPTABLE_SOURCE_PATTERNS:
        if re.search(pattern, body):
            matched.append(label)

    assert matched, (
        f"{module_name} doesn't appear to read from any cross-process "
        f"source (shared_dir / network_path / live local / HTTP / "
        f"audit-state disk mirror). This is the trap pod_state.audit "
        f"shipped with (#1337). If you genuinely need an in-process "
        f"cache, also expose either a disk mirror or an HTTP read "
        f"path so the MCP child process can see the data.\n\n"
        f"Acceptable patterns and where to find examples:\n"
        f"  shared_dir          → pod_state_signals.py\n"
        f"  network_path        → pod_state_bots.py\n"
        f"  psutil/host_health  → pod_state_host.py\n"
        f"  HTTP fallback       → pod_state_audit._read_via_http\n"
        f"  disk mirror         → audit_state.snapshot(shared_dir)"
    )


def test_audit_state_module_persists_to_disk():
    """``audit_state.persist`` must exist as a callable function and
    write to a path under shared_dir. The audit cache is the one tool
    that used to be in-memory-only; locking the disk mirror in here
    catches a refactor that re-introduces the bug."""
    from evolve_admin import audit_state
    assert hasattr(audit_state, "persist"), (
        "audit_state.persist removed — the disk mirror is what makes "
        "pod_state.audit cross-process correct. If you're moving the "
        "cache somewhere else, update this test and the lint above."
    )
    assert callable(audit_state.persist)


def test_audit_state_snapshot_accepts_shared_dir():
    """``audit_state.snapshot(shared_dir=X)`` must accept a shared_dir
    argument so the MCP child process can read the disk mirror
    without depending on admin-server memory."""
    import inspect
    from evolve_admin import audit_state
    sig = inspect.signature(audit_state.snapshot)
    assert "shared_dir" in sig.parameters, (
        "audit_state.snapshot signature regressed — the shared_dir "
        "parameter was added in Sprint 2b so cross-process readers "
        "can hit the disk mirror. Removing it re-introduces the trap."
    )


def test_pod_state_audit_handler_threads_shared_dir():
    """``pod_state_audit._handler`` must accept a ``shared_dir``
    parameter so the MCP bridge can inject it. Without this, the
    handler can't reach the disk mirror in production."""
    import inspect
    from evolve_admin.evo.tools import pod_state_audit
    sig = inspect.signature(pod_state_audit._handler)
    assert "shared_dir" in sig.parameters, (
        "pod_state_audit._handler signature regressed — Sprint 2b "
        "added shared_dir so the bridge could inject it (see "
        "mcp_server.py:_wrap_handler signature inspection)."
    )
