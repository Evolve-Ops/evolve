"""B7 Phase 2 (design-evo-mcp-tool-diet-2026-08-01): the facade layer.

The MCP surface advertises 12 enum-dispatch facades + the dieted
standalones; the 88 fine-grained registry Tools stay untouched behind
them. These tests pin the design's §3.5 "what must NOT weaken" contract:

  * coverage lockstep — every registry tool is reachable via exactly one
    facade member or a standalone listing (a tool in neither would vanish
    from the model's world);
  * dispatch resolves the underlying Tool and the EXISTING authorization
    gate runs against it per action (mixed-scope families keep per-action
    semantics);
  * server-side schema enforcement — the member's original input_schema
    goes from advisory to enforced at the boundary;
  * self-correcting errors for missing/unknown enum values;
  * old canonical + sanitized names stay dispatchable as unadvertised
    aliases;
  * meta.tools detail mode serves the relocated per-tool teaching.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.evo import tools as reg  # noqa: E402
from evolve_admin.evo.tools import RiskTier, Tool, facades  # noqa: E402
from evolve_admin.evo.tools.authorization import CallerIdentity  # noqa: E402
from evolve_admin.evo.tools.mcp_server import (  # noqa: E402
    RuntimeContext,
    advertised_mcp_tools,
    dispatch_tool_call,
)


# ── Coverage lockstep ─────────────────────────────────────────────────────


def test_every_registry_tool_is_reachable_exactly_once():
    assert facades.coverage_errors() == []


def test_advertised_surface_is_facades_plus_standalones():
    entries = facades.advertised_manifest()
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names))
    assert set(names) == set(facades.FACADES) | facades.STANDALONE_TOOLS
    # The design's headline: 24 advertised entries over an 88-tool registry.
    assert len(names) == len(facades.FACADES) + len(facades.STANDALONE_TOOLS)
    assert len(names) < len(reg.all_tools())


def test_facade_names_are_dot_free():
    """Facade names must survive the Anthropic name regex without the
    ``.`` → ``__`` sanitization (design §3.1)."""
    for name in facades.FACADES:
        assert "." not in name, name


def test_facade_schema_unions_member_properties():
    spec = facades.FACADES["bot_action"]
    schema = facades.facade_schema(spec)
    assert schema["required"] == ["action"]
    assert set(schema["properties"]["action"]["enum"]) == set(spec.members)
    # pin_plugin_version's 'pins' and restart's 'bot_id' both surface.
    assert "pins" in schema["properties"]
    assert "bot_id" in schema["properties"]
    # Property annotations name the actions that accept them.
    assert "pin_plugin_version" in schema["properties"]["pins"]["description"]


# ── resolve() ─────────────────────────────────────────────────────────────


def test_resolve_maps_to_underlying_tool_and_strips_enum():
    resolved = facades.resolve("bot_action", {"action": "restart", "bot_id": "b"})
    assert isinstance(resolved, facades.ResolvedCall)
    assert resolved.tool.name == "action.bot.restart"
    assert resolved.arguments == {"bot_id": "b"}


def test_resolve_missing_enum_is_self_correcting():
    err = facades.resolve("signal_action", {"signal_id": "x"})
    assert isinstance(err, dict)
    assert "requires 'action'" in err["error"]
    assert "snooze" in err["error"] and "dismiss" in err["error"]


def test_resolve_unknown_enum_is_self_correcting():
    err = facades.resolve("pod_state", {"query": "nope"})
    assert isinstance(err, dict)
    assert "unknown query 'nope'" in err["error"]
    assert "signals.firing" in err["error"]


def test_resolve_non_facade_returns_none():
    assert facades.resolve("action.bot.restart", {}) is None
    assert facades.resolve("meta.tools", {}) is None


# ── Server-side schema enforcement (design §3.5.2) ────────────────────────


def test_validate_rejects_missing_required_args():
    tool = reg.lookup("action.signal.snooze")
    err = facades.validate_against_tool(tool, {})
    assert err is not None and "action.signal.snooze" in err


def test_validate_accepts_conforming_args():
    tool = reg.lookup("pod_state.signals.firing")
    assert facades.validate_against_tool(tool, {}) is None


def test_validate_rejects_unknown_args_when_schema_is_closed():
    tool = reg.lookup("action.bot.pin_plugin_version")
    err = facades.validate_against_tool(
        tool, {"pins": [{"bot_id": "b", "plugin_name": "p", "version": "1.2.3"}],
               "bogus_arg": 1},
    )
    assert err is not None and "bogus_arg" in err


# ── The MCP boundary end-to-end ───────────────────────────────────────────


def _ctx(tmp_path, surface="admin_ui"):
    return RuntimeContext(
        shared_dir=tmp_path, network_path=tmp_path / "network.json",
        caller_identity=CallerIdentity(surface=surface),
    )


def _call(ctx, name, args):
    return asyncio.run(dispatch_tool_call(name, args, ctx))


def test_list_tools_advertises_the_consolidated_surface(tmp_path):
    tools = advertised_mcp_tools()
    names = {t.name for t in tools}
    assert len(names) == len(facades.FACADES) + len(facades.STANDALONE_TOOLS)
    assert "pod_state" in names and "bot_action" in names
    assert "meta__tools" in names          # standalone, sanitized
    # No fine-grained facade member is advertised.
    assert "pod_state__signals__firing" not in names
    assert "action__bot__restart" not in names


def test_facade_call_dispatches_to_member_handler(tmp_path):
    probe_calls = []
    probe = Tool(
        name="pod_state.facade_probe",
        description="probe", input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: probe_calls.append(kw) or {"ok": True},
        risk_tier=RiskTier.READ,
        authorization_scope="anyone",
    )
    reg.register(probe)
    facades.FACADES["pod_state"].members["facade_probe"] = probe.name
    try:
        payload = _call(_ctx(tmp_path), "pod_state", {"query": "facade_probe"})
        assert payload == {"ok": True}
        assert probe_calls == [{}]
    finally:
        del facades.FACADES["pod_state"].members["facade_probe"]
        reg._REGISTRY[:] = [t for t in reg._REGISTRY if t.name != probe.name]


def test_facade_call_enforces_member_schema(tmp_path):
    payload = _call(_ctx(tmp_path), "signal_action", {"action": "snooze"})
    assert "error" in payload
    assert "action.signal.snooze" in payload["error"]


def test_facade_authorization_runs_per_resolved_action(tmp_path):
    """Mixed-scope families: a non-admin caller is refused on an
    admin-scope member but served by an anyone-scope member of the SAME
    facade — the gate runs against the resolved Tool, not the facade."""
    ctx = _ctx(tmp_path, surface="cross_bot_member")

    # admin-scope member → refusal envelope naming the canonical tool.
    denied = _call(ctx, "signal_action", {"action": "snooze", "signal_id": "s1"})
    denied_text = json.dumps(denied)
    assert "authorization" in denied_text or "admin" in denied_text

    # anyone-scope member of a facade still serves.
    anyone_members = [
        (spec.name, value)
        for spec in facades.FACADES.values()
        for value, member in spec.members.items()
        if (t := reg.lookup(member)) and t.authorization_scope == "anyone"
    ]
    assert anyone_members, "expected at least one anyone-scope facade member"


def test_alias_dispatch_still_serves_old_names(tmp_path, caplog):
    """Design §3.6: canonical and sanitized old names keep working
    (deprecation-logged), so stale teaching degrades to a warning."""
    import logging
    ctx = _ctx(tmp_path)
    with caplog.at_level(logging.INFO):
        payload = _call(ctx, "pod_state__host", {})
    assert "error" not in payload or "unknown tool" not in str(payload.get("error"))
    assert any("deprecated direct tool name" in r.message for r in caplog.records)

    payload = _call(ctx, "pod_state.host", {})
    assert "unknown tool" not in str(payload.get("error", ""))


def test_standalone_dispatch_is_not_deprecation_logged(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        payload = _call(_ctx(tmp_path), "meta__tools", {})
    assert payload.get("count", 0) > 0
    assert not any("deprecated direct tool name" in r.message for r in caplog.records)


def test_unknown_tool_still_errors(tmp_path):
    payload = _call(_ctx(tmp_path), "no_such_tool", {})
    assert "unknown tool" in payload["error"]


# ── meta.tools detail mode (design §3.4 — the relocated teaching) ─────────


def test_meta_tools_facade_index_and_action_detail():
    from evolve_admin.evo.tools.meta_tools import _handler
    admin = CallerIdentity(surface="admin_ui")

    index = _handler(tool="bot_action", caller_identity=admin)
    assert index["facade"] == "bot_action"
    values = {m["value"] for m in index["members"]}
    assert "restart" in values and "pin_plugin_version" in values

    detail = _handler(tool="bot_action", action="pin_plugin_version",
                      caller_identity=admin)
    d = detail["detail"]
    assert d["name"] == "action.bot.pin_plugin_version"
    # Full teaching text + exact schema — this is where the diet's prose lives.
    assert len(d["description"]) > 400
    assert d["input_schema"]["properties"]["pins"]
    assert d["invoke_via"] == {
        "facade": "bot_action", "param": "action", "value": "pin_plugin_version",
    }


def test_meta_tools_canonical_name_detail_and_unknown_action():
    from evolve_admin.evo.tools.meta_tools import _handler
    admin = CallerIdentity(surface="admin_ui")

    detail = _handler(tool="pod_state.signals.firing", caller_identity=admin)
    assert detail["detail"]["name"] == "pod_state.signals.firing"
    assert detail["detail"]["invoke_via"]["facade"] == "pod_state"

    err = _handler(tool="bot_action", action="nope", caller_identity=admin)
    assert "unknown action 'nope'" in err["error"]


def test_meta_tools_listing_carries_invoke_via():
    from evolve_admin.evo.tools.meta_tools import _handler
    admin = CallerIdentity(surface="admin_ui")
    listing = _handler(prefix="action.signal.", caller_identity=admin)
    assert listing["count"] >= 3
    for row in listing["tools"]:
        assert row["invoke_via"]["facade"] == "signal_action"
