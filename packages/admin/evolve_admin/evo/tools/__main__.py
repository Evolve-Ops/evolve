"""CLI entry point: ``python3 -m evolve_admin.evo.tools``.

Used by OpenClaw's MCP subprocess spawn — the openclaw.json
``mcp.servers.evo_tools`` entry points at this module.

Minimal wrapper around :func:`mcp_server.main`; kept tiny so the
process startup cost is dominated by the import graph rather than
unnecessary work.
"""

from __future__ import annotations

from .mcp_server import main


if __name__ == "__main__":
    main()
