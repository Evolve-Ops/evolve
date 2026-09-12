"""
evolve_admin.mcp_bridge — MCP Bridge server for Claude Desktop integration.

Exposes the pod's live context (all bot workspaces, memory, tasks, proposals)
as MCP tools. Claude Desktop connects to this bridge over Tailscale (or localhost)
and gains full pod awareness from session start.

Entry point: python -m evolve_admin.mcp_bridge [--network /path/network.json] [--port 5051]
"""
