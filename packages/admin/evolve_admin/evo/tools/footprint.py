"""Manifest-size measurement for the evo MCP toolset (overhead-budget B7).

The evo MCP server's tool list rides in every primary-bot prompt via OC's
MCP integration, and it is invisible to the plugin's boot-time
ToolFootprint (which wraps only ``api.registerTool`` — MCP tools never
pass through it). This module is the measurement seam on the Python side:
it renders the registry exactly as the MCP bridge serves it (sanitized
names, description tool-name rewrites) and reports the char/token weight,
so ``context_health --overhead`` can attribute the MCP share of the
prefix instead of reporting it as unexplained.

CLI: ``python3 -m evolve_admin.evo.tools --manifest-size`` prints the
stats as JSON (see ``__main__.py``). Output field names deliberately
mirror the plugin's ``context-footprint.json`` (``tools: [{name,
chars}]``, ``total_chars``, ``tool_count``) so downstream readers can
treat the two surfaces uniformly.

Spec: docs/spec-evolve-overhead-budget-2026-07-31.md (B7).
"""

from __future__ import annotations

import json
from typing import Any

from . import all_tools

# Same estimate convention as context_health.BYTES_PER_TOKEN — an
# estimate, labeled as such wherever it is rendered.
BYTES_PER_TOKEN = 4


def family_of(name: str) -> str:
    """Grouping key for the per-family rollup: the first two dotted
    segments (``action.bot.restart`` → ``action.bot``), or the whole
    name for undotted tools (``evolve_help_search``)."""
    parts = name.split(".")
    return ".".join(parts[:2]) if len(parts) > 1 else name


def manifest_stats() -> dict[str, Any]:
    """Measure the ADVERTISED surface as served over MCP.

    Post-B7-Phase-2 (the facade consolidation), what rides the model's
    tools array is the 12-facade + standalones surface from
    ``facades.advertised_manifest()`` — so ``tools`` rows and
    ``total_chars`` measure THAT, in as-served form (sanitized ``__``
    names, rewritten descriptions). ``total_chars_canonical`` still
    measures the full fine-grained registry (dotted names, raw teaching
    descriptions): it is the on-demand corpus behind ``meta.tools``, and
    specs/docs quote that figure. ``registry_tool_count`` keeps the 88-ish
    registry size visible beside the advertised ``tool_count``.
    """
    from . import facades as _facades
    from .mcp_server import _sanitize_description, _sanitize_name

    tools = all_tools()
    canonical_names = tuple(t.name for t in tools)

    total_canonical = 0
    for t in tools:
        total_canonical += len(json.dumps({
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }))

    rows: list[dict[str, Any]] = []
    families: dict[str, dict[str, int]] = {}
    for entry in _facades.advertised_manifest():
        chars = len(json.dumps({
            "name": _sanitize_name(entry["name"]),
            "description": _sanitize_description(entry["description"], canonical_names),
            "input_schema": entry["input_schema"],
        }))
        rows.append({"name": entry["name"], "chars": chars})
        fam = families.setdefault(
            family_of(entry["name"]), {"tool_count": 0, "chars": 0})
        fam["tool_count"] += 1
        fam["chars"] += chars

    rows.sort(key=lambda r: -r["chars"])
    total = sum(r["chars"] for r in rows)
    return {
        "server": "evo_tools",
        "tool_count": len(rows),
        "registry_tool_count": len(tools),
        "total_chars": total,
        "total_chars_canonical": total_canonical,
        "est_tokens": total // BYTES_PER_TOKEN,
        "tools": rows,
        "families": [
            {"family": name, **counts}
            for name, counts in sorted(families.items(), key=lambda kv: -kv[1]["chars"])
        ],
    }
