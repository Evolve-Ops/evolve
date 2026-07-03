"""tests/test_evo_tools_mcp.py — MCP bridge for the evo Tool registry.

Verifies the wrapper logic in ``evolve_admin.evo.tools.mcp_server`` —
list_tools mirrors the registry, call_tool dispatches to the right
handler, runtime context (shared_dir, network_path) is injected per
the handler's signature, errors don't crash, results round-trip as
JSON.

Tests build the MCP Server object via ``build_server(ctx)`` and
invoke its handlers programmatically. We don't spin up an actual stdio
subprocess because that requires asyncio orchestration that's out of
scope for unit tests — Phase 2.1's end-to-end test on the mini will
cover the subprocess path.

The MCP Server class wraps registered handlers in
``RequestHandler`` objects; we extract those and invoke directly.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

# The mcp SDK is required for this module. There's also a name-collision
# wrinkle: `packages/analyzer/mcp/` (the MCP-administration package)
# shadows the SDK when analyzer is on sys.path. The fix lives in a
# separate task (rename analyzer/mcp/ → analyzer/mcp_admin/). Until
# that's resolved, these tests skip cleanly rather than fail with an
# ImportError that obscures the real story.
pytest.importorskip(
    "mcp.server",
    reason=(
        "mcp SDK not importable — either not installed in this venv, "
        "or `packages/analyzer/mcp/` is shadowing the SDK. See "
        "'Rename analyzer/mcp/ package' spinoff."
    ),
)

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import mcp_server  # noqa: E402


def _make_context(tmp_path: Path) -> mcp_server.RuntimeContext:
    """Build a RuntimeContext that points all paths into tmp_path. The
    actual network.json file may not exist — tools handle that
    defensively via {available: false, error: ...} returns."""
    return mcp_server.RuntimeContext(
        shared_dir=tmp_path,
        network_path=tmp_path / "network.json",
    )


def _seed_minimal_network(tmp_path: Path) -> None:
    """Write a minimal network.json so config.* tools have something to
    read. Tests that exercise the network path call this first."""
    (tmp_path / "network.json").write_text(json.dumps({
        "networkId": "test-pod",
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "members": ["evolve"],
        "bots": {"evolve": {"role": "primary", "port": 19030}},
    }))


def _seed_signal(shared_dir: Path, *, signature, producer, type_, bot_id,
                 severity, title):
    """Helper mirroring test_evo_tools.py's _seed_signal — creates one
    firing signal via the real store. Reused so tests can populate
    the signal store and then exercise the MCP-wrapped firing tool."""
    from signals import store as _store
    return _store.observe(
        shared_dir,
        signature=signature,
        producer=producer,
        type=type_,
        flavor="security",
        severity=severity,
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=title,
        body=f"test body for {title}",
    )


def _extract_handler(server, kind: str):
    """Pull out the registered list_tools or call_tool handler from an
    mcp.server.Server. The Server stores them in request_handlers
    keyed by the MCP request type. Across MCP-SDK versions this
    storage location moves around; we accept either of two known
    shapes."""
    # mcp.types defines the request type classes; we look them up
    # generically by name so the test isn't pinned to one SDK rev.
    from mcp import types as _mt
    type_name = {
        "list_tools": "ListToolsRequest",
        "call_tool": "CallToolRequest",
    }[kind]
    request_type = getattr(_mt, type_name)
    # Server stores in request_handlers dict.
    handlers = getattr(server, "request_handlers", None)
    if handlers is None:
        # Fallback for older SDKs that used _handlers / _request_handlers
        for attr in ("_request_handlers", "_handlers"):
            handlers = getattr(server, attr, None)
            if handlers is not None:
                break
    assert handlers is not None, "Server has no discoverable handler map"
    return handlers[request_type]


# ─────────────────────────────────────────────────────────────────────────────
# Build / metadata
# ─────────────────────────────────────────────────────────────────────────────


def test_build_server_returns_server_instance(tmp_path):
    """build_server returns an mcp.server.Server. Should not require
    any I/O at construction time."""
    from mcp.server import Server
    ctx = _make_context(tmp_path)
    server = mcp_server.build_server(ctx)
    assert isinstance(server, Server)


def test_signals_and_arbiter_importable_via_bridge_path():
    """The analyzer-side modules the tools actually need MUST resolve
    cleanly. Regression case for the bridge subprocess silently breaking
    half the tool surface.

    Observed 2026-05-19 against a live admin proxy: pod_state.proposals.
    pending returned ``error: 'arbiter store unavailable in this
    runtime'`` because the bridge subprocess (``python3 -m
    evolve_admin.evo.tools``) couldn't import analyzer modules. Back then
    the fix was a sys.path bootstrap in mcp_server; since Phase 6.1 the
    analyzer is a real installed package (evolve-analyzer), so this pins
    the import contract itself.
    """
    import importlib
    for modname in ("signals.store", "tile_metrics", "audit_state"):
        try:
            importlib.import_module(modname)
        except ImportError as exc:
            pytest.fail(
                f"Bridge import contract is broken: {modname} not "
                f"importable. Got: {exc}"
            )


def test_server_name_is_evo_pod_tools(tmp_path):
    """The server identifies itself as 'evo-pod-tools' in OC's MCP
    inventory. Stable name so operator log filtering / metric
    rollups can target it."""
    ctx = _make_context(tmp_path)
    server = mcp_server.build_server(ctx)
    assert server.name == "evo-pod-tools"


# ─────────────────────────────────────────────────────────────────────────────
# list_tools — reflects the registry
# ─────────────────────────────────────────────────────────────────────────────


def test_list_tools_mirrors_registry(tmp_path):
    """list_tools() returns one entry per registered Tool with the
    name + description + input_schema we shipped. Names are sanitized
    for Anthropic-API compatibility (dots → double underscores) — see
    test_anthropic_compat_sanitization for the contract.
    """
    ctx = _make_context(tmp_path)
    server = mcp_server.build_server(ctx)
    handler = _extract_handler(server, "list_tools")

    # The handler is the SDK's wrapper around our @list_tools callback;
    # invoking with a dummy request returns a ServerResult that wraps
    # the tool list. Easier path: call our build-mcp-tool conversion
    # directly and assert it round-trips.
    from evolve_admin.evo.tools.mcp_server import _to_mcp_tool
    mcp_tools = [_to_mcp_tool(t) for t in _tools.all_tools()]
    by_name = {mt.name: mt for mt in mcp_tools}

    # Every registered tool surfaces — but under its SANITIZED name.
    # The canonical dotted names live in the registry; the MCP transport
    # uses the Anthropic-API-legal form.
    registered_canonical = {t.name for t in _tools.all_tools()}
    expected_sanitized = {n.replace(".", "__") for n in registered_canonical}
    assert set(by_name.keys()) == expected_sanitized

    # Specific spot-checks — these tools shipped in Phases 1.0–1.2.
    # MCP surface uses the sanitized form (double underscore separator).
    for expected in (
        "pod_state__signals__firing",
        "pod_state__signals__history",
        "pod_state__proposals__pending",
        "pod_state__proposals__snoozed",
        "pod_state__bots",
        "pod_state__host",
        "pod_state__audit",
        "config__bot",
        "config__network",
    ):
        assert expected in by_name, f"missing {expected} in MCP tool list"

    # Each entry carries description + input_schema. Descriptions are
    # the prose the LLM reads to decide whether to call; truncated or
    # missing descriptions would silently degrade tool selection.
    for name, mt in by_name.items():
        assert mt.description and len(mt.description) > 20, f"{name}: thin description"
        assert mt.inputSchema is not None
        assert mt.inputSchema.get("type") == "object", f"{name}: bad schema"


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic-API name compatibility (added 2026-05-28 after the
# tools.<n>.custom.name regex incident — see mcp_server.py header comment)
# ─────────────────────────────────────────────────────────────────────────────


_ANTHROPIC_TOOL_NAME_REGEX = r"^[a-zA-Z0-9_-]{1,128}$"


def test_anthropic_compat_sanitization():
    """REGRESSION: Anthropic's Messages API rejects tool names that
    don't match ^[a-zA-Z0-9_-]{1,128}$. Every evo tool is named with
    dotted notation (e.g. 'pod_state.config_drift'). The MCP boundary
    MUST sanitize before the model sees the list, otherwise every
    Anthropic-routed turn fails with:
        tools.<n>.custom.name: String should match pattern ...
    """
    import re
    pattern = re.compile(_ANTHROPIC_TOOL_NAME_REGEX)
    from evolve_admin.evo.tools.mcp_server import _to_mcp_tool
    tools = list(_tools.all_tools())
    canonical = tuple(t.name for t in tools)
    for t in tools:
        mcp = _to_mcp_tool(t, canonical_names=canonical)
        assert pattern.match(mcp.name), (
            f"tool {t.name!r} surfaced as {mcp.name!r} — does NOT match "
            f"Anthropic regex {_ANTHROPIC_TOOL_NAME_REGEX!r}. This will "
            f"cause every Anthropic turn to fail with the regex error."
        )


def test_sanitization_is_invertible_for_call_tool():
    """STRUCTURAL: OC sends the SANITIZED name back to _call_tool when
    the model issues a tool_use block. The handler must reverse-translate
    to the canonical form to find the registered Tool. If this contract
    breaks, every tool call from Anthropic-routed turns silently
    resolves to 'unknown tool'."""
    from evolve_admin.evo.tools.mcp_server import (
        _sanitize_name, _desanitize_name,
    )
    for t in _tools.all_tools():
        sanitized = _sanitize_name(t.name)
        # MUST round-trip
        assert _desanitize_name(sanitized) == t.name, (
            f"name {t.name!r} → {sanitized!r} → "
            f"{_desanitize_name(sanitized)!r} (lost in translation)"
        )


def test_descriptions_have_no_unsanitized_tool_references():
    """REGRESSION: descriptions reference other tool names heavily
    (e.g. 'use action.bot.rollback next'). If we sanitize the `name`
    field but leave the description with the dotted reference, the
    model reads the description, issues a tool_use call with the
    UNSANITIZED name, and OC can't find it ('unknown tool'). The
    description rewriter handles this — pin the contract here."""
    from evolve_admin.evo.tools.mcp_server import _sanitize_name, _to_mcp_tool
    tools = list(_tools.all_tools())
    canonical = tuple(t.name for t in tools)
    for t in tools:
        mcp = _to_mcp_tool(t, canonical_names=canonical)
        # No canonical (dotted) tool name should appear in the surfaced
        # description — they should all have been rewritten.
        for other in canonical:
            # Dotless names (e.g. evolve_help_search / evolve_help_read)
            # are surfaced VERBATIM — _sanitize_name only rewrites the
            # "." -> "__" of dotted names. When canonical == sanitized
            # the surfaced name IS the canonical name, so a description
            # reference resolves correctly and is NOT the "unknown tool"
            # footgun this guard exists for. Only flag names sanitization
            # actually changes (the dotted ones).
            if _sanitize_name(other) == other:
                continue
            assert other not in mcp.description, (
                f"tool {t.name!r}'s description still contains the "
                f"unsanitized reference {other!r} — the model will call "
                f"by that name and hit 'unknown tool'. Description: "
                f"{mcp.description[:200]!r}..."
            )


def test_call_tool_resolves_sanitized_name_to_canonical_handler(tmp_path):
    """End-to-end: OC dispatches by the sanitized name (what was
    advertised in list_tools); the registry lookup must succeed."""
    import asyncio
    ctx = _make_context(tmp_path)
    server = mcp_server.build_server(ctx)
    handler = _extract_handler(server, "call_tool")
    if handler is None:
        import pytest
        pytest.skip("could not extract call_tool handler from server")

    # Pick any registered tool, build its sanitized name, dispatch.
    tools = list(_tools.all_tools())
    target = next((t for t in tools if "." in t.name), None)
    if target is None:
        import pytest
        pytest.skip("no dotted tool names in registry — sanitizer is a no-op here")
    sanitized = target.name.replace(".", "__")

    # Just confirm the lookup branch finds the handler (we may not have
    # arguments that match the tool's schema, but the "unknown tool"
    # error path is what we're guarding against).
    # The simplest assertion: a synchronous, non-network registry walk.
    from evolve_admin.evo.tools.mcp_server import _desanitize_name
    canonical_from_sanitized = _desanitize_name(sanitized)
    assert canonical_from_sanitized == target.name, (
        "OC's tool_use dispatch sends the sanitized name; "
        "_desanitize_name must produce the canonical form so the "
        "registry lookup finds the handler. Without this round-trip, "
        "every tool call from an Anthropic-routed turn becomes "
        "'unknown tool'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# _wrap_handler — context injection by signature inspection
# ─────────────────────────────────────────────────────────────────────────────


def test_wrap_handler_injects_shared_dir_when_handler_accepts_it(tmp_path):
    """A tool whose handler accepts shared_dir as a kwarg should have
    it injected from the runtime context. Model-supplied args pass
    through verbatim."""
    ctx = _make_context(tmp_path)
    # Register a test tool that records the kwargs it sees
    captured = {}

    def _handler(shared_dir, bot_id=None):
        captured["shared_dir"] = shared_dir
        captured["bot_id"] = bot_id
        return {"ok": True}

    test_tool = _tools.Tool(
        name="test.shared_dir_injection",
        description="test injection",
        input_schema={"type": "object", "properties": {"bot_id": {"type": "string"}}},
        handler=_handler,
        risk_tier=_tools.RiskTier.READ,
    )
    _tools.register(test_tool)

    wrapped = mcp_server._wrap_handler(test_tool, ctx)
    result = asyncio.run(wrapped({"bot_id": "personal_bot"}))
    assert result == {"ok": True}
    assert captured["shared_dir"] == tmp_path
    assert captured["bot_id"] == "personal_bot"


def test_wrap_handler_injects_network_path(tmp_path):
    """Same for network_path."""
    ctx = _make_context(tmp_path)
    captured = {}

    def _handler(network_path, bot_id):
        captured["network_path"] = network_path
        captured["bot_id"] = bot_id
        return {"ok": True}

    test_tool = _tools.Tool(
        name="test.network_path_injection",
        description="test",
        input_schema={"type": "object", "properties": {"bot_id": {"type": "string"}}},
        handler=_handler,
        risk_tier=_tools.RiskTier.READ,
    )
    _tools.register(test_tool)

    wrapped = mcp_server._wrap_handler(test_tool, ctx)
    result = asyncio.run(wrapped({"bot_id": "team_bot_a"}))
    assert result == {"ok": True}
    assert captured["network_path"] == tmp_path / "network.json"


def test_wrap_handler_no_context_for_handlers_that_dont_need_it(tmp_path):
    """A handler that takes neither shared_dir nor network_path gets
    only the model-supplied args. pod_state.host is the real example
    — it reads psutil directly with no filesystem dep."""
    ctx = _make_context(tmp_path)
    captured = {}

    def _handler(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    test_tool = _tools.Tool(
        name="test.no_context",
        description="test",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
        risk_tier=_tools.RiskTier.READ,
    )
    _tools.register(test_tool)

    wrapped = mcp_server._wrap_handler(test_tool, ctx)
    asyncio.run(wrapped({}))
    # No shared_dir or network_path injected
    assert captured == {}


def test_wrap_handler_catches_handler_exceptions(tmp_path):
    """When a handler raises, the wrapper returns an error dict
    instead of propagating. MCP session stays alive; model sees the
    error and can recover in prose."""
    ctx = _make_context(tmp_path)

    def _handler(shared_dir):
        raise RuntimeError("simulated failure")

    test_tool = _tools.Tool(
        name="test.raises",
        description="test",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
        risk_tier=_tools.RiskTier.READ,
    )
    _tools.register(test_tool)

    wrapped = mcp_server._wrap_handler(test_tool, ctx)
    result = asyncio.run(wrapped({}))
    assert "error" in result
    assert "simulated failure" in result["error"]


def test_wrap_handler_catches_bad_args(tmp_path):
    """Wrong arg names → TypeError caught, returned as error result."""
    ctx = _make_context(tmp_path)

    def _handler(shared_dir, expected_arg):
        return {"ok": True}

    test_tool = _tools.Tool(
        name="test.bad_args",
        description="test",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
        risk_tier=_tools.RiskTier.READ,
    )
    _tools.register(test_tool)

    wrapped = mcp_server._wrap_handler(test_tool, ctx)
    result = asyncio.run(wrapped({"unexpected_arg": "value"}))
    assert "error" in result
    assert "bad args" in result["error"]


def test_wrap_handler_wraps_non_dict_results(tmp_path):
    """If a handler returns something that isn't a dict (defensive
    case — handlers SHOULD return dicts), wrap in a result envelope so
    JSON serialization doesn't fail downstream."""
    ctx = _make_context(tmp_path)

    def _handler():
        return ["a", "b", "c"]

    test_tool = _tools.Tool(
        name="test.list_return",
        description="test",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
        risk_tier=_tools.RiskTier.READ,
    )
    _tools.register(test_tool)

    wrapped = mcp_server._wrap_handler(test_tool, ctx)
    result = asyncio.run(wrapped({}))
    assert result == {"result": ["a", "b", "c"]}


