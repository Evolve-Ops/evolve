"""permissions.posture — rule-based composite-posture classifier.

Spec: docs/spec-permission-posture-2026-05-10.md §3.2 + §8.1.

Pure Python, no LLM. The rule set is small and auditable: a future
reader of an inventory file can re-derive the score by running these
rules over the same fields.

Outputs:
  - score: "tight" | "moderate" | "wide" | "open"
  - axes: per-axis category strings for the matrix display
  - rationale: short one-liner explaining the score (UI tooltip)
"""
from __future__ import annotations

from typing import Any

from .inventory import PermissionInventory

Score = str  # "tight" | "moderate" | "wide" | "open"

# ── Axis classifiers ─────────────────────────────────────────────────────────

def _axis_execution(fields: dict[str, Any]) -> str:
    """Classify the exec axis: deny / allowlist / full+ask-always / full+ask-on-miss / full+ask-off."""
    sec = fields.get("tools.exec.security")
    ask = fields.get("tools.exec.ask")
    if sec == "deny":
        return "deny"
    if sec == "allowlist":
        return "allowlist"
    if sec == "full" and ask == "off":
        return "full+ask-off"
    if sec == "full" and ask == "always":
        return "full+ask-always"
    if sec == "full":
        return "full+ask-on-miss"
    return "unset"


def _axis_filesystem(fields: dict[str, Any]) -> str:
    if fields.get("tools.fs.workspaceOnly") is True:
        return "workspace-only"
    return "unrestricted"


def _axis_web(fields: dict[str, Any]) -> str:
    search = fields.get("tools.web.search.enabled")
    fetch = fields.get("tools.web.fetch.enabled")
    if search is False and fetch is False:
        return "off"
    if search is False or fetch is False:
        return "partial"
    return "open"


def _axis_sandbox(fields: dict[str, Any]) -> str:
    """Sandbox axis is currently stubbed.

    The pre-2026-05-18 implementation read `sandbox.enabled` at the top
    level of openclaw.json, but OC's config schema has no top-level
    `sandbox` key — that read silently returned None for every bot, so
    no bot ever scored "sandbox on". Until sandbox posture is modelled
    against the real OC schema path (agents.defaults.sandbox.mode), this
    axis is "unmodeled" and the score rules don't credit sandbox.
    """
    return "unmodeled"


def _axis_scheduled(inv: PermissionInventory) -> str:
    """Classify cron-job axis.

    "uncapped-agent-turns" — any agentTurn job lacks a turn or budget cap.
    "capped"               — every agentTurn job has both caps.
    "no-agent-turns"       — no agentTurn jobs (system events only).
    "no-cron"              — no cron jobs at all (or no file).
    """
    si = inv.scheduled_invocations
    if not si.present or not si.jobs:
        return "no-cron"
    agent_turns = [j for j in si.jobs if j.payload_kind == "agentTurn" and j.enabled]
    if not agent_turns:
        return "no-agent-turns"
    uncapped = [j for j in agent_turns if not (j.has_turn_cap and j.has_budget_cap)]
    return "uncapped-agent-turns" if uncapped else "capped"


# ── Composite score ──────────────────────────────────────────────────────────

def _has_owner_restriction(fields: dict[str, Any]) -> bool:
    allow_from = fields.get("commands.ownerAllowFrom")
    return isinstance(allow_from, list) and len(allow_from) > 0


def classify(inv: PermissionInventory) -> dict[str, Any]:
    """Apply the rule set from spec §8.1 to compute score + axes + rationale.

    Returns the composite_posture dict the UI consumes.
    """
    fields = inv.permission_config.fields

    axes = {
        "execution": _axis_execution(fields),
        "filesystem": _axis_filesystem(fields),
        "web": _axis_web(fields),
        "sandbox": _axis_sandbox(fields),
        "scheduled": _axis_scheduled(inv),
    }

    sec = fields.get("tools.exec.security")
    ask = fields.get("tools.exec.ask")
    ws_only = fields.get("tools.fs.workspaceOnly") is True
    owner_restricted = _has_owner_restriction(fields)

    # Sandbox is intentionally absent from the rule set — see _axis_sandbox
    # docstring. The pre-2026-05-18 "tight: allowlist + sandbox + workspaceOnly"
    # and "moderate: sandbox enabled" branches could never fire because the
    # underlying read was permanently None. They are dropped rather than
    # carried forward as dead code.
    if sec == "deny":
        score, rationale = "tight", "Execution disabled (security=deny)"
    # open: full + ask=off
    elif sec == "full" and ask == "off":
        score, rationale = "open", "Full exec with no approval gate"
    # moderate: exactly one narrowing axis
    elif sec == "allowlist":
        score, rationale = "moderate", "Exec restricted to allowlist"
    elif ws_only and owner_restricted:
        score, rationale = "moderate", "Workspace-only filesystem + owner-channel restriction"
    elif ws_only:
        score, rationale = "moderate", "Workspace-only filesystem"
    elif owner_restricted:
        score, rationale = "moderate", "Owner-channel restriction"
    # wide: full + ask asks something + no other narrowing
    elif sec == "full" and ask in ("on-miss", "always"):
        score, rationale = "wide", f"Full exec with ask={ask}, no fs scope"
    else:
        score, rationale = "wide", "Default unconstrained config"

    return {
        "score": score,
        "axes": axes,
        "rationale": rationale,
    }


def annotate(inv: PermissionInventory) -> PermissionInventory:
    """Compute composite_posture in-place on an inventory and return it."""
    inv.composite_posture = classify(inv)
    return inv
