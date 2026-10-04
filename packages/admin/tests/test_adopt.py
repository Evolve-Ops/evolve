"""
Tests for adopt (v7-arc §8.1.5, Adopt v1 pointer-only).

Covers:
- compute_spec_diff classifies presentation_only / structural / no_change correctly
- Unknown fields fail-safe to structural
- adopt_with_specs rebinds provenance.spec_version and appends history
- Validation rejects non-v7-arc, mismatched spec_id, missing provenance
- load_spec_version walks local → builtin → imported tiers
- Round-trip: structural adopt produces a plan with safe_to_adopt=False
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.adopt import (
    AdoptPlan,
    SpecDiff,
    _PRESENTATION_FIELDS,
    _STRUCTURAL_FIELDS,
    adopt_with_specs,
    compute_spec_diff,
    load_spec_version,
)


# ── Fixture builders ─────────────────────────────────────────────────────────


def _baseline_spec(spec_id: str = "p-aaaa1111",
                   version: str = "2026.05.20-1.0") -> dict:
    """Minimal v7-arc Spec dict."""
    return {
        "spec_id": spec_id,
        "spec_version": version,
        "name": "Journal",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "objective": {"primary": "Daily log", "sub_objectives": []},
        "blueprint": {"approach": "regex-based intake"},
        "dependencies": [],
        "audience_scoping": {"pod_operator": True},
    }


def _baseline_instance(spec_id: str = "p-aaaa1111",
                       version: str = "2026.05.20-1.0") -> dict:
    """Minimal v7-arc Instance dict pinned to ``version``."""
    return {
        "instance_id": "i-12345678",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": version,
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": [],
        "status": "active",
    }


# ── compute_spec_diff ────────────────────────────────────────────────────────


class TestComputeSpecDiffClassification:
    def test_identical_specs_are_no_change(self):
        a = _baseline_spec()
        b = _baseline_spec()
        d = compute_spec_diff(a, b)
        assert d.kind == "no_change"
        assert d.safe_to_adopt is True
        assert d.fields_changed == []

    def test_name_change_is_presentation_only(self):
        a = _baseline_spec()
        b = _baseline_spec()
        b["name"] = "Daily Journal"
        d = compute_spec_diff(a, b)
        assert d.kind == "presentation_only"
        assert d.safe_to_adopt is True
        assert "name" in d.fields_changed

    def test_description_change_is_presentation_only(self):
        a = _baseline_spec()
        a["description"] = "old"
        b = _baseline_spec()
        b["description"] = "new"
        d = compute_spec_diff(a, b)
        assert d.kind == "presentation_only"

    def test_blueprint_change_is_structural(self):
        a = _baseline_spec()
        b = _baseline_spec()
        b["blueprint"] = {"approach": "LLM-based"}
        d = compute_spec_diff(a, b)
        assert d.kind == "structural"
        assert d.safe_to_adopt is False
        assert "blueprint" in d.structural_fields_touched

    def test_realized_files_change_is_structural(self):
        a = _baseline_spec()
        b = _baseline_spec()
        b["realized_files"] = [{"file_id": "f-aaa@1.0", "path": "x.py"}]
        d = compute_spec_diff(a, b)
        assert d.kind == "structural"
        assert "realized_files" in d.structural_fields_touched

    def test_constraints_change_is_structural(self):
        a = _baseline_spec()
        b = _baseline_spec()
        b["constraints"] = {"privacy": "no_pii_logged"}
        d = compute_spec_diff(a, b)
        assert d.kind == "structural"

    def test_mixed_presentation_and_structural_is_structural(self):
        """Worst-of-both: name changed AND blueprint changed → structural."""
        a = _baseline_spec()
        b = _baseline_spec()
        b["name"] = "New Name"
        b["blueprint"] = {"approach": "new"}
        d = compute_spec_diff(a, b)
        assert d.kind == "structural"
        assert "name" in d.fields_changed
        assert "blueprint" in d.fields_changed


class TestComputeSpecDiffIgnoredFields:
    def test_spec_version_bump_alone_is_no_change(self):
        """Bumping spec_version with no other diff → no_change."""
        a = _baseline_spec(version="2026.05.20-1.0")
        b = _baseline_spec(version="2026.05.22-1.0")
        d = compute_spec_diff(a, b)
        assert d.kind == "no_change"

    def test_app_version_bump_ignored(self):
        a = _baseline_spec()
        a["app_version"] = "1.0.0"
        b = _baseline_spec()
        b["app_version"] = "1.0.1"
        d = compute_spec_diff(a, b)
        assert d.kind == "no_change"

    def test_source_attribution_change_ignored(self):
        a = _baseline_spec()
        a["source"] = {"pod_id": "pod-a", "bot_id": "team_bot_a"}
        b = _baseline_spec()
        b["source"] = {"pod_id": "pod-b", "bot_id": "admin_bot"}
        d = compute_spec_diff(a, b)
        assert d.kind == "no_change"


class TestComputeSpecDiffUnknownFields:
    def test_unknown_field_added_is_structural(self):
        """Fail-safe: a field not classified anywhere defaults to structural."""
        a = _baseline_spec()
        b = _baseline_spec()
        b["future_field"] = "something we haven't classified yet"
        d = compute_spec_diff(a, b)
        assert d.kind == "structural"
        assert "future_field" in d.structural_fields_touched

    def test_unknown_field_changed_is_structural(self):
        a = _baseline_spec()
        a["future_field"] = "old"
        b = _baseline_spec()
        b["future_field"] = "new"
        d = compute_spec_diff(a, b)
        assert d.kind == "structural"


class TestComputeSpecDiffAddedRemoved:
    def test_presentation_field_added(self):
        a = _baseline_spec()
        b = _baseline_spec()
        b["description"] = "new description"  # not in baseline
        d = compute_spec_diff(a, b)
        assert d.kind == "presentation_only"
        assert "description" in d.fields_added

    def test_presentation_field_removed(self):
        a = _baseline_spec()
        a["description"] = "to be removed"
        b = _baseline_spec()
        d = compute_spec_diff(a, b)
        assert d.kind == "presentation_only"
        assert "description" in d.fields_removed


# ── adopt_with_specs ──────────────────────────────────────────────────────────


class TestAdoptWithSpecsHappyPath:
    def test_rebinds_provenance_spec_version(self):
        inst = _baseline_instance()
        cur = _baseline_spec(version="2026.05.20-1.0")
        tgt = _baseline_spec(version="2026.05.22-1.0")
        plan = adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")

        assert plan.safe_to_adopt is True
        assert plan.new_instance["provenance"]["spec_version"] == "2026.05.22-1.0"

    def test_input_instance_not_mutated(self):
        inst = _baseline_instance()
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")

        before_version = inst["provenance"]["spec_version"]
        adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")

        assert inst["provenance"]["spec_version"] == before_version

    def test_appends_history_entry(self):
        inst = _baseline_instance()
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        plan = adopt_with_specs(
            inst, cur, tgt, "2026.05.22-1.0", reason="lesson_adoption",
        )

        history = plan.new_instance["spec_version_history"]
        assert len(history) == 1
        assert history[0]["version"] == "2026.05.22-1.0"
        assert history[0]["reason"] == "lesson_adoption"
        assert "adopted_at" in history[0]

    def test_preserves_existing_history(self):
        inst = _baseline_instance()
        inst["spec_version_history"] = [
            {"version": "2026.05.18-1.0", "adopted_at": "2026-05-18T00:00:00Z",
             "reason": "initial_install"},
        ]
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        plan = adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")

        history = plan.new_instance["spec_version_history"]
        assert len(history) == 2
        assert history[0]["version"] == "2026.05.18-1.0"
        assert history[1]["version"] == "2026.05.22-1.0"

    def test_default_reason_is_manual_adopt(self):
        inst = _baseline_instance()
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        plan = adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")
        assert plan.new_instance["spec_version_history"][-1]["reason"] == "manual_adopt"

    def test_injected_now_function(self):
        """now() override pins adopted_at deterministically for tests."""
        inst = _baseline_instance()
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        plan = adopt_with_specs(
            inst, cur, tgt, "2026.05.22-1.0",
            now=lambda: "2099-01-01T00:00:00Z",
        )
        assert plan.new_instance["spec_version_history"][-1]["adopted_at"] == "2099-01-01T00:00:00Z"

    def test_plan_fields_populated(self):
        inst = _baseline_instance()
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        plan = adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")
        assert plan.instance_id == "i-12345678"
        assert plan.spec_id == "p-aaaa1111"
        assert plan.from_version == "2026.05.20-1.0"
        assert plan.to_version == "2026.05.22-1.0"


class TestAdoptWithSpecsRefusalSurface:
    def test_structural_diff_returns_plan_with_safe_to_adopt_false(self):
        """Structural Adopt still returns a plan — caller inspects safe_to_adopt
        before writing. This lets the UI render the diff even on refusal."""
        inst = _baseline_instance()
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        tgt["blueprint"] = {"approach": "totally different"}

        plan = adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")
        assert plan.safe_to_adopt is False
        assert plan.spec_diff.kind == "structural"
        # new_instance is still computed (so the UI can preview), but caller
        # must not write it.
        assert plan.new_instance["provenance"]["spec_version"] == "2026.05.22-1.0"


class TestAdoptWithSpecsValidation:
    def test_rejects_v13_legacy_instance(self):
        legacy = {"id": "old-app", "schema_version": 13}
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        with pytest.raises(ValueError, match="v7-arc"):
            adopt_with_specs(legacy, cur, tgt, "2026.05.22-1.0")

    def test_rejects_instance_missing_provenance(self):
        inst = _baseline_instance()
        del inst["provenance"]
        cur = _baseline_spec()
        tgt = _baseline_spec(version="2026.05.22-1.0")
        with pytest.raises(ValueError, match="provenance"):
            adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")

    def test_rejects_target_spec_id_mismatch(self):
        inst = _baseline_instance(spec_id="p-aaaa1111")
        cur = _baseline_spec(spec_id="p-aaaa1111")
        tgt = _baseline_spec(spec_id="p-bbbb2222", version="2026.05.22-1.0")
        with pytest.raises(ValueError, match="spec_id"):
            adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")

    def test_rejects_current_spec_id_mismatch(self):
        inst = _baseline_instance(spec_id="p-aaaa1111")
        cur = _baseline_spec(spec_id="p-cccc3333")
        tgt = _baseline_spec(spec_id="p-aaaa1111", version="2026.05.22-1.0")
        with pytest.raises(ValueError, match="spec_id"):
            adopt_with_specs(inst, cur, tgt, "2026.05.22-1.0")


# ── load_spec_version ────────────────────────────────────────────────────────


class TestLoadSpecVersion:
    def test_loads_from_local(self, tmp_path):
        d = tmp_path / "gallery" / "local" / "p-a"
        d.mkdir(parents=True)
        (d / "2026.05.20-1.0.json").write_text(json.dumps({"spec_id": "p-a"}))

        out = load_spec_version(tmp_path, "p-a", "2026.05.20-1.0")
        assert out == {"spec_id": "p-a"}

    def test_falls_back_to_builtin(self, tmp_path):
        d = tmp_path / "gallery" / "builtin" / "p-a"
        d.mkdir(parents=True)
        (d / "2026.05.20-1.0.json").write_text(json.dumps({"tier": "builtin"}))

        out = load_spec_version(tmp_path, "p-a", "2026.05.20-1.0")
        assert out == {"tier": "builtin"}

    def test_local_wins_over_builtin(self, tmp_path):
        local = tmp_path / "gallery" / "local" / "p-a"
        local.mkdir(parents=True)
        (local / "1.0.json").write_text(json.dumps({"tier": "local"}))
        builtin = tmp_path / "gallery" / "builtin" / "p-a"
        builtin.mkdir(parents=True)
        (builtin / "1.0.json").write_text(json.dumps({"tier": "builtin"}))

        out = load_spec_version(tmp_path, "p-a", "1.0")
        assert out == {"tier": "local"}

    def test_falls_back_to_imported(self, tmp_path):
        imp = tmp_path / "gallery" / "imported" / "pod-other" / "p-a"
        imp.mkdir(parents=True)
        (imp / "2026.05.20-1.0.json").write_text(json.dumps({"tier": "imported"}))

        out = load_spec_version(tmp_path, "p-a", "2026.05.20-1.0")
        assert out == {"tier": "imported"}

    def test_returns_none_when_missing(self, tmp_path):
        assert load_spec_version(tmp_path, "p-missing", "1.0") is None


# ── Field classification sanity ──────────────────────────────────────────────


class TestFieldClassification:
    def test_no_overlap_between_presentation_and_structural(self):
        """Pin: each field must be classified at most once."""
        overlap = _PRESENTATION_FIELDS & _STRUCTURAL_FIELDS
        assert overlap == set(), f"fields in both sets: {overlap}"

    def test_known_fields_listed_in_some_set(self):
        """Sanity: a representative sample of v7 Spec fields is classified."""
        for f in ("name", "objective", "success_criteria"):
            assert f in _PRESENTATION_FIELDS, f
        for f in ("realized_files", "blueprint", "dependencies"):
            assert f in _STRUCTURAL_FIELDS, f
