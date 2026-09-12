"""CLI entry point: ``python3 -m evolve_admin.evo.tools``.

Used by OpenClaw's MCP subprocess spawn — the openclaw.json
``mcp.servers.evo_tools`` entry points at this module.

Minimal wrapper around :func:`mcp_server.main`; kept tiny so the
process startup cost is dominated by the import graph rather than
unnecessary work.

``--manifest-size`` prints the toolset's context weight as JSON and
exits instead of serving MCP (overhead-budget B7 measurement seam —
the plugin's ToolFootprint cannot see MCP tools, so
``context_health --overhead`` shells out to this flag). Anything on
argv other than that flag is rejected loudly rather than silently
falling through to a stdio server that would then eat the caller's
terminal waiting on MCP framing.
"""

from __future__ import annotations

import sys


def _cli(argv: list[str]) -> int:
    if argv == ["--manifest-size"]:
        import json

        from .footprint import manifest_stats

        print(json.dumps(manifest_stats(), indent=1))
        return 0
    if argv:
        print(
            f"unknown arguments: {argv!r} — supported: --manifest-size "
            "(no args serves MCP on stdio)",
            file=sys.stderr,
        )
        return 2
    from .mcp_server import main

    main()
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
