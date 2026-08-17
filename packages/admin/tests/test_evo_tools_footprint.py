"""Tests for the evo MCP toolset measurement seam (overhead-budget B7).

Pins:
  1. ``manifest_stats()`` measures the AS-SERVED manifest (sanitized
     names, description rewrites) and its totals reconcile with the
     per-tool and per-family rows.
  2. The ``--manifest-size`` CLI flag prints the stats as JSON and does
     NOT start the MCP stdio server; unknown args are rejected loudly
     instead of falling through to a stdio server.
"""

from __future__ import annotations

import json

from evolve_admin.evo.tools import all_tools
from evolve_admin.evo.tools.footprint import family_of, manifest_stats


def test_manifest_stats_reconciles_with_registry() -> None:
    stats = manifest_stats()
    tools = all_tools()

    assert stats["server"] == "evo_tools"
    # Post-Phase-2 (the facade consolidation), the ADVERTISED surface is
    # what rides the model's tools array: 12 facades + the standalones —
    # far fewer entries than the fine-grained registry, which stays intact
    # behind them (registry_tool_count keeps it visible).
    from evolve_admin.evo.tools import facades as _facades

    expected_advertised = len(_facades.FACADES) + len(_facades.STANDALONE_TOOLS)
    assert stats["tool_count"] == expected_advertised
    assert stats["registry_tool_count"] == len(tools)
    assert stats["tool_count"] < stats["registry_tool_count"]
    assert stats["tool_count"] == len(stats["tools"])

    # Totals are the sum of their parts — per-tool and per-family.
    assert stats["total_chars"] == sum(r["chars"] for r in stats["tools"])
    assert stats["total_chars"] == sum(f["chars"] for f in stats["families"])
    assert stats["tool_count"] == sum(f["tool_count"] for f in stats["families"])
    assert stats["est_tokens"] == stats["total_chars"] // 4

    # Post-Phase-2 the as-served manifest is the consolidated facade
    # surface while canonical is the full fine-grained registry teaching
    # corpus (the meta.tools detail sink) — the gap between them IS the
    # diet. Both stay plausibly non-trivial.
    assert 10_000 < stats["total_chars"] < stats["total_chars_canonical"]

    # Rows are sorted heaviest-first so operators see the offenders.
    chars = [r["chars"] for r in stats["tools"]]
    assert chars == sorted(chars, reverse=True)


def test_family_of_groups_by_first_two_segments() -> None:
    assert family_of("action.bot.pin_plugin_version") == "action.bot"
    assert family_of("pod_state.signals.firing") == "pod_state.signals"
    assert family_of("pod_state.bots") == "pod_state.bots"
    assert family_of("evolve_help_search") == "evolve_help_search"


def test_cli_manifest_size_prints_json_and_exits(capsys) -> None:
    from evolve_admin.evo.tools.__main__ import _cli

    assert _cli(["--manifest-size"]) == 0
    out = capsys.readouterr().out
    stats = json.loads(out)
    assert stats["registry_tool_count"] == len(all_tools())
    assert stats["tool_count"] < stats["registry_tool_count"]
    assert stats["total_chars"] > 10_000


def test_cli_unknown_args_rejected_not_stdio(capsys) -> None:
    from evolve_admin.evo.tools.__main__ import _cli

    assert _cli(["--bogus"]) == 2
    err = capsys.readouterr().err
    assert "--manifest-size" in err
