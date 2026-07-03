"""Meta-tool — registry introspection.

Reliability lever #5 from spec §3.7. Closes:

  * Category 2 (confabulation — tool/capability fabrication). When
    the model can list its real tools, it can't invent fake ones.
  * Category 7 (discoverability — "what can you do?"). The
    operator's natural question gets a tool-grounded answer.

The tool returns the live registry — same data that powers
``build_tool_manifest()`` for the OC manifest. Read-tier (pure
introspection; no side effects). Model can call it any time without
gates.

Why this is needed even though TOOLS.md exists in evo's workspace:

  * Workspace files are loaded at session start. New tools added
    after the session began aren't in the model's context until next
    session. ``meta.tools`` is always live.

  * TOOLS.md is rendered prose. ``meta.tools`` returns structured
    data the model can filter / pattern-match against.

  * The AGENTS.md rule "don't claim a tool exists without confirming
    it via the tool registry" cites this tool by name. Without it
    the rule has no concrete escape hatch.
"""

from __future__ import annotations

import logging
from typing import Any

from . import RiskTier, Tool, all_tools, register
from .authorization import (
    DEFAULT_CONSERVATIVE_IDENTITY,
    NON_ADMIN_HELP_FOOTER,
    CallerIdentity,
    visible_tools,
)

log = logging.getLogger(__name__)


def _project_tool(tool: Tool) -> dict[str, Any]:
    """Project one Tool into the model-facing shape.

    Strips internals that don't help the model (handler reference,
    validate reference). Keeps everything that helps it decide
    whether/how to call the tool: name, description, risk_tier, tags,
    input_schema, and a derived ``requires_validate`` flag for
    confirmation-aware reasoning.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "risk_tier": tool.risk_tier.value,
        "tags": list(tool.tags),
        "authorization_scope": tool.authorization_scope,
        "input_schema": tool.input_schema,
        # Informational — non-read tools require validate(), which the
        # admin-UI proxy runs as the 3rd gate before rendering a
        # confirmation button. Model doesn't call validate directly,
        # but knowing the surface is gated helps it explain to the
        # operator what will happen when it offers the action.
        "requires_validate": tool.validate is not None,
    }


def _handler(
    *,
    tag: str | None = None,
    risk_tier: str | None = None,
    prefix: str | None = None,
    caller_identity: CallerIdentity = DEFAULT_CONSERVATIVE_IDENTITY,
) -> dict[str, Any]:
    """Return the registry, optionally filtered.

    All filters AND together. Empty filters → all tools (after the
    authorization-scope filter — see below).

    Phase 2 authorization framework: the result is always filtered to
    tools the *caller* is authorized to invoke. Admin callers see
    everything; non-admin (cross-bot member) callers see only the
    ``user`` and ``anyone`` scope tools, plus a footer telling them
    their pod admin has access to additional commands. The MCP bridge
    injects ``caller_identity`` automatically when the handler declares
    the kwarg.

    The response shape mirrors other ``pod_state.*`` list tools:
    ``{count, tools, filters_applied}`` so the model can scan the
    same way it does for signals / proposals / bots.
    """
    # Authorization-scope filter runs first so the caller never sees a
    # tool they couldn't invoke — keeps the model from "helpfully"
    # suggesting an admin-only command to a cross-bot member.
    tools = list(visible_tools(all_tools(), caller_identity))

    filters_applied: dict[str, Any] = {}
    if tag:
        tools = [t for t in tools if tag in t.tags]
        filters_applied["tag"] = tag
    if risk_tier:
        # Validate against the enum — silently ignore unknown values
        # so the model can pass through what it heard from the
        # operator without us raising a TypeError.
        if risk_tier in {r.value for r in RiskTier}:
            tools = [t for t in tools if t.risk_tier.value == risk_tier]
            filters_applied["risk_tier"] = risk_tier
        else:
            filters_applied["risk_tier_ignored"] = risk_tier
    if prefix:
        tools = [t for t in tools if t.name.startswith(prefix)]
        filters_applied["prefix"] = prefix

    result: dict[str, Any] = {
        "count": len(tools),
        "tools": [_project_tool(t) for t in tools],
        "filters_applied": filters_applied,
    }
    if not caller_identity.is_admin:
        # Non-admin callers see a brief footer pointing them at the
        # right escalation path. Admin callers don't need it — the
        # full registry is already visible to them.
        result["non_admin_footer"] = NON_ADMIN_HELP_FOOTER
    return result


TOOL = Tool(
    name="meta.tools",
    description=(
        "List the tools currently registered in evo's runtime. Returns "
        "name, description, risk_tier, tags, and input_schema for "
        "each. Optional filters: tag (eg \"action\", \"signal\"), "
        "risk_tier (eg \"read\", \"write_safe\"), or name prefix "
        "(eg \"pod_state.\"). Use this when the operator asks "
        "\"what can you do?\" / \"what tools do you have?\" / \"is "
        "there a tool for X?\" — and CRITICALLY, use this BEFORE "
        "claiming a tool doesn't exist. Your training has a snapshot "
        "of the registry that may be stale; the live registry is the "
        "only source of truth."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tag": {
                "type": "string",
                "description": (
                    "Filter to tools carrying this tag. Common tags: "
                    "'pod_state', 'config', 'action', 'signal', "
                    "'proposal', 'bots'."
                ),
            },
            "risk_tier": {
                "type": "string",
                "enum": ["read", "write_safe", "write_risky", "destructive"],
                "description": (
                    "Filter to tools at this tier. 'read' = side-"
                    "effect-free; 'write_safe' = reversible; "
                    "'write_risky' = non-trivial-to-undo; "
                    "'destructive' = irreversible."
                ),
            },
            "prefix": {
                "type": "string",
                "description": (
                    "Filter to tools whose name starts with this "
                    "string. Useful for 'show me only the proposal "
                    "tools' (prefix='pod_state.proposals') or "
                    "'what action tools do I have?' (prefix='action.')."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("meta",),
    # The handler already filters its output by the caller's scope and
    # appends the "ask your pod admin" footer for non-admins, so opening
    # meta.tools itself to anyone is safe — a cross-bot member sees only
    # the user/anyone tools, never the admin-tier surface. Keeping it
    # admin-only would defeat the introspection lever (a cross-bot member
    # would have no way to learn what they CAN run).
    authorization_scope="anyone",
)

register(TOOL)
