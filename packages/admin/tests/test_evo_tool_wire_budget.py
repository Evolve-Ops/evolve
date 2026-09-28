"""B7 (spec-evolve-overhead-budget §B7): the evo MCP tool manifest rides in
every primary-bot prompt (~27k tokens measured for 88 tools / 92.7k chars on
2026-08-01 — the largest single slice of the primary prefix). These budgets
keep the wire surface dieted:

  * every tool's EFFECTIVE wire description (``Tool.oc_description``) fits a
    hard per-tool budget — fat tools set ``wire_description`` (short,
    selection + usage + hard DO-NOTs) and keep the rich ``description`` as
    the on-demand teaching surface served by ``meta.tools``;
  * schema-internal property descriptions fit a per-tool budget (constraint
    text stays; narrative goes).

The MANIFEST TOTAL is no longer capped here: it is the ``py:evo-mcp-manifest``
surface in tools/context-budget-baseline.txt, enforced diff-relative by
``tools/context-budget-ratchet`` (overhead-budget D1) — one measurement home,
measuring the ADVERTISED manifest (``facades.advertised_manifest()``, the
post-Phase-2 surface the MCP bridge actually serves: 92,726 → 72,462 → 25,265
chars). This file keeps a handoff guard so the surface can't silently drop
out of that baseline or go stale-high.

No allowlists — a tool that genuinely needs more wire prose should spend its
budget on the load-bearing sentences and move the rest behind meta.tools.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.evo import tools as reg  # noqa: E402
from evolve_admin.evo.tools import RiskTier, Tool, build_tool_manifest  # noqa: E402

# Per-tool budget for the description that rides the wire. Chosen so a tool
# gets what selection needs (what it does, when to use it, one hard DO-NOT)
# and nothing else — the 2026-08-01 baseline had 43 tools past 500 chars,
# topping out at 1,297.
WIRE_DESCRIPTION_BUDGET = 400

# Per-tool budget for the sum of ``description`` strings INSIDE the input
# schema. Constraints (enums, exact formats, rejection rules) are worth
# their chars; mechanism narrative is not. Baseline max was 992.
SCHEMA_DESCRIPTION_BUDGET = 600

# Per-facade description budget (design-evo-mcp-tool-diet §3.2): what the
# family does, when to reach for it, one-line-per-action routing hints.
FACADE_DESCRIPTION_BUDGET = 900


def _schema_desc_chars(node) -> int:
    if isinstance(node, dict):
        return sum(
            (len(v) if k == "description" and isinstance(v, str)
             else _schema_desc_chars(v))
            for k, v in node.items()
        )
    if isinstance(node, list):
        return sum(_schema_desc_chars(x) for x in node)
    return 0


def test_every_tool_wire_description_within_budget():
    over = {
        t.name: len(t.oc_description)
        for t in reg.all_tools()
        if len(t.oc_description) > WIRE_DESCRIPTION_BUDGET
    }
    assert not over, (
        f"wire descriptions over the {WIRE_DESCRIPTION_BUDGET}-char budget: "
        f"{over}. Set wire_description= (short form for the prompt) and keep "
        f"the rich text in description (served on demand by meta.tools)."
    )


def test_schema_descriptions_within_budget():
    over = {
        t.name: _schema_desc_chars(t.input_schema)
        for t in reg.all_tools()
        if _schema_desc_chars(t.input_schema) > SCHEMA_DESCRIPTION_BUDGET
    }
    assert not over, (
        f"schema-internal descriptions over the {SCHEMA_DESCRIPTION_BUDGET}-"
        f"char per-tool budget: {over}. Keep constraints, cut narrative."
    )


def test_facade_descriptions_within_budget():
    from evolve_admin.evo.tools import facades as _facades

    over = {
        name: len(spec.description)
        for name, spec in _facades.FACADES.items()
        if len(spec.description) > FACADE_DESCRIPTION_BUDGET
    }
    assert not over, (
        f"facade descriptions over the {FACADE_DESCRIPTION_BUDGET}-char "
        f"budget: {over}. Routing hints belong here; per-action parameter "
        f"docs belong behind meta.tools."
    )


def test_manifest_total_is_budgeted_by_the_context_ratchet():
    """The advertised-manifest total is enforced by tools/context-budget-
    ratchet (D1, diff-relative) as the ``py:evo-mcp-manifest`` surface.
    Guard the handoff: the surface must stay listed in the baseline, and the
    frozen budget must match what the advertised manifest measures TODAY or
    less — i.e. the baseline can't silently drift above the real rendered
    size (a stale-high budget is a free growth pass). This is also what
    makes each B7 cut's down-ratchet mandatory rather than advisory."""
    from evolve_admin.evo.tools import facades as _facades

    baseline = (
        Path(__file__).parents[3] / "tools" / "context-budget-baseline.txt"
    ).read_text(encoding="utf-8")
    row = next(
        (ln for ln in baseline.splitlines()
         if ln.strip().endswith("\tpy:evo-mcp-manifest")),
        None,
    )
    assert row is not None, (
        "py:evo-mcp-manifest missing from tools/context-budget-baseline.txt — "
        "the manifest total would be unenforced. Re-add it and rerun "
        "`tools/context-budget-ratchet --update-baseline`."
    )
    budget = int(row.split("\t", 1)[0])
    total = len(json.dumps(_facades.advertised_manifest()))
    assert budget <= total, (
        f"context-budget baseline froze the advertised evo manifest at "
        f"{budget:,} chars but it renders at {total:,} — the budget is "
        f"stale-high (free growth headroom). Rerun "
        f"`tools/context-budget-ratchet --update-baseline`."
    )


def test_wire_paths_send_the_wire_form():
    """Both prompt-facing renderers must send oc_description, never the full
    teaching text."""
    probe = Tool(
        name="probe.wire_form",
        description="LONG TEACHING " * 50,
        wire_description="short wire form",
        input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: {},
        risk_tier=RiskTier.READ,
    )
    reg.register(probe)
    try:
        rendered = next(
            t for t in build_tool_manifest() if t["name"] == "probe.wire_form"
        )
        assert rendered["description"] == "short wire form"

        from evolve_admin.evo.tools.mcp_server import _to_mcp_tool
        mcp_tool = _to_mcp_tool(probe, canonical_names=(probe.name,))
        assert mcp_tool.description == "short wire form"
    finally:
        reg._REGISTRY[:] = [t for t in reg._REGISTRY if t.name != probe.name]


def test_meta_tools_serves_the_full_teaching():
    """meta.tools is the on-demand escape hatch — it must return the FULL
    description even for tools whose wire form is abbreviated, or the
    trimmed prose is simply lost."""
    from evolve_admin.evo.tools.meta_tools import _project_tool
    probe = Tool(
        name="probe.full_teaching",
        description="the full teaching text with mechanism and caveats",
        wire_description="short",
        input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: {},
        risk_tier=RiskTier.READ,
    )
    projected = _project_tool(probe)
    assert projected["description"] == (
        "the full teaching text with mechanism and caveats"
    )


def test_oc_description_falls_back_to_description():
    probe = Tool(
        name="probe.fallback",
        description="already short",
        input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: {},
        risk_tier=RiskTier.READ,
    )
    assert probe.oc_description == "already short"
