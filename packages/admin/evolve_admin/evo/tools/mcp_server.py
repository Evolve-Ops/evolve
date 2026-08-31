"""Bridge the evo Tool registry to an MCP stdio server.

OpenClaw can register external tools via the standard MCP (Model Context
Protocol) over either subprocess-stdio or HTTP transport. Subprocess-stdio
is the right choice for evo because OC manages our lifecycle — it spawns
the server on session start, talks to it over stdin/stdout, and reaps
on idle.

Wire-up:

* This module exposes a Python callable (``run()``) that starts an MCP
  server on stdio.

* ``python3 -m evolve_admin.evo.tools`` is the CLI entry point OC
  subprocess'es (see ``__main__.py``). Runtime context (``shared_dir``,
  ``network_path``) reaches the handlers via env vars.

* OC's openclaw.json registers us under ``mcp.servers.evo_tools`` —
  the deploy hook lands in Phase 2.1; for v1 we register manually on
  the test pod and validate end-to-end.

Bridge mechanics:

* The MCP server's tool list comes from ``evolve_admin.evo.tools.all_tools()``
  — same registry the Phase 1 tests exercise. No duplication of
  tool definitions between Python and MCP.

* For each Tool, the bridge inspects the handler's parameter signature
  to figure out which runtime-context kwargs to inject (``shared_dir``
  for tools that read the signal store / arbiter store; ``network_path``
  for tools that read network.json / openclaw.json; neither for tools
  with no filesystem dependency). The model-supplied kwargs (the
  ``arguments`` dict from OC) are passed through verbatim.

* Tool results are JSON-serialized into a single ``TextContent``. OC's
  Pi-embedded runner reads the text, parses, and passes the result back
  to the model.

* Sync handler functions run on a thread (``asyncio.to_thread``) so
  the MCP server's asyncio loop isn't blocked by file I/O.

Failure modes:

* Unknown tool name → MCP error result. Should never happen in practice
  (OC asks for tools that were in our tool list), but defensive.

* Handler raised an exception → caught, error message returned as the
  tool result. The model sees the error and can recover in prose. We
  do NOT raise back to MCP because that would tear down the session.

* Registry import failure → caught at server startup, exits with a
  non-zero status. OC will surface the failure to the operator instead
  of a half-running server.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

# Analyzer modules (``signals.store``, ``arbiter.store``, ``tile_metrics``,
# ``audit_state``) come from the installed evolve-analyzer package.

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import TextContent  # noqa: E402
from mcp.types import Tool as McpTool  # noqa: E402

from . import Tool as _EvoTool  # noqa: E402
from . import all_tools  # noqa: E402
from .authorization import (  # noqa: E402
    DEFAULT_CONSERVATIVE_IDENTITY,
    CallerIdentity,
    check_authorization,
)

log = logging.getLogger(__name__)


# ── Anthropic-API name compatibility ────────────────────────────────────────
#
# Anthropic's Messages API rejects tool names that don't match
# ``^[a-zA-Z0-9_-]{1,128}$`` — dots are NOT allowed. Every evo tool is
# named with dotted notation (``pod_state.config_drift``,
# ``action.plugin.enable``, etc.) because that's the canonical schema
# documented in TOOLS.md and referenced throughout the codebase.
#
# Renaming all 30+ tools to use underscores would touch every tool file,
# every cross-tool reference (e.g. action_security.py's "next: pod_state.
# config_drift" hint strings), every skill file, and every system prompt.
# Massive blast radius for what's really a transport-layer constraint.
#
# Instead: sanitize at the MCP boundary. The model sees Anthropic-legal
# names (e.g. ``pod_state__config_drift``); the registry keeps the
# canonical dotted names. We translate both directions and also rewrite
# tool-name references inside descriptions so the model doesn't read
# "use action.bot.rollback" and call by the unsanitized form.
#
# ``.`` ↔ ``__`` (double underscore) is the chosen mapping. No canonical
# tool name currently contains ``__`` (verified by grep), so the mapping
# is a clean bijection. If you add a tool with ``__`` in its name, fix
# the encoding here first.
#
# Incident: 2026-05-28 — Anthropic tightened server-side regex enforcement,
# causing Sonnet calls to fail with ``tools.<n>.custom.name: String
# should match pattern ...`` across every member bot. Admin_bot/Team_bot_a's grok-4
# fallback worked because xAI didn't enforce; Team_bot_c (Anthropic-only
# fallback) and evo (admin chat) became unreachable.

_SANITIZE_FROM = "."
_SANITIZE_TO = "__"


def _sanitize_name(name: str) -> str:
    """Convert canonical dotted evo tool name → Anthropic-API-legal form."""
    return name.replace(_SANITIZE_FROM, _SANITIZE_TO)


def _desanitize_name(sanitized: str) -> str:
    """Inverse of :func:`_sanitize_name`. Used when OC dispatches a
    tool_use block back to us — OC saw the sanitized name; we look up
    the registry entry under its canonical form."""
    return sanitized.replace(_SANITIZE_TO, _SANITIZE_FROM)


def _sanitize_description(description: str, canonical_names: tuple[str, ...]) -> str:
    """Rewrite references to canonical tool names inside a tool's
    description so they match the sanitized names the model will see.

    Without this, a description like *"Use action.bot.rollback next"*
    would teach the model to issue a tool_use call with name
    ``action.bot.rollback`` — which OC would then fail to find (it
    only listed the sanitized form).

    Sorts canonical names longest-first so that a substring tool name
    (e.g. ``action.bot``) doesn't shadow a longer one
    (``action.bot.rollback``) during replacement.
    """
    for name in sorted(canonical_names, key=len, reverse=True):
        if name in description:
            description = description.replace(name, _sanitize_name(name))
    return description


# Server metadata — surfaces in OC's MCP-server inventory.
_SERVER_NAME = "evo-pod-tools"


# Default paths. CLI overrides via env vars; tests inject via run().
def _default_shared_dir() -> Path:
    return Path(os.environ.get("EVOLVE_SHARED_DIR") or "/Users/Shared/evolve")


def _default_network_path() -> Path:
    return Path(os.environ.get("EVOLVE_NETWORK_PATH") or "/Users/Shared/evolve/network.json")


def _default_caller_identity() -> CallerIdentity:
    """Build the CallerIdentity for production MCP-server startup.

    Three env vars carry the surface info from whichever transport spawned
    this subprocess:

      * ``EVOLVE_CALLER_SURFACE`` — one of
        ``admin_ui`` / ``telegram_operator`` / ``cross_bot_member``.
        When unset or unrecognized we fall back to the conservative
        ``cross_bot_member`` default; a misconfigured transport is
        treated as non-admin rather than silently elevated.
      * ``EVOLVE_CALLER_BOT_ID`` — originating bot for cross-bot
        callers (optional; used in refusal messages).
      * ``EVOLVE_CALLER_OPERATOR_HANDLE`` — operator's channel handle
        when known (optional; refusal messages only).

    Every real spawn path sets ``EVOLVE_CALLER_SURFACE`` explicitly via
    ``proxy.send_to_evo`` (the admin-UI routes pass ``admin_ui``; the
    Telegram-operator and cross-bot relay paths pass their own surface).
    So an absent/unrecognized value here means a transport we don't know
    about or a direct/misconfigured spawn — and we fail CLOSED, granting
    the least privilege rather than silently elevating to admin.
    """
    raw = (os.environ.get("EVOLVE_CALLER_SURFACE") or "").strip().lower()
    if raw not in ("admin_ui", "telegram_operator", "cross_bot_member"):
        # Fail closed: unknown/absent surface → least privilege, never admin.
        surface: Any = "cross_bot_member"
    else:
        surface = raw
    bot_id = (os.environ.get("EVOLVE_CALLER_BOT_ID") or "").strip() or None
    handle = (os.environ.get("EVOLVE_CALLER_OPERATOR_HANDLE") or "").strip() or None
    return CallerIdentity(
        surface=surface, bot_id=bot_id, operator_handle=handle,
    )


@dataclass(frozen=True)
class RuntimeContext:
    """Runtime context the bridge injects into tool handlers.

    Most tools accept exactly one of ``shared_dir`` or ``network_path``;
    a couple need neither (``pod_state.host`` reads psutil directly,
    ``pod_state.audit`` reads a module-global cache). The bridge
    inspects each handler's signature to decide which of these to
    inject — handlers stay clean and declarative.

    ``caller_identity`` (Phase 2 authorization) is the surface +
    optional ``bot_id`` / ``operator_handle`` of whoever invoked the
    tool. Defaults to the conservative ``cross_bot_member`` so any
    transport that hasn't been updated to thread the identity through
    yet is treated as non-admin (refusing admin-tier tools). Today
    every MCP call to this server originates from evo's gateway
    subprocess started by the admin-UI proxy or evolve-bot Telegram —
    both admin surfaces — so production callers should pass an
    ``admin_ui`` or ``telegram_operator`` identity explicitly. Cross-bot
    callers wire in once their relay path lands (currently a stub; the
    audit doc tracks the open work).
    """

    shared_dir: Path
    network_path: Path
    caller_identity: CallerIdentity = DEFAULT_CONSERVATIVE_IDENTITY


def _wrap_handler(
    tool: _EvoTool, context: RuntimeContext,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Build the async callable MCP will invoke for one tool.

    Inspects the underlying handler's signature and injects only the
    context kwargs it actually accepts. Model-supplied args from the
    MCP call pass through verbatim.

    Async wrapping uses asyncio.to_thread so a sync handler doing
    file I/O doesn't block the MCP server's event loop. Tools with
    no filesystem work (host, audit) still run on a thread for
    consistency — overhead is negligible at our call volumes.
    """
    handler = tool.handler
    try:
        params = inspect.signature(handler).parameters
    except (ValueError, TypeError):
        params = {}

    context_kwargs: dict[str, Any] = {}
    if "shared_dir" in params:
        context_kwargs["shared_dir"] = context.shared_dir
    if "network_path" in params:
        context_kwargs["network_path"] = context.network_path
    # Phase 2 authorization framework — handlers that need to filter
    # their own output by caller scope (notably ``meta.tools``) can
    # declare a ``caller_identity`` kwarg and the bridge injects the
    # active identity. Tools that don't declare it get the same
    # signature they had pre-Phase-2.
    if "caller_identity" in params:
        context_kwargs["caller_identity"] = context.caller_identity

    async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
        combined = {**context_kwargs, **(args or {})}
        try:
            result = await asyncio.to_thread(handler, **combined)
        except TypeError as exc:
            # Model sent unexpected arg / missing required arg →
            # surface as a tool error, don't crash the session.
            log.warning("Tool %s: bad args: %s", tool.name, exc)
            return {"error": f"bad args: {exc}"}
        except Exception as exc:  # noqa: BLE001
            log.exception("Tool %s: handler raised", tool.name)
            return {"error": f"handler failed: {exc}"}
        # Defensive: handlers should return dicts; if one returns
        # something else, wrap in a result envelope so we don't
        # JSON-serialize-fail downstream.
        if not isinstance(result, dict):
            return {"result": result}
        return result

    return wrapped


