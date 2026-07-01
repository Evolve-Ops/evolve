"""Plugin inventory monitor + baseline + signals.

Spec: docs/spec-plugin-inventory-2026-05-10.md.

Phase A (this package): observation-only drift monitor that compares
each bot's ``plugins.entries`` / ``plugins.allow`` / ``plugins.load.paths``
/ ``plugins.installs`` against a pod-curated baseline and emits signals
on divergence. Phase B will add the curator generator that produces
``UpdatePluginAllowDeny`` proposals to close the bulk-allow-list gap
(5 of 6 deployed bots lack a ``plugins.allow`` list today). Phase C
adds marketplace install handling.

Module name is ``plugins`` (not ``mcp_plugins`` or similar) because
this is the OpenClaw first-party plugin system — the colloquial
"plugin" in the Claude Code community. MCP servers are a separate
mechanism handled in ``packages/analyzer/mcp_admin/``.
"""
