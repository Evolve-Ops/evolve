"""Facade layer — the advertised MCP surface after the B7 Phase 2 cut.

Design: docs/design-evo-mcp-tool-diet-2026-08-01.md (operator-approved
2026-08-01). The 88 fine-grained :class:`~evolve_admin.evo.tools.Tool`
entries in the registry stay exactly as they are — handlers,
``input_schema``, ``risk_tier``, ``authorization_scope``, ``validate`` are
all untouched, and every Evolve-side consumer (the admin-UI proxy's
button-rendering flow, ``tools_by_tier``, the arbiter bridge) keeps
resolving fine-grained Tools. What changes is ONLY what ``list_tools()``
advertises to the model: 12 enum-dispatch facades covering 76 tools, plus
the 12 dieted standalones — 24 advertised entries instead of 88.

Contract highlights (design §3.3–§3.6):

* **Dispatch resolves early.** ``resolve(name, arguments)`` maps a facade
  call to its underlying canonical Tool and strips the enum param; the
  existing authorization gate then runs against the RESOLVED Tool, so
  per-action scopes (e.g. the ``anyone``-scope pod_state queries beside
  admin-scope siblings) keep their exact semantics and refusal envelopes
  still name the canonical tool.
* **Validation gets stronger.** The facade's advertised schema is
  necessarily looser than the member schemas, so the MCP boundary now
  validates the model's remaining args against the underlying tool's
  original ``input_schema`` (jsonschema when available; a structural
  required/unknown-keys check otherwise) BEFORE the handler runs — the
  fine-grained schemas go from advisory to enforced.
* **Self-correcting errors.** Unknown/missing enum values return the
  PodStateTools.ts-style error naming the valid values, so the model can
  retry without operator intervention.
* **Facade schemas are generated, not hand-mirrored.** The enum comes from
  the member map; the shared-parameter union is derived from the live
  member ``input_schema``s at advertisement time, each property annotated
  with the enum values that accept it. A tool-module edit therefore shows
  up in the facade automatically — no second copy to drift.

The old canonical (``action.bot.restart``) and sanitized
(``action__bot__restart``) names remain dispatchable as DEPRECATED aliases
(mcp_server logs each hit); they are never advertised, so they cost zero
prompt chars. Removal is a follow-up once pod logs show no alias traffic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import Tool, all_tools, lookup

log = logging.getLogger(__name__)


# ── The facade map (design §3.1 — the operator-approved 12 + 12 split) ──────


@dataclass(frozen=True)
class FacadeSpec:
    """One advertised enum-dispatch facade over a family of registry Tools."""

    name: str                     # advertised name — deliberately dot-free
    param: str                    # the enum arg: "query" (reads) / "action"
    members: dict[str, str]       # enum value -> canonical registry tool name
    description: str              # wire description incl. routing hints


def _pod_state_members() -> dict[str, str]:
    m = {
        "config_bot": "config.bot",
        "config_network": "config.network",
    }
    for name in (
        "app_scan_status", "app_usage", "audit", "backup_status", "bots", "breakers",
        "config_drift", "cost_caps", "cost_remediation_status", "dispatches",
        "errors", "forge_job", "home_narrative", "host", "pause_state",
        "proposals.in_process", "proposals.pending", "proposals.snoozed",
        "rollback_points", "rollbacks", "signals.firing", "signals.history",
        "subscriptions", "tool_gaps", "usage",
    ):
        m[name] = f"pod_state.{name}"
    return m


FACADES: dict[str, FacadeSpec] = {
    spec.name: spec
    for spec in (
        FacadeSpec(
            name="pod_state",
            param="query",
            members=_pod_state_members(),
            description=(
                "Read-only pod state — one query tool for every dashboard "
                "surface. query values: signals.firing / signals.history "
                "(alert store), proposals.pending / proposals.snoozed / "
                "proposals.in_process (recommendation queue), bots (fleet "
                "tiles + chips), host (CPU/mem/disk), audit (OC security "
                "audit findings), usage, errors (raw per-bot error lines), "
                "rollbacks / rollback_points, app_scan_status, "
                "backup_status, config_drift, home_narrative (the report "
                "banner), subscriptions, dispatches, cost_caps / "
                "cost_remediation_status, breakers, pause_state, forge_job, "
                "tool_gaps, config_bot (one bot's openclaw.json view), "
                "config_network (network.json view). Other args vary per "
                "query — full per-query docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="bot_action",
            param="action",
            members={
                a: f"action.bot.{a}" for a in (
                    "backup_workspace", "pin_plugin_version", "redeploy",
                    "remove", "repair_acls", "rescan_apps", "reset_breaker",
                    "restart", "reverse_rollback", "rollback",
                    "set_auto_memory", "trip_breaker",
                )
            },
            description=(
                "Per-bot lifecycle + maintenance actions (risk tier varies "
                "per action). action values: restart (bounce gateway), "
                "redeploy (full config re-render + restart), remove "
                "(DESTRUCTIVE), rollback (DESTRUCTIVE — always confirms) / "
                "reverse_rollback, trip_breaker / reset_breaker ('cost' "
                "blocks background activity, 'full' takes the gateway "
                "down), pin_plugin_version (exact X.Y.Z pins; NEVER emit "
                "shell for this), set_auto_memory (per-bot OC memory "
                "kill-switch), backup_workspace, rescan_apps, repair_acls. "
                "Required args vary (most take bot_id; pin_plugin_version "
                "takes pins[]) — full per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="proposal_action",
            param="action",
            members={
                a: f"action.proposal.{a}" for a in (
                    "apply", "mark_complete", "refine", "reject", "snooze",
                )
            },
            description=(
                "Act on arbiter proposals: apply (execute a pending "
                "proposal), snooze, reject (terminal), refine (one LLM "
                "refinement round), mark_complete (manual-completion kinds "
                "only). Takes proposal_id plus per-action extras — full "
                "per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="keys_action",
            param="action",
            members={a: f"action.keys.{a}" for a in ("add", "remove", "rotate")},
            description=(
                "Manage provider API keys in a bot's keystore (via the "
                "admin API; secrets are never echoed back): add, rotate, "
                "remove. Full per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="alerts_action",
            param="action",
            members={
                "subscriptions_set": "action.subscriptions.set",
                "subscriptions_test": "action.subscriptions.test",
                "subscriptions_reset": "action.subscriptions.reset",
                "set_digest_hour": "action.alerts.set_digest_hour",
                "send_test_report": "action.alerts.send_test_report",
            },
            description=(
                "Operator notification plumbing: subscriptions_set "
                "(per-signal-type notification preferences; destructive "
                "narrowing requires confirmed=true), subscriptions_test, "
                "subscriptions_reset, set_digest_hour (0-23, pod-local), "
                "send_test_report. Full per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="cost_action",
            param="action",
            members={
                a: f"action.cost.{a}" for a in (
                    "clear_bot_cap", "clear_enforcement", "reset_remediation",
                    "set_bot_cap", "set_cap",
                )
            },
            description=(
                "Cost-control writes (real enforcement — an exceeded L1 cap "
                "trips the breaker and disables the bot's heartbeat): "
                "set_cap (any cost-ladder field; validates ladder ordering; "
                "value=null clears), set_bot_cap (legacy L1 daily USD cap), "
                "clear_bot_cap, clear_enforcement, reset_remediation (clear "
                "tier_downgrade/L1/L2 mid-day after triage). Full "
                "per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="app_action",
            param="action",
            members={
                "install": "action.app.install",
                "audit": "action.app.audit",
                "forge_approve": "action.forge.approve",
                "forge_reject": "action.forge.reject",
            },
            description=(
                "App lifecycle: install (gallery app onto a bot), audit "
                "(queue an async Tier-3 app audit; returns request_id), "
                "forge_approve / forge_reject (forge-job review gates). "
                "Full per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="roster_action",
            param="action",
            members={
                "set_role": "action.roster.set_role",
                "block": "action.roster.block",
                "unblock": "action.roster.unblock",
                "set_newcomer_mode": "action.channel.set_newcomer_mode",
            },
            description=(
                "Cross-bot user administration: set_role, block, unblock "
                "(roster identities), set_newcomer_mode (per-channel "
                "newcomer policy). Full per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="pod_action",
            param="action",
            members={
                a: f"action.pod.{a}" for a in (
                    "pause_all", "resume_all", "trip_breaker", "reset_breaker",
                )
            },
            description=(
                "Pod-wide controls: pause_all / resume_all (every gateway; "
                "pause requires confirm:true — a fleet-wide outage with a "
                "destructive-tier confirmation), trip_breaker / "
                "reset_breaker (pod-scope circuit breaker). Full per-action "
                "docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="signal_action",
            param="action",
            members={
                a: f"action.signal.{a}" for a in ("dismiss", "resolve", "snooze")
            },
            description=(
                "Alert-store writes: snooze (auto-wakes at snoozed_until; "
                "default 24h), dismiss (terminal — does not auto-reopen; "
                "optional verdict feeds producer tuning), resolve. Takes "
                "signal_id. Full per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="audit_action",
            param="action",
            members={a: f"action.audit.{a}" for a in ("mute", "run", "unmute")},
            description=(
                "OC security-audit surface: run (re-run the audit now), "
                "mute (requires a reason) / unmute a finding. Full "
                "per-action docs: meta.tools."
            ),
        ),
        FacadeSpec(
            name="plugin_action",
            param="action",
            members={
                "enable": "action.plugin.enable",
                "disable": "action.plugin.disable",
            },
            description=(
                "Propose an OC plugin enable/disable for a bot (routes "
                "through the plugins-admin propose endpoints; NEVER emit "
                "shell for plugin changes). Full per-action docs: "
                "meta.tools."
            ),
        ),
    )
}

# The 12 tools advertised as themselves (already wire-dieted in B7 v1).
STANDALONE_TOOLS: frozenset[str] = frozenset({
    "action.autonomy.set",
    "action.dispatch.acknowledge",
    "action.errors.dismiss",
    "action.evo.log_tool_gap",
    "action.feedback.file_issue",
    "action.infra.daemon_restart",
    "action.models.check_freshness",
    "action.scan.run",
    "action.security.accept_drift",
    "evolve_help_read",
    "evolve_help_search",
    "meta.tools",
})


def facade_for(canonical_name: str) -> "tuple[str, str] | None":
    """``(facade_name, enum_value)`` covering a canonical tool, or None."""
    for spec in FACADES.values():
        for value, member in spec.members.items():
            if member == canonical_name:
                return spec.name, value
    return None


# ── Generated facade schemas (union of live member schemas) ────────────────


def facade_schema(spec: FacadeSpec) -> dict:
    """The advertised input schema for one facade.

    Enum from the member map; the shared-parameter union derived from the
    live member ``input_schema``s, each property annotated with the enum
    values that accept it (a type is emitted only when every member
    agrees). Loose by construction — the per-action schemas are enforced
    server-side at dispatch (see :func:`validate_against_tool`).
    """
    prop_users: dict[str, list[str]] = {}
    prop_types: dict[str, set] = {}
    for value in sorted(spec.members):
        tool = lookup(spec.members[value])
        if tool is None:
            continue
        for pname, pschema in (tool.input_schema.get("properties") or {}).items():
            prop_users.setdefault(pname, []).append(value)
            if isinstance(pschema, dict) and "type" in pschema:
                prop_types.setdefault(pname, set()).add(str(pschema["type"]))

    properties: dict[str, Any] = {
        spec.param: {
            "type": "string",
            "enum": sorted(spec.members),
        },
    }
    for pname in sorted(prop_users):
        entry: dict[str, Any] = {"description": "for: " + ", ".join(prop_users[pname])}
        types = prop_types.get(pname, set())
        if len(types) == 1:
            entry["type"] = next(iter(types))
        properties[pname] = entry

    return {
        "type": "object",
        "properties": properties,
        "required": [spec.param],
        "additionalProperties": False,
    }


def advertised_manifest() -> list[dict]:
    """The full advertised surface: 12 facades + the 12 standalones, in the
    OC custom-tool shape (canonical names — the MCP bridge sanitizes).

    Every registry tool must be reachable: covered by exactly one facade or
    listed standalone. :func:`coverage_errors` is the guard.
    """
    entries: list[dict] = [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": facade_schema(spec),
        }
        for _, spec in sorted(FACADES.items())
    ]
    for t in all_tools():
        if t.name in STANDALONE_TOOLS:
            entries.append({
                "name": t.name,
                "description": t.oc_description,
                "input_schema": t.input_schema,
            })
    return entries


def coverage_errors() -> list[str]:
    """Registry↔facade coverage defects (the lockstep guard, test-pinned).

    Every registered tool must be exactly one of: a facade member, or a
    standalone. Every facade member must exist in the registry. A tool in
    neither set would silently vanish from the model's world; a tool in
    both would be advertised twice.
    """
    errors: list[str] = []
    covered: dict[str, str] = {}
    for spec in FACADES.values():
        for value, member in spec.members.items():
            if member in covered:
                errors.append(
                    f"{member} covered by both {covered[member]} and "
                    f"{spec.name}({value})"
                )
            covered[member] = f"{spec.name}({value})"
            if lookup(member) is None:
                errors.append(f"{spec.name}({value}) maps to unregistered tool {member}")
    registered = {t.name for t in all_tools()}
    for name in registered:
        in_facade = name in covered
        standalone = name in STANDALONE_TOOLS
        if in_facade and standalone:
            errors.append(f"{name} is both a facade member and a standalone")
        if not in_facade and not standalone:
            errors.append(
                f"{name} is neither covered by a facade nor listed standalone "
                f"— it would vanish from the advertised surface"
            )
    for name in STANDALONE_TOOLS - registered:
        errors.append(f"standalone {name} is not in the registry")
    return errors


# ── Dispatch-time resolution + server-side validation (design §3.3/§3.5) ───


@dataclass(frozen=True)
class ResolvedCall:
    """A facade call resolved to its underlying registry Tool."""

    tool: Tool
    arguments: dict[str, Any]     # model args with the enum param stripped
    facade: str
    value: str


def resolve(name: str, arguments: dict[str, Any]) -> "ResolvedCall | dict | None":
    """Resolve a facade invocation.

    Returns a :class:`ResolvedCall` on success, an ``{"error": ...}``
    payload for a facade name with a missing/unknown enum value (the
    self-correcting shape), or ``None`` when ``name`` is not a facade (the
    caller falls through to the standalone/alias path).
    """
    spec = FACADES.get(name)
    if spec is None:
        return None
    arguments = dict(arguments or {})
    value = arguments.pop(spec.param, None)
    valid = ", ".join(sorted(spec.members))
    if not isinstance(value, str) or not value:
        return {"error": f"{name} requires '{spec.param}' — one of: {valid}"}
    member = spec.members.get(value)
    if member is None:
        return {"error": f"{name}: unknown {spec.param} '{value}'. Valid: {valid}"}
    tool = lookup(member)
    if tool is None:  # registry/facade drift — coverage_errors() test-pins this
        return {"error": f"{name}({value}): underlying tool {member} not registered"}
    return ResolvedCall(tool=tool, arguments=arguments, facade=name, value=value)


def validate_against_tool(tool: Tool, arguments: dict[str, Any]) -> "str | None":
    """Validate model args against the underlying tool's original schema.

    Returns an error string (self-correcting: names the tool and the
    violation) or None when the args validate. Uses jsonschema when
    importable; otherwise falls back to a structural check of required
    keys + unknown keys, so the MCP server still boots (and still
    enforces the load-bearing part) on a host without jsonschema.
    """
    schema = tool.input_schema
    if not isinstance(schema, dict):
        return None
    try:
        import jsonschema  # type: ignore
    except ImportError:
        required = schema.get("required") or []
        missing = [k for k in required if k not in arguments]
        if missing:
            return (
                f"{tool.name}: missing required argument(s) "
                f"{', '.join(missing)}"
            )
        if schema.get("additionalProperties") is False:
            known = set((schema.get("properties") or {}).keys())
            unknown = [k for k in arguments if k not in known]
            if unknown:
                return (
                    f"{tool.name}: unknown argument(s) {', '.join(unknown)}. "
                    f"Valid: {', '.join(sorted(known))}"
                )
        return None
    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "(top level)"
        return f"{tool.name}: invalid arguments at {path}: {exc.message}"
    except jsonschema.SchemaError as exc:
        # A malformed registry schema must not block the call — the
        # pre-facade behavior was no server-side validation at all.
        log.warning("%s: schema unusable for validation: %s", tool.name, exc)
        return None
    return None