def _to_mcp_tool(
    tool: _EvoTool, canonical_names: tuple[str, ...] | None = None,
) -> McpTool:
    """Translate one evo Tool to the mcp.types.Tool shape MCP exposes
    in its list_tools() response. Risk tier / validate stay on the
    Evolve side and aren't surfaced — those are concerns for the
    admin-UI proxy's button-rendering (spec §5.2), not for the MCP
    transport.

    Sanitizes the tool name AND any references to canonical tool names
    in the description, so the model never sees the dotted form (which
    Anthropic's API rejects with a regex error). ``canonical_names`` is
    the set of all registered tool names for the description rewrite;
    callers that don't supply it get a fresh snapshot from the registry
    (callers that translate many tools should snapshot once and pass it
    in to avoid O(N²)).
    """
    if canonical_names is None:
        canonical_names = tuple(t.name for t in all_tools())
    return McpTool(
        name=_sanitize_name(tool.name),
        # B7 context diet: the wire carries the SHORT form; the full
        # description stays behind meta.tools for on-demand teaching.
        description=_sanitize_description(tool.oc_description, canonical_names),
        inputSchema=tool.input_schema,
    )


def build_server(context: RuntimeContext) -> Server:
    """Construct the MCP Server instance with tools + handlers wired up.

    Exposed as a separate function so tests can build a server with a
    tmp_path-based context and invoke list_tools / call_tool
    programmatically (no real subprocess).

    The Server is created with name = ``_SERVER_NAME`` (visible in
    OC's MCP inventory). list_tools() snapshots the registry at call
    time, so adding a Tool via register() AFTER server construction is
    reflected on the next list_tools() call.
    """
    server = Server(_SERVER_NAME)

    # Each tool gets its own bound wrapper — closure over (tool, context).
    # Built lazily on every call_tool() because the registry can grow
    # at runtime (e.g. test that registers a tool after build_server).
    @server.list_tools()
    async def _list_tools() -> list[McpTool]:
        return advertised_mcp_tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        payload = await dispatch_tool_call(name, arguments, context)
        return [TextContent(
            type="text",
            text=json.dumps(payload, ensure_ascii=False, default=str),
        )]

    return server