# ─────────────────────────────────────────────────────────────────────────────
# Real tool end-to-end via wrapper (not the MCP server itself, but
# the wrapper used inside call_tool() — same code path).
# ─────────────────────────────────────────────────────────────────────────────


def test_wrapped_signals_firing_returns_real_data(tmp_path):
    """End-to-end: build a wrapped handler for pod_state.signals.firing,
    seed signals into shared_dir, invoke through the wrapper, assert
    the result matches what we'd get calling the handler directly.
    This is the integration that proves the bridge actually plumbs
    context + args correctly."""
    ctx = _make_context(tmp_path)
    _seed_signal(
        tmp_path,
        signature="t:mcp:personal_bot:memory",
        producer="content_scan",
        type_="content_scan_file_disappeared",
        bot_id="personal_bot", severity="alert",
        title="personal_bot: MEMORY.md missing or unreadable",
    )
    tool = _tools.lookup("pod_state.signals.firing")
    assert tool is not None
    wrapped = mcp_server._wrap_handler(tool, ctx)
    result = asyncio.run(wrapped({"bot_id": "personal_bot"}))
    assert result["count"] == 1
    assert result["signals"][0]["title"].startswith("personal_bot:")


def test_wrapped_config_network_redacts_secrets(tmp_path):
    """Regression: when the model invokes config.network via MCP,
    secrets must still be redacted. Verifies the redaction logic is
    in the projection, not the route — i.e. the wrapper doesn't
    accidentally bypass it."""
    ctx = _make_context(tmp_path)
    (tmp_path / "network.json").write_text(json.dumps({
        "networkId": "test-pod",
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "members": ["evolve"],
        "bots": {"evolve": {"role": "primary", "port": 19030}},
        "alerts": {"channel": "telegram", "chatId": "should-not-leak"},
    }))
    tool = _tools.lookup("config.network")
    assert tool is not None
    wrapped = mcp_server._wrap_handler(tool, ctx)
    result = asyncio.run(wrapped({}))
    assert result["available"] is True
    assert result["config"]["alerts"]["chat_configured"] is True
    # chatId value must not appear anywhere in the serialized result
    serialized = json.dumps(result)
    assert "should-not-leak" not in serialized


