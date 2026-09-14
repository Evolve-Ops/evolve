"""Scan router — translates a detector decision into a routing plan.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §7.7.

PR 6 (change detector) decides WHETHER a scan is warranted and at what
SCOPE. PR 7 (this module) decides WHICH LLM TIER (if any) handles each
change. The decision is cost-driven:

  Filter   — handled by reconciliation, no LLM
             (data/log/state files in volatile_paths globs, sha drift
              on data layer, etc.)
  Tier 1   — cheap LLM (haiku-class, ~$0.005)
             (1-2 attributable changes that need light interpretation)
  Tier 3   — expensive LLM (sonnet/opus, ~$0.10-$0.40)
             (≥3 unattributable files for new-app discovery, OR
              Tier 1 escalation)

The router NEVER dispatches LLM calls itself — it returns a
RoutingPlan that the caller (next PR's dispatcher) consumes. This
keeps the router unit-testable without LLM mocks.

## Why split routing from dispatch

Spec §7.7's decision matrix is the load-bearing cost knob: get it
wrong, and either every change burns Tier 3 tokens (90%+ over-spend
relative to the spec's target) or routine changes silently slip
through Filter when they really needed an LLM look. The router's
correctness is verifiable in pure-Python tests; the dispatcher (which
talks to OpenClaw / the network) is harder to test exhaustively.
Separating them means we can pin every route decision with cheap
unit tests.

## Tier 1 output schema

When Tier 1 fires, the LLM returns a JSON payload with:

    {
        "scan_id": "scan-<8hex>",
        "tier": "tier1",
        "tokens": {"input": int, "output": int},
        "verdict": "handled" | "escalate",
        "manifest_patches": [...],     # only when verdict=handled
        "escalation_target":  ...,     # only when verdict=escalate
        "escalation_target_apps": [...],
        "escalation_reason": "..."
    }

``validate_tier1_output`` enforces this contract on the dispatcher's
inputs — the LLM can hallucinate, so we don't trust the payload until
the schema check passes. Invalid → caller can treat as escalation
(safer than applying a malformed patch).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any


# ── Route constants ────────────────────────────────────────────────────────

ROUTE_FILTER = "filter"
ROUTE_TIER1  = "tier1"
ROUTE_TIER3  = "tier3"

VALID_ROUTES = frozenset({ROUTE_FILTER, ROUTE_TIER1, ROUTE_TIER3})


# Tier 1 escalation targets (spec §7.7.4).
TIER3_TARGET_MANIFEST_REGEN  = "manifest_regen"
TIER3_TARGET_DISCOVERY       = "discovery"
TIER3_TARGET_COHERENCE_CHECK = "coherence_check"

VALID_TIER3_TARGETS = frozenset({
    TIER3_TARGET_MANIFEST_REGEN,
    TIER3_TARGET_DISCOVERY,
    TIER3_TARGET_COHERENCE_CHECK,
})


# Manifest fields safe to patch via Tier 1's add/append/replace ops.
# Spec open question Q19: "custom verb set scoped to manifest fields —
# JSON Patch is general-purpose and over-permits operations we don't
# want Tier 1 doing."
TIER1_PATCHABLE_FIELDS = frozenset({
    "files",                # add new files[] entries
    "crons",                # add new crons[] entries
    "scheduled_actions",    # add new scheduled_actions[] entries
    "volatile_paths",       # add new volatile_paths[] entries
    "description",          # short append-style updates
    "requirements",         # add integrations under requirements.integrations
})


TIER1_PATCH_OPS = frozenset({"add", "append", "update_sha", "remove"})


# ── Routing plan ───────────────────────────────────────────────────────────


@dataclass
class RouteEntry:
    """One routing decision for one logical change.

    Each entry pairs a route with the change that produced it. The
    dispatcher consumes the entries in order. ``targeted_apps`` is
    used by Tier 1 (which app's manifest to feed in) and Tier 3
    follow-up dispatches.
    """
    route: str                         # ROUTE_FILTER | ROUTE_TIER1 | ROUTE_TIER3
    targeted_apps: list[str] = field(default_factory=list)
    reason: str = ""
    change_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingPlan:
    """Aggregate of route entries from a single detector decision.

    The dispatcher can short-circuit before any LLM call by checking
    ``has_llm_work`` — if False, only Filter route entries fired and
    the reconciliation pass (PR 4) handles everything.
    """
    entries: list[RouteEntry] = field(default_factory=list)
    scan_id: str = ""           # set by dispatcher when LLM call kicks off
    notes: list[str] = field(default_factory=list)

    @property
    def has_llm_work(self) -> bool:
        return any(e.route in (ROUTE_TIER1, ROUTE_TIER3) for e in self.entries)

    @property
    def discovery_needed(self) -> bool:
        return any(e.route == ROUTE_TIER3 and "discovery" in e.reason.lower()
                   for e in self.entries)

    @property
    def tier1_apps(self) -> list[str]:
        out: list[str] = []
        for e in self.entries:
            if e.route == ROUTE_TIER1:
                for app in e.targeted_apps:
                    if app not in out:
                        out.append(app)
        return out

    @property
    def tier3_apps(self) -> list[str]:
        out: list[str] = []
        for e in self.entries:
            if e.route == ROUTE_TIER3:
                for app in e.targeted_apps:
                    if app not in out:
                        out.append(app)
        return out

    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "has_llm_work": self.has_llm_work,
            "discovery_needed": self.discovery_needed,
            "tier1_apps": self.tier1_apps,
            "tier3_apps": self.tier3_apps,
            "notes": list(self.notes),
            "entries": [
                {
                    "route": e.route,
                    "targeted_apps": list(e.targeted_apps),
                    "reason": e.reason,
                    "change_summary": dict(e.change_summary),
                }
                for e in self.entries
            ],
        }


# ── Attribution helpers ────────────────────────────────────────────────────


def _apps_owning_volatile_glob(
    path: str, manifests: list[dict],
) -> list[str]:
    """Return app_ids whose volatile_paths glob matches ``path``."""
    out: list[str] = []
    for m in manifests:
        if not isinstance(m, dict):
            continue
        app_id = m.get("id") or ""
        if not app_id:
            continue
        for vp in m.get("volatile_paths") or []:
            if not isinstance(vp, dict):
                continue
            glob = vp.get("glob") or ""
            if glob and fnmatch.fnmatch(path, glob):
                out.append(app_id)
                break
    return out


def _apps_claiming_path(
    path: str, manifests: list[dict],
) -> list[str]:
    """Return app_ids whose files[] explicitly claim ``path``."""
    out: list[str] = []
    for m in manifests:
        if not isinstance(m, dict):
            continue
        app_id = m.get("id") or ""
        if not app_id:
            continue
        for entry in m.get("files") or []:
            if not isinstance(entry, dict):
                continue
            if (entry.get("path") or "").lstrip("/") == path:
                out.append(app_id)
                break
    return out


# ── Layer-based filter thresholds (spec §7.7.2 matrix) ─────────────────────

# Layers whose new files / sha drift are handled by Filter only.
_FILTER_LAYERS = frozenset({"data", "log", "state", "content"})

# Layers whose changes need LLM interpretation when attributable.
_TIER1_LAYERS = frozenset({"code", "config", "behavior_doc"})


def _classify_path_for_routing(path: str) -> str | None:
    """Layer-classify a path; returns None if classifier unavailable."""
    try:
        from .layer_classifier import classify
    except Exception:
        return None
    result = classify(path)
    return result.layer


# ── Main entry point ───────────────────────────────────────────────────────


def route(
    detector_decision: Any,
    manifests: list[dict],
    *,
    tier1_attributable_threshold: int = 2,
) -> RoutingPlan:
    """Translate a detector decision into a RoutingPlan.

    Args:
        detector_decision: a ScanDecision (PR 6) or dict-shaped equivalent.
            Reads ``scope``, ``targeted_apps``, ``reasons``, ``evidence``,
            ``scan_warranted``.
        manifests: current manifest set; used for attribution lookups.
        tier1_attributable_threshold: spec §7.7.2 matrix row "1-2 files
            added in code/config, confident attribution → Tier 1".
            Default 2; >2 attributable changes also route to Tier 1
            (the LLM handles batched patches).

    Returns:
        A RoutingPlan. Empty entries means no work — caller can no-op.
    """
    plan = RoutingPlan()

    # Tolerate either ScanDecision object or dict.
    if hasattr(detector_decision, "to_dict"):
        d = detector_decision.to_dict()
    elif isinstance(detector_decision, dict):
        d = detector_decision
    else:
        plan.notes.append("invalid detector_decision shape")
        return plan

    scan_warranted = bool(d.get("scan_warranted"))
    if not scan_warranted:
        plan.notes.append("detector says no scan warranted")
        return plan

    scope = d.get("scope") or "none"
    targeted_apps = list(d.get("targeted_apps") or [])
    reasons = list(d.get("reasons") or [])
    evidence = dict(d.get("evidence") or {})

    # ── full_discovery → Tier 3 ──────────────────────────────────────────
    # The detector picked full_discovery because either ≥3 unattributable
    # files surfaced (rule 2) or a trigger-layer unattributable file
    # appeared (rule 1) — both cases need the clustering LLM Tier 3
    # provides. Targeted apps may also be set (sha drift cases promoted
    # by rules 3/6); we keep them as a sidecar for the dispatcher.
    if scope == "full_discovery":
        plan.entries.append(RouteEntry(
            route=ROUTE_TIER3,
            targeted_apps=targeted_apps,   # may be empty
            reason="detector scope=full_discovery (new-app discovery)",
            change_summary={"detector_reasons": reasons, "evidence": evidence},
        ))
        return plan

    # ── targeted scope → Tier 1 ──────────────────────────────────────────
    # Targeted means specific apps have sha-drift / removed-file changes
    # on their authored code/config/contract files. Tier 1 reads the
    # affected files and updates the manifest narrowly; if the change
    # looks bigger than 1-2 files OR the manifest doesn't exist for the
    # targeted app, escalate to Tier 3.
    if scope == "targeted":
        if not targeted_apps:
            # Defensive: scope=targeted with no apps named is malformed;
            # fall back to no work but log a note.
            plan.notes.append("scope=targeted but no targeted_apps — skipping")
            return plan

        # Look at how many drifted/removed files belong to each targeted app.
        # This is informational for the dispatcher (so it can preempt LLM
        # work if there's only one trivial change).
        per_app_changes = _count_targeted_changes(targeted_apps, reasons)

        for app_id in targeted_apps:
            n_changes = per_app_changes.get(app_id, 0)
            if n_changes > tier1_attributable_threshold:
                plan.entries.append(RouteEntry(
                    route=ROUTE_TIER3,
                    targeted_apps=[app_id],
                    reason=(
                        f"app {app_id} has {n_changes} changes "
                        f"(>{tier1_attributable_threshold} threshold) — "
                        f"Tier 3 manifest regeneration"
                    ),
                    change_summary={
                        "n_changes": n_changes,
                        "escalation_target": TIER3_TARGET_MANIFEST_REGEN,
                    },
                ))
            else:
                plan.entries.append(RouteEntry(
                    route=ROUTE_TIER1,
                    targeted_apps=[app_id],
                    reason=(
                        f"app {app_id} has {n_changes} change(s) — "
                        f"Tier 1 quick scan"
                    ),
                    change_summary={"n_changes": n_changes},
                ))
        return plan

    # ── Other scopes (none, etc.) ───────────────────────────────────────
    plan.notes.append(f"unhandled scope={scope!r}")
    return plan


def _count_targeted_changes(
    targeted_apps: list[str], reasons: list[str],
) -> dict[str, int]:
    """Best-effort count of detector reasons attributable to each app.

    The detector's reason strings include the owning app in the form
    ``sha_drift_on_code_file:scripts/main.py@myapp`` and
    ``claimed_code_file_missing:scripts/x.py@myapp``. We pattern-match
    on the ``@`` separator to bucket.
    """
    counts: dict[str, int] = {a: 0 for a in targeted_apps}
    for r in reasons:
        if "@" not in r:
            continue
        owner = r.rsplit("@", 1)[-1]
        if owner in counts:
            counts[owner] += 1
    return counts


# ── Per-change routing (used by future change-driven inline dispatch) ──────


def classify_individual_change(
    change_kind: str,
    path: str,
    manifests: list[dict],
) -> RouteEntry:
    """Route a single change. Used by the change-driven trigger when
    the detector reports just one change worth dispatching on.

    ``change_kind`` is one of:
        "new_file"          — file appeared, not in any manifest
        "sha_drift"         — sha changed on a claimed file
        "removed_file"      — claimed file gone from disk
        "new_cron"          — cron label appeared, unattributable
        "removed_cron"      — claimed cron label gone
        "heartbeat_changed" — heartbeat sha changed
    """
    # Volatile-path catch first — operator-declared volatile means filter.
    volatile_owners = _apps_owning_volatile_glob(path, manifests)
    if volatile_owners:
        return RouteEntry(
            route=ROUTE_FILTER,
            targeted_apps=volatile_owners,
            reason=f"path matches volatile_paths glob on {volatile_owners}",
            change_summary={"change_kind": change_kind, "path": path},
        )

    if change_kind in ("new_file", "removed_file", "sha_drift"):
        layer = _classify_path_for_routing(path)
        if layer in _FILTER_LAYERS:
            return RouteEntry(
                route=ROUTE_FILTER,
                reason=f"layer={layer} → filter (reconciliation owns)",
                change_summary={"change_kind": change_kind, "path": path,
                                "layer": layer},
            )
        # Tier 1 needs attribution to know which app's manifest to feed.
        owners = _apps_claiming_path(path, manifests)
        if owners:
            return RouteEntry(
                route=ROUTE_TIER1,
                targeted_apps=owners,
                reason=f"attributable to {owners}, layer={layer}",
                change_summary={"change_kind": change_kind, "path": path,
                                "layer": layer},
            )
        # Unattributable → Tier 3 discovery.
        return RouteEntry(
            route=ROUTE_TIER3,
            reason="unattributable change — Tier 3 discovery",
            change_summary={"change_kind": change_kind, "path": path,
                            "layer": layer,
                            "escalation_target": TIER3_TARGET_DISCOVERY},
        )

    if change_kind == "heartbeat_changed":
        return RouteEntry(
            route=ROUTE_TIER3,
            reason="heartbeat changed — Tier 3 full discovery",
            change_summary={"change_kind": change_kind,
                            "escalation_target": TIER3_TARGET_DISCOVERY},
        )

    # Default: unknown change kinds escalate to Tier 3 (safer than
    # silently filtering).
    return RouteEntry(
        route=ROUTE_TIER3,
        reason=f"unknown change_kind {change_kind!r} — Tier 3 fallback",
        change_summary={"change_kind": change_kind, "path": path},
    )


# ── Tier 1 output schema validator ─────────────────────────────────────────


def validate_tier1_output(payload: Any) -> tuple[bool, list[str]]:
    """Validate a Tier 1 LLM output payload against the spec §7.7.3 schema.

    Returns ``(valid, errors)``. ``valid`` is False if any required
    field is missing or has the wrong shape; the dispatcher treats
    invalid Tier 1 output as an escalation to Tier 3 (safer than
    applying a malformed patch).
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["payload is not a dict"]

    # Required top-level fields.
    for key in ("scan_id", "tier", "verdict"):
        if key not in payload:
            errors.append(f"missing required key {key!r}")

    if payload.get("tier") != "tier1":
        errors.append(f"tier must be 'tier1', got {payload.get('tier')!r}")

    verdict = payload.get("verdict")
    if verdict not in ("handled", "escalate"):
        errors.append(
            f"verdict must be 'handled' or 'escalate', got {verdict!r}"
        )

    # Verdict-specific required fields.
    if verdict == "handled":
        patches = payload.get("manifest_patches")
        if not isinstance(patches, list):
            errors.append("verdict=handled requires manifest_patches list")
        else:
            for i, patch in enumerate(patches):
                ok, perrs = _validate_patch(patch)
                if not ok:
                    errors.extend(f"patch[{i}]: {e}" for e in perrs)

    elif verdict == "escalate":
        target = payload.get("escalation_target")
        if target not in VALID_TIER3_TARGETS:
            errors.append(
                f"escalation_target must be one of {sorted(VALID_TIER3_TARGETS)}, "
                f"got {target!r}"
            )
        target_apps = payload.get("escalation_target_apps")
        if not isinstance(target_apps, list):
            errors.append("escalation_target_apps must be a list")

    # Tokens field is optional but if present must be a dict.
    tokens = payload.get("tokens")
    if tokens is not None and not isinstance(tokens, dict):
        errors.append("tokens must be a dict (or absent)")

    return not errors, errors


def _validate_patch(patch: Any) -> tuple[bool, list[str]]:
    """Validate one manifest patch entry from a Tier 1 output."""
    errors: list[str] = []
    if not isinstance(patch, dict):
        return False, ["patch is not a dict"]
    for key in ("app_id", "field", "op"):
        if key not in patch:
            errors.append(f"missing key {key!r}")
    op = patch.get("op")
    if op not in TIER1_PATCH_OPS:
        errors.append(
            f"op must be one of {sorted(TIER1_PATCH_OPS)}, got {op!r}"
        )
    field_name = patch.get("field")
    if field_name not in TIER1_PATCHABLE_FIELDS:
        errors.append(
            f"field must be one of {sorted(TIER1_PATCHABLE_FIELDS)}, "
            f"got {field_name!r}"
        )
    if op in ("add", "append", "update_sha") and "value" not in patch:
        errors.append(f"op={op!r} requires 'value' field")
    return not errors, errors