def advertised_mcp_tools() -> list[McpTool]:
    """The consolidated advertised surface (B7 Phase 2,
    design-evo-mcp-tool-diet-2026-08-01): 12 enum-dispatch facades + the
    dieted standalones — NOT the 88-entry registry. The registry itself is
    unchanged; old names stay dispatchable as unadvertised aliases.
    Module-level (not a closure) so tests exercise exactly what the
    ``list_tools`` callback serves."""
    from . import facades as _facades

    canonical = tuple(t.name for t in all_tools())
    return [
        McpTool(
            name=_sanitize_name(entry["name"]),
            description=_sanitize_description(entry["description"], canonical),
            inputSchema=entry["input_schema"],
        )
        for entry in _facades.advertised_manifest()
    ]


async def dispatch_tool_call(
    name: str, arguments: "dict[str, Any] | None", context: RuntimeContext,
) -> dict[str, Any]:
    """Resolve + gate + invoke one tool call; returns the payload dict.

    The whole B7 Phase 2 dispatch contract lives here (module-level so
    tests drive it without SDK request plumbing):

    1. Facade names resolve to their underlying registry Tool
       (self-correcting errors for missing/unknown enum values), the
       member's ORIGINAL input_schema is enforced server-side (the
       facade's advertised union is looser by construction — design
       §3.5.2), and the EXISTING authorization gate runs against the
       resolved Tool so per-action scopes and refusal envelopes are
       unchanged.
    2. Standalones dispatch as before (sanitized or canonical name).
    3. Any other registry name is a DEPRECATED alias (design §3.6) —
       served, but logged, and never advertised.
    """
    from . import facades as _facades

    async def _gated_invoke(tool, args: dict[str, Any]):
        denial = check_authorization(tool, context.caller_identity)
        if denial is not None:
            log.info(
                "Tool %s denied for caller surface=%s "
                "(scope=%s)", tool.name, context.caller_identity.surface,
                tool.authorization_scope,
            )
            return denial
        wrapped = _wrap_handler(tool, context)
        return await wrapped(args)

    resolved = _facades.resolve(name, arguments or {})
    if isinstance(resolved, dict):          # facade, bad/missing enum value
        return resolved
    if resolved is not None:                # facade → underlying tool
        error = _facades.validate_against_tool(resolved.tool, resolved.arguments)
        if error:
            return {"error": error}
        return await _gated_invoke(resolved.tool, resolved.arguments)

    # Standalone (advertised directly) or a deprecated alias for a
    # facade-covered tool. OC dispatches via the SANITIZED name; accept
    # the canonical form too for internal callers.
    canonical_name = _desanitize_name(name)
    match = next(
        (t for t in all_tools() if t.name == canonical_name or t.name == name),
        None,
    )
    if match is None:
        return {"error": f"unknown tool: {name}"}
    if match.name not in _facades.STANDALONE_TOOLS:
        covering = _facades.facade_for(match.name)
        log.info(
            "deprecated direct tool name %s — advertised surface is %s",
            match.name,
            f"{covering[0]}({covering[1]})" if covering else "facades",
        )
    return await _gated_invoke(match, arguments or {})