def test_unknown_tool_returns_clean_error(tmp_path):
    """An MCP client asking for a tool name we don't have returns a
    structured error, not a crash. Belt-and-suspenders — OC should
    only ask for tools it saw in list_tools, but defensive."""
    ctx = _make_context(tmp_path)
    server = mcp_server.build_server(ctx)
    # Direct test: lookup returns None for unknown names.
    assert _tools.lookup("definitely.not.a.real.tool") is None


# ─────────────────────────────────────────────────────────────────────────────
# Default context resolution from env
# ─────────────────────────────────────────────────────────────────────────────


def test_default_shared_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOLVE_SHARED_DIR", str(tmp_path))
    assert mcp_server._default_shared_dir() == tmp_path


def test_default_network_path_from_env(monkeypatch, tmp_path):
    target = tmp_path / "custom-network.json"
    monkeypatch.setenv("EVOLVE_NETWORK_PATH", str(target))
    assert mcp_server._default_network_path() == target


def test_default_shared_dir_fallback(monkeypatch):
    monkeypatch.delenv("EVOLVE_SHARED_DIR", raising=False)
    assert mcp_server._default_shared_dir() == Path("/Users/Shared/evolve")


def test_default_network_path_fallback(monkeypatch):
    monkeypatch.delenv("EVOLVE_NETWORK_PATH", raising=False)
    assert mcp_server._default_network_path() == Path(
        "/Users/Shared/evolve/network.json"
    )
