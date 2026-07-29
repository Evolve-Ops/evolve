"""MCP server administration — inventory monitor + allowlist + signals.

Spec: docs/spec-mcp-administration-2026-05-10.md.

Phase A (this package): read-only inventory monitor that observes each
bot's mcp.servers configuration, compares against a pod-wide allowlist,
and emits signals on drift. No mutation; no install path; no health
probe. Phases B–D extend this with the catalog, appliers, probes, and
CVE feed.
"""
