"""Scan router tests — PR 7 of the coherence + reconciliation framework.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §7.7.

The router decides which LLM tier (if any) handles each change
flagged by the detector. Wrong routing = expensive Tier 3 every time
(over-spend) OR routine changes silently filter through (missed bugs).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.scan_router import (  # noqa: E402
    ROUTE_FILTER,
    ROUTE_TIER1,
    ROUTE_TIER3,
    TIER1_PATCH_OPS,
    TIER1_PATCHABLE_FIELDS,
    TIER3_TARGET_COHERENCE_CHECK,
    TIER3_TARGET_DISCOVERY,
    TIER3_TARGET_MANIFEST_REGEN,
    VALID_ROUTES,
    VALID_TIER3_TARGETS,
    RouteEntry,
    RoutingPlan,
    classify_individual_change,
    route,
    validate_tier1_output,
)


# ── Routing plan from a no-scan-needed decision ──────────────────────────

def test_route_returns_empty_plan_when_no_scan_warranted() -> None:
    """detector says scan_warranted=False → no work for the router."""
    decision = {
        "scan_warranted": False, "scope": "none", "targeted_apps": [],
        "reasons": [], "evidence": {},
    }
    plan = route(decision, [])
    assert plan.entries == []
    assert plan.has_llm_work is False


def test_route_handles_invalid_decision_shape() -> None:
    """Random object → empty plan + note. Defensive."""
    plan = route("not a dict", [])
    assert plan.entries == []
    assert any("invalid" in n.lower() for n in plan.notes)


# ── full_discovery → Tier 3 ───────────────────────────────────────────────

def test_route_full_discovery_goes_to_tier3() -> None:
    """detector scope=full_discovery → Tier 3."""
    decision = {
        "scan_warranted": True, "scope": "full_discovery",
        "targeted_apps": [], "reasons": ["new_unattributable_code_file:x.py"],
        "evidence": {"new_files": 1},
    }
    plan = route(decision, [])
    assert len(plan.entries) == 1
    assert plan.entries[0].route == ROUTE_TIER3
    assert plan.has_llm_work is True
    assert plan.discovery_needed is True


def test_route_full_discovery_preserves_targeted_apps_as_sidecar() -> None:
    """Even when scope=full_discovery, targeted_apps (from sha drift)
    are kept as a sidecar so the dispatcher can hand them to Tier 1
    in parallel with Tier 3 discovery."""
    decision = {
        "scan_warranted": True, "scope": "full_discovery",
        "targeted_apps": ["myapp"],
        "reasons": ["new_unattributable_code_file:x.py",
                    "sha_drift_on_code_file:scripts/main.py@myapp"],
        "evidence": {},
    }
    plan = route(decision, [])
    assert plan.entries[0].route == ROUTE_TIER3
    assert plan.entries[0].targeted_apps == ["myapp"]


# ── targeted scope ────────────────────────────────────────────────────────

def test_route_targeted_one_change_per_app_to_tier1() -> None:
    """scope=targeted with one sha-drift per app → Tier 1 quick scan."""
    decision = {
        "scan_warranted": True, "scope": "targeted",
        "targeted_apps": ["app1"],
        "reasons": ["sha_drift_on_code_file:scripts/main.py@app1"],
        "evidence": {},
    }
    plan = route(decision, [])
    assert len(plan.entries) == 1
    assert plan.entries[0].route == ROUTE_TIER1
    assert plan.entries[0].targeted_apps == ["app1"]


def test_route_targeted_many_changes_escalates_to_tier3() -> None:
    """When an app has >2 changes (default threshold), the patch set is
    too large for Tier 1 — promote to Tier 3 manifest regeneration."""
    decision = {
        "scan_warranted": True, "scope": "targeted",
        "targeted_apps": ["app1"],
        "reasons": [
            "sha_drift_on_code_file:scripts/a.py@app1",
            "sha_drift_on_code_file:scripts/b.py@app1",
            "sha_drift_on_code_file:scripts/c.py@app1",
            "sha_drift_on_code_file:scripts/d.py@app1",
        ],
        "evidence": {},
    }
    plan = route(decision, [])
    assert plan.entries[0].route == ROUTE_TIER3
    assert "manifest_regen" in plan.entries[0].change_summary.get(
        "escalation_target", "")


def test_route_targeted_each_app_routes_independently() -> None:
    """Different apps with different change-counts route independently."""
    decision = {
        "scan_warranted": True, "scope": "targeted",
        "targeted_apps": ["app1", "app2"],
        "reasons": [
            "sha_drift_on_code_file:scripts/a.py@app1",  # 1 change
            "sha_drift_on_code_file:scripts/x.py@app2",
            "sha_drift_on_code_file:scripts/y.py@app2",
            "sha_drift_on_code_file:scripts/z.py@app2",  # 3 changes → tier3
        ],
        "evidence": {},
    }
    plan = route(decision, [])
    assert len(plan.entries) == 2
    routes_by_app = {e.targeted_apps[0]: e.route for e in plan.entries}
    assert routes_by_app["app1"] == ROUTE_TIER1
    assert routes_by_app["app2"] == ROUTE_TIER3


def test_route_targeted_threshold_configurable() -> None:
    """tier1_attributable_threshold lets ops tune the cost/coverage knob."""
    decision = {
        "scan_warranted": True, "scope": "targeted",
        "targeted_apps": ["app1"],
        "reasons": [
            "sha_drift_on_code_file:scripts/a.py@app1",
            "sha_drift_on_code_file:scripts/b.py@app1",
        ],
        "evidence": {},
    }
    plan = route(decision, [], tier1_attributable_threshold=1)
    # Under threshold=1, 2 changes → escalate.
    assert plan.entries[0].route == ROUTE_TIER3

    plan = route(decision, [], tier1_attributable_threshold=5)
    # Under threshold=5, 2 changes → tier1.
    assert plan.entries[0].route == ROUTE_TIER1


def test_route_targeted_empty_apps_no_work() -> None:
    """scope=targeted with empty targeted_apps → no work, but noted."""
    decision = {
        "scan_warranted": True, "scope": "targeted",
        "targeted_apps": [], "reasons": [], "evidence": {},
    }
    plan = route(decision, [])
    assert plan.entries == []
    assert any("no targeted_apps" in n for n in plan.notes)


# ── classify_individual_change ────────────────────────────────────────────

def test_classify_volatile_path_routes_to_filter() -> None:
    """Volatile-path-matched file → Filter (operator-declared expected)."""
    entry = classify_individual_change(
        change_kind="new_file", path="data/2026-06-06.md",
        manifests=[{
            "id": "j",
            "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
        }],
    )
    assert entry.route == ROUTE_FILTER
    assert entry.targeted_apps == ["j"]


def test_classify_data_layer_file_routes_to_filter() -> None:
    """A data-layer file (not in any glob) → Filter — reconciliation
    silently handles it."""
    entry = classify_individual_change(
        change_kind="new_file", path="data/observations.csv", manifests=[],
    )
    # data/ directory → data layer → filter route.
    assert entry.route == ROUTE_FILTER


def test_classify_code_layer_attributable_routes_to_tier1() -> None:
    """A code file owned by a manifest → Tier 1 quick scan."""
    entry = classify_individual_change(
        change_kind="sha_drift", path="scripts/main.py",
        manifests=[{
            "id": "myapp",
            "files": [{"path": "scripts/main.py", "layer": "code"}],
        }],
    )
    assert entry.route == ROUTE_TIER1
    assert entry.targeted_apps == ["myapp"]


def test_classify_code_layer_unattributable_routes_to_tier3() -> None:
    """A code file not claimed by any manifest → Tier 3 discovery."""
    entry = classify_individual_change(
        change_kind="new_file", path="scripts/orphan.py", manifests=[],
    )
    assert entry.route == ROUTE_TIER3
    assert "discovery" in entry.change_summary.get(
        "escalation_target", "")


def test_classify_heartbeat_change_routes_to_tier3() -> None:
    """heartbeat content change → Tier 3 full discovery (could affect
    any app)."""
    entry = classify_individual_change(
        change_kind="heartbeat_changed", path="HEARTBEAT.md", manifests=[],
    )
    assert entry.route == ROUTE_TIER3


def test_classify_unknown_change_kind_falls_to_tier3() -> None:
    """Defensive: unknown change_kind → Tier 3 (safer than filtering)."""
    entry = classify_individual_change(
        change_kind="rogue_change_type", path="x.txt", manifests=[],
    )
    assert entry.route == ROUTE_TIER3


# ── RoutingPlan properties ────────────────────────────────────────────────

def test_routing_plan_has_llm_work_false_when_only_filter() -> None:
    plan = RoutingPlan(entries=[
        RouteEntry(route=ROUTE_FILTER, reason="data file"),
    ])
    assert plan.has_llm_work is False


def test_routing_plan_has_llm_work_true_when_tier1() -> None:
    plan = RoutingPlan(entries=[
        RouteEntry(route=ROUTE_TIER1, targeted_apps=["app1"]),
    ])
    assert plan.has_llm_work is True


def test_routing_plan_tier1_apps_deduplicated() -> None:
    """Same app referenced from multiple entries dedupes."""
    plan = RoutingPlan(entries=[
        RouteEntry(route=ROUTE_TIER1, targeted_apps=["a", "b"]),
        RouteEntry(route=ROUTE_TIER1, targeted_apps=["a", "c"]),
    ])
    assert plan.tier1_apps == ["a", "b", "c"]


def test_routing_plan_to_dict_round_trips() -> None:
    plan = RoutingPlan(scan_id="scan-abc", entries=[
        RouteEntry(route=ROUTE_TIER1, targeted_apps=["app1"],
                   reason="sha drift", change_summary={"n": 1}),
    ])
    d = plan.to_dict()
    assert d["scan_id"] == "scan-abc"
    assert d["tier1_apps"] == ["app1"]
    assert d["entries"][0]["route"] == ROUTE_TIER1
    assert d["entries"][0]["change_summary"] == {"n": 1}


# ── validate_tier1_output ────────────────────────────────────────────────

def test_validate_tier1_handled_with_valid_patches() -> None:
    payload = {
        "scan_id": "scan-abc", "tier": "tier1",
        "tokens": {"input": 100, "output": 50},
        "verdict": "handled",
        "manifest_patches": [
            {"app_id": "x", "field": "files", "op": "add",
             "value": {"path": "scripts/y.py", "layer": "code"}},
        ],
    }
    valid, errors = validate_tier1_output(payload)
    assert valid, errors


def test_validate_tier1_escalate_with_valid_target() -> None:
    payload = {
        "scan_id": "scan-abc", "tier": "tier1",
        "verdict": "escalate",
        "escalation_target": TIER3_TARGET_DISCOVERY,
        "escalation_target_apps": ["x"],
        "escalation_reason": "...",
    }
    valid, errors = validate_tier1_output(payload)
    assert valid, errors


def test_validate_tier1_rejects_missing_top_level_keys() -> None:
    payload = {"verdict": "handled", "manifest_patches": []}
    valid, errors = validate_tier1_output(payload)
    assert not valid
    assert any("scan_id" in e for e in errors)


def test_validate_tier1_rejects_wrong_tier() -> None:
    payload = {"scan_id": "x", "tier": "tier2", "verdict": "handled",
               "manifest_patches": []}
    valid, errors = validate_tier1_output(payload)
    assert not valid
    assert any("tier" in e for e in errors)


def test_validate_tier1_rejects_unknown_verdict() -> None:
    payload = {"scan_id": "x", "tier": "tier1", "verdict": "ok"}
    valid, errors = validate_tier1_output(payload)
    assert not valid
    assert any("verdict" in e for e in errors)


def test_validate_tier1_rejects_handled_without_patches() -> None:
    payload = {"scan_id": "x", "tier": "tier1", "verdict": "handled"}
    valid, errors = validate_tier1_output(payload)
    assert not valid
    assert any("manifest_patches" in e for e in errors)


def test_validate_tier1_rejects_escalate_invalid_target() -> None:
    payload = {
        "scan_id": "x", "tier": "tier1", "verdict": "escalate",
        "escalation_target": "totally_made_up",
        "escalation_target_apps": [],
    }
    valid, errors = validate_tier1_output(payload)
    assert not valid


def test_validate_tier1_rejects_patch_with_unknown_op() -> None:
    payload = {
        "scan_id": "x", "tier": "tier1", "verdict": "handled",
        "manifest_patches": [
            {"app_id": "a", "field": "files", "op": "obliterate",
             "value": {}}
        ],
    }
    valid, errors = validate_tier1_output(payload)
    assert not valid
    assert any("op" in e for e in errors)


def test_validate_tier1_rejects_patch_with_disallowed_field() -> None:
    """Tier 1 can only touch the safe-list fields. Anything else (e.g.,
    identity) requires Tier 3."""
    payload = {
        "scan_id": "x", "tier": "tier1", "verdict": "handled",
        "manifest_patches": [
            {"app_id": "a", "field": "identity", "op": "append",
             "value": "..."}
        ],
    }
    valid, errors = validate_tier1_output(payload)
    assert not valid
    assert any("field" in e for e in errors)


def test_validate_tier1_rejects_non_dict_payload() -> None:
    valid, errors = validate_tier1_output("not a dict")
    assert not valid


def test_validate_tier1_rejects_tokens_not_dict() -> None:
    payload = {
        "scan_id": "x", "tier": "tier1", "verdict": "handled",
        "manifest_patches": [],
        "tokens": "1234",
    }
    valid, errors = validate_tier1_output(payload)
    assert not valid


# ── Constants are sane ───────────────────────────────────────────────────

def test_route_constants_are_valid() -> None:
    assert ROUTE_FILTER in VALID_ROUTES
    assert ROUTE_TIER1 in VALID_ROUTES
    assert ROUTE_TIER3 in VALID_ROUTES
    assert len(VALID_ROUTES) == 3


def test_tier3_targets_are_valid() -> None:
    assert TIER3_TARGET_MANIFEST_REGEN in VALID_TIER3_TARGETS
    assert TIER3_TARGET_DISCOVERY in VALID_TIER3_TARGETS
    assert TIER3_TARGET_COHERENCE_CHECK in VALID_TIER3_TARGETS
    assert len(VALID_TIER3_TARGETS) == 3


def test_patchable_fields_subset_of_safe_list() -> None:
    """Sanity: the patchable field list doesn't include 'identity' or
    other deep-meaning fields."""
    for unsafe in ("identity", "success_criteria", "constraints",
                   "bot_id", "id"):
        assert unsafe not in TIER1_PATCHABLE_FIELDS, (
            f"unsafe field {unsafe!r} must not be Tier 1 patchable"
        )
