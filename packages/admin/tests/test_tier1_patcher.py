"""Tier 1 patcher tests — PR 7.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §7.7.3.

The patcher applies LLM-emitted manifest_patches respecting the
provenance gate: observational fields apply directly, authored fields
stage in reconciliation.<list>[]. Tests pin each op + provenance combo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.tier1_patcher import (  # noqa: E402
    PatchResult,
    apply_tier1_patches,
)


# ── add op on observational fields (silent updates) ─────────────────────

def test_add_files_observational_applies_directly() -> None:
    """Observational files[] → add lands in the manifest immediately."""
    manifest = {"files": []}   # no provenance → observational by default
    patches = [{
        "app_id": "myapp", "field": "files", "op": "add",
        "value": {"path": "scripts/helper.py", "layer": "code"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert result.staged_count == 0
    assert result.rejected_count == 0
    assert manifest["files"][0]["path"] == "scripts/helper.py"


def test_add_files_authored_stages_in_reconciliation() -> None:
    """Authored files[] → patch is staged in reconciliation.extra_files,
    NOT applied directly."""
    manifest = {
        "files": [],
        "provenance": {"field_origins": {
            "files": {"source": "forge_built"}
        }},
    }
    patches = [{
        "app_id": "myapp", "field": "files", "op": "add",
        "value": {"path": "scripts/helper.py", "layer": "code"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 0
    assert result.staged_count == 1
    assert manifest["files"] == []   # not modified
    extras = manifest["reconciliation"]["extra_files"]
    assert len(extras) == 1
    assert extras[0]["path"] == "scripts/helper.py"
    assert extras[0]["inferred_layer"] == "code"


def test_add_crons_observational_applies_directly() -> None:
    manifest = {"crons": []}
    patches = [{
        "app_id": "myapp", "field": "crons", "op": "add",
        "value": {"label": "backup", "schedule": "0 3 * * *",
                  "script": "scripts/backup.sh"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert manifest["crons"][0]["label"] == "backup"


def test_add_volatile_paths_applies_directly() -> None:
    """volatile_paths can be added by Tier 1 (operator-declared
    volatile glob → no chip on later data file additions)."""
    manifest = {"volatile_paths": []}
    patches = [{
        "app_id": "myapp", "field": "volatile_paths", "op": "add",
        "value": {"glob": "data/*.md", "layer": "data"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert manifest["volatile_paths"][0]["glob"] == "data/*.md"


def test_add_to_requirements_integrations() -> None:
    """requirements.integrations is the requirements special-case."""
    manifest = {"requirements": {"integrations": []}}
    patches = [{
        "app_id": "myapp", "field": "requirements", "op": "add",
        "value": {"id": "slack", "required": True},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert manifest["requirements"]["integrations"][0]["id"] == "slack"


# ── append op on description ─────────────────────────────────────────────

def test_append_description_observational() -> None:
    manifest = {"description": "Sends a daily briefing."}
    patches = [{
        "app_id": "myapp", "field": "description", "op": "append",
        "value": "Now with weather data.",
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert "Now with weather data" in manifest["description"]


def test_append_description_inserts_space_when_needed() -> None:
    manifest = {"description": "Sends daily."}   # ends without space
    patches = [{
        "app_id": "myapp", "field": "description", "op": "append",
        "value": "New section.",
    }]
    apply_tier1_patches(manifest, patches)
    # No double-space; no missing space.
    assert manifest["description"] == "Sends daily. New section."


def test_append_description_authored_stages_for_review() -> None:
    """Authored description → patch staged for operator review."""
    manifest = {
        "description": "Original.",
        "provenance": {"field_origins": {
            "description": {"source": "user_authored"}
        }},
    }
    patches = [{
        "app_id": "myapp", "field": "description", "op": "append",
        "value": " New section.",
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.staged_count == 1
    assert manifest["description"] == "Original."   # untouched
    decisions = manifest["reconciliation"]["operator_decisions"]
    assert decisions[0]["kind"] == "tier1_pending"


# ── update_sha op ────────────────────────────────────────────────────────

def test_update_sha_observational_applies() -> None:
    manifest = {
        "files": [
            {"path": "scripts/main.py", "layer": "code", "sha256": "old"},
        ],
    }
    patches = [{
        "app_id": "myapp", "field": "files", "op": "update_sha",
        "value": {"path": "scripts/main.py", "sha256": "new"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert manifest["files"][0]["sha256"] == "new"


def test_update_sha_path_not_found_rejected() -> None:
    manifest = {"files": []}
    patches = [{
        "app_id": "myapp", "field": "files", "op": "update_sha",
        "value": {"path": "missing.py", "sha256": "new"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.rejected_count == 1


def test_update_sha_authored_stages() -> None:
    """Even sha updates on authored files go through the gate — the
    operator may have a specific contract on the sha."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code",
                   "sha256": "old"}],
        "provenance": {"field_origins": {"files": {"source": "forge_built"}}},
    }
    patches = [{
        "app_id": "myapp", "field": "files", "op": "update_sha",
        "value": {"path": "scripts/main.py", "sha256": "new"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.staged_count == 1
    assert manifest["files"][0]["sha256"] == "old"   # untouched


# ── remove op ────────────────────────────────────────────────────────────

def test_remove_files_observational_applies() -> None:
    manifest = {
        "files": [
            {"path": "a.py", "layer": "code"},
            {"path": "b.py", "layer": "code"},
        ],
    }
    patches = [{
        "app_id": "myapp", "field": "files", "op": "remove",
        "value": {"path": "a.py"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert [f["path"] for f in manifest["files"]] == ["b.py"]


def test_remove_crons_by_label_observational() -> None:
    manifest = {
        "crons": [
            {"label": "old", "schedule": "x"},
            {"label": "new", "schedule": "y"},
        ],
    }
    patches = [{
        "app_id": "myapp", "field": "crons", "op": "remove",
        "value": {"label": "old"},
    }]
    apply_tier1_patches(manifest, patches)
    assert len(manifest["crons"]) == 1


def test_remove_authored_stages() -> None:
    """Removing an entry from an authored field requires operator
    confirmation."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "provenance": {"field_origins": {"files": {"source": "forge_built"}}},
    }
    patches = [{
        "app_id": "myapp", "field": "files", "op": "remove",
        "value": {"path": "scripts/main.py"},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.staged_count == 1
    assert len(manifest["files"]) == 1   # untouched


# ── Rejection cases ──────────────────────────────────────────────────────

def test_unknown_op_rejected() -> None:
    manifest = {"files": []}
    patches = [{
        "app_id": "x", "field": "files", "op": "obliterate", "value": {},
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.rejected_count == 1
    assert "obliterate" in result.rejected[0]["reason"]


def test_non_dict_patch_rejected() -> None:
    manifest = {"files": []}
    result = apply_tier1_patches(manifest, ["not a dict"])
    assert result.rejected_count == 1


def test_append_to_non_string_field_rejected() -> None:
    """append on a list-typed field → rejected (the LLM mis-typed the
    op; the patcher refuses rather than corrupting the structure)."""
    manifest = {"files": []}
    patches = [{
        "app_id": "x", "field": "files", "op": "append", "value": "..."
    }]
    result = apply_tier1_patches(manifest, patches)
    assert result.rejected_count == 1


# ── PatchResult shape ────────────────────────────────────────────────────

def test_patch_result_to_dict_round_trips() -> None:
    manifest = {"files": []}
    patches = [
        {"app_id": "x", "field": "files", "op": "add",
         "value": {"path": "a.py", "layer": "code"}},
    ]
    result = apply_tier1_patches(manifest, patches)
    d = result.to_dict()
    assert d["applied_count"] == 1
    assert d["staged_count"] == 0
    assert d["rejected_count"] == 0
    assert d["applied"][0]["patch"]["op"] == "add"


# ── Multiple patches in one call ─────────────────────────────────────────

def test_multiple_patches_apply_independently() -> None:
    """Each patch routes through the gate independently. Mixed
    observational/authored manifests have some patches applied and some
    staged."""
    manifest = {
        "files": [],
        "description": "Original.",
        "provenance": {"field_origins": {
            "description": {"source": "user_authored"},
            # files left observational
        }},
    }
    patches = [
        {"app_id": "x", "field": "files", "op": "add",
         "value": {"path": "a.py", "layer": "code"}},  # observational → applied
        {"app_id": "x", "field": "description", "op": "append",
         "value": " More."},  # authored → staged
    ]
    result = apply_tier1_patches(manifest, patches)
    assert result.applied_count == 1
    assert result.staged_count == 1
    assert manifest["files"][0]["path"] == "a.py"
    assert manifest["description"] == "Original."   # untouched
    assert "reconciliation" in manifest


# ── Stage shape on authored ──────────────────────────────────────────────

def test_staged_extra_files_have_required_shape() -> None:
    """The staged entries match spec §5 reconciliation.extra_files[]
    shape so the chip UI can render them directly."""
    manifest = {
        "files": [],
        "provenance": {"field_origins": {"files": {"source": "forge_built"}}},
    }
    patches = [{
        "app_id": "x", "field": "files", "op": "add",
        "value": {"path": "new.py", "layer": "code"},
    }]
    apply_tier1_patches(manifest, patches)
    extra = manifest["reconciliation"]["extra_files"][0]
    for key in ("path", "inferred_layer", "confidence",
                "attribution_evidence", "detected_at",
                "authored_field_affected"):
        assert key in extra, f"missing required key {key}"
    assert extra["authored_field_affected"] == "files"


# ── Empty patches list ───────────────────────────────────────────────────

def test_empty_patches_no_changes() -> None:
    manifest = {"files": []}
    result = apply_tier1_patches(manifest, [])
    assert result.applied_count == 0
    assert result.staged_count == 0
    assert result.rejected_count == 0
    assert manifest == {"files": []}


def test_none_patches_no_changes() -> None:
    """Tolerate ``patches=None`` since LLM output may omit the field."""
    manifest = {"files": []}
    result = apply_tier1_patches(manifest, None)  # type: ignore
    assert result.applied_count == 0