async def run(context: RuntimeContext | None = None) -> None:
    """Run the MCP server on stdio until OC closes the connection.

    ``context`` is optional for testing — production callers (the CLI
    entry point) pass None and we fill from env vars. Test callers
    inject a tmp_path-based context.

    Logs go to stderr — stdout is reserved for the MCP wire protocol;
    writing anything there breaks the framing.
    """
    if context is None:
        context = RuntimeContext(
            shared_dir=_default_shared_dir(),
            network_path=_default_network_path(),
            caller_identity=_default_caller_identity(),
        )

    # Smoke-check: log what we're about to serve so the operator can
    # spot a runtime-context misconfiguration when reading the gateway
    # log. The MCP spec reserves stdout; logging.basicConfig defaults
    # to stderr in our environment, which is correct.
    log.info(
        "evo MCP server starting: shared_dir=%s network_path=%s tools=%d",
        context.shared_dir, context.network_path, len(all_tools()),
    )

    server = build_server(context)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Sync entry point — ``python3 -m evolve_admin.evo.tools`` lands here.

    Sets up basic logging to stderr (default for logging.basicConfig)
    and runs the asyncio event loop. Returns normally on clean
    shutdown; raises on uncaught errors during startup (OC's
    subprocess wrapper will surface the exit code).
    """
    logging.basicConfig(
        level=os.environ.get("EVO_TOOLS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
