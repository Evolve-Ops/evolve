"""
Tests for v7-arc display compat (S2.9) + root-required check (S2.8).

S2.9: list_manifests + GET /api/applications/<bot>/<app_id> must hydrate
v7-arc Instances from their bound Spec so the admin UI shows names /
descriptions instead of falling back to the i-XXX filename stem.

S2.8: --apply refuses to run without root EUID.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from evolve_admin.applications.manifest import hydrate_v7_arc_instance, load_manifest


# ── S2.9: hydrate_v7_arc_instance ─────────────────────────────────────────────

class TestHydrateV7ArcInstance:
    """A v7-arc Instance + its Spec → merged dict that the UI can render."""

    def _seed_spec(self, shared_dir: Path, spec_id: str, spec_version: str, name: str, *,
                    tier: str = "local", source_pod: str | None = None) -> Path:
        if tier == "imported":
            assert source_pod, "imported tier needs source_pod"
            path = shared_dir / "gallery" / "imported" / source_pod / spec_id / f"{spec_version}.json"
        else:
            path = shared_dir / "gallery" / tier / spec_id / f"{spec_version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "spec_id": spec_id,
            "spec_version": spec_version,
            "name": name,
            "schema_version": 14,
            "manifest_shape": "v7-arc",
            "objective": {"primary": f"Goal of {name}", "sub_objectives": []},
            "blueprint": {"files": []},
            "dependencies": {
                "apps": [], "python_packages": [], "system_packages": [],
                "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
            },
            "audience_scoping": {
                "operator": "operator_only", "approved_surfaces": [],
                "role_capabilities": {},
            },
            "description": f"{name} does the thing",
            "tags": ["test", "v7"],
            "app_version": "1.2.3",
            "approval_audience": "pod_operator",
            "success_criteria": {"behavioral": [], "observable": []},
        }))
        return path

    def _instance(self, instance_id: str, spec_id: str, spec_version: str,
                   *, source_pod: str | None = None,
                   realized_files: list | None = None) -> dict:
        return {
            "instance_id": instance_id,
            "bot_id": "team_bot_a",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "provenance": {
                "spec_id": spec_id,
                "spec_version": spec_version,
                "source_pod_id": source_pod,
            },
            "realized_files": realized_files or [],
            "status": "active",
        }

    def test_local_spec_overlays_name(self, tmp_path: Path):
        self._seed_spec(tmp_path, "p-abcd1234", "2026.05.20-1.0", "Journal Logging")
        inst = self._instance("i-aaaa1111", "p-abcd1234", "2026.05.20-1.0")
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated["name"] == "Journal Logging"
        assert hydrated["description"] == "Journal Logging does the thing"
        assert hydrated["tags"] == ["test", "v7"]
        assert hydrated["app_version"] == "1.2.3"
        # `id` falls back to instance_id so the UI's existing flows keep working
        assert hydrated["id"] == "i-aaaa1111"

    def test_objective_flattened_to_primary(self, tmp_path: Path):
        self._seed_spec(tmp_path, "p-abcd1234", "2026.05.20-1.0", "X")
        inst = self._instance("i-aaaa1111", "p-abcd1234", "2026.05.20-1.0")
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated["objective"] == "Goal of X"

    def test_files_derived_from_realized_files(self, tmp_path: Path):
        self._seed_spec(tmp_path, "p-abcd1234", "2026.05.20-1.0", "X")
        inst = self._instance(
            "i-aaaa1111", "p-abcd1234", "2026.05.20-1.0",
            realized_files=[
                {"logical_name": "ingest", "path": "/x/scripts/ingest.py",
                 "file_id": "f-zzz@2026.05.20-1.0"},
                {"logical_name": "summary", "path": "/x/scripts/summary.py",
                 "file_id": "f-yyy@2026.05.20-1.0"},
            ],
        )
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert len(hydrated["files"]) == 2
        assert hydrated["files"][0]["path"] == "/x/scripts/ingest.py"
        assert hydrated["files"][0]["description"] == "ingest"
        assert hydrated["files"][0]["file_id"] == "f-zzz@2026.05.20-1.0"

    def test_status_preserved_from_instance(self, tmp_path: Path):
        # status lives on the Instance (lifecycle is per-bot), not the Spec.
        self._seed_spec(tmp_path, "p-abcd1234", "2026.05.20-1.0", "X")
        inst = self._instance("i-aaaa1111", "p-abcd1234", "2026.05.20-1.0")
        inst["status"] = "paused"
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated["status"] == "paused"

    def test_imported_spec_found_via_source_pod(self, tmp_path: Path):
        self._seed_spec(
            tmp_path, "p-abcd1234", "2026.05.20-1.0", "Imported App",
            tier="imported", source_pod="pod-external",
        )
        inst = self._instance(
            "i-aaaa1111", "p-abcd1234", "2026.05.20-1.0", source_pod="pod-external",
        )
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated["name"] == "Imported App"

    def test_builtin_spec_found(self, tmp_path: Path):
        self._seed_spec(tmp_path, "p-abcd1234", "2026.05.20-1.0", "Builtin", tier="builtin")
        inst = self._instance("i-aaaa1111", "p-abcd1234", "2026.05.20-1.0")
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated["name"] == "Builtin"

    def test_local_wins_over_builtin_when_both_present(self, tmp_path: Path):
        self._seed_spec(tmp_path, "p-abcd1234", "2026.05.20-1.0", "Local Version")
        self._seed_spec(tmp_path, "p-abcd1234", "2026.05.20-1.0", "Builtin Version", tier="builtin")
        inst = self._instance("i-aaaa1111", "p-abcd1234", "2026.05.20-1.0")
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated["name"] == "Local Version"

    def test_no_spec_returns_instance_unchanged(self, tmp_path: Path):
        # No Spec on disk → degrade gracefully, don't crash.
        inst = self._instance("i-aaaa1111", "p-ghost", "2026.05.20-1.0")
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        # Unchanged (no Spec to hydrate from)
        assert hydrated == inst

    def test_missing_provenance_returns_instance_unchanged(self, tmp_path: Path):
        inst = {"instance_id": "i-x", "manifest_shape": "v7-arc"}  # no provenance
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated == inst

    def test_corrupted_spec_file_returns_instance_unchanged(self, tmp_path: Path):
        path = tmp_path / "gallery" / "local" / "p-abcd1234" / "2026.05.20-1.0.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        inst = self._instance("i-aaaa1111", "p-abcd1234", "2026.05.20-1.0")
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated == inst

    def test_identity_block_passed_through(self, tmp_path: Path):
        # v7 Spec preserves the v13 identity block — UI's view modal reads
        # purpose / scope_includes / scope_excludes / user from there.
        spec_path = tmp_path / "gallery" / "local" / "p-abcd1234" / "2026.05.20-1.0.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text(json.dumps({
            "spec_id": "p-abcd1234",
            "spec_version": "2026.05.20-1.0",
            "name": "App",
            "schema_version": 14,
            "manifest_shape": "v7-arc",
            "objective": {"primary": "x", "sub_objectives": []},
            "blueprint": {"files": []},
            "dependencies": {
                "apps": [], "python_packages": [], "system_packages": [],
                "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
            },
            "audience_scoping": {
                "operator": "operator_only", "approved_surfaces": [],
                "role_capabilities": {},
            },
            "identity": {
                "purpose": "Durable record of session activity",
                "scope_includes": ["session start/end", "tool calls"],
                "scope_excludes": ["network packet capture"],
                "user": "team_bot_a bot operator",
            },
        }))
        inst = self._instance("i-aaaa1111", "p-abcd1234", "2026.05.20-1.0")
        hydrated = hydrate_v7_arc_instance(inst, tmp_path)
        assert hydrated["identity"]["purpose"] == "Durable record of session activity"
        assert hydrated["identity"]["scope_includes"] == ["session start/end", "tool calls"]
        assert hydrated["identity"]["user"] == "team_bot_a bot operator"


# ── load_manifest must hydrate v7-arc Instances too ──────────────────────────
#
# Without hydration, ApplicationManifest.from_dict raises on the missing
# required id/name fields and load_manifest silently returns None. That
# surfaces in the admin UI as "manifest not found" when the operator clicks
# Delete on an Instance manifest they can clearly see in the modal —
# delete_application -> delete_manifest -> load_manifest path.

class TestLoadManifestV7Arc:
    def _seed(self, tmp_path: Path) -> tuple[Path, str, str]:
        # Spec under shared_dir/gallery/local
        shared = tmp_path / "shared"
        spec_dir = shared / "gallery" / "local" / "p-abcd1234"
        spec_dir.mkdir(parents=True)
        (spec_dir / "2026.05.20-1.0.json").write_text(json.dumps({
            "spec_id": "p-abcd1234",
            "spec_version": "2026.05.20-1.0",
            "name": "Inquiry Management",
        }))
        # Instance under <workspace>/manifests/<instance_id>.json
        workspace = tmp_path / "bot-ws"
        manifests = workspace / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "i-dd3336e6.json").write_text(json.dumps({
            "instance_id": "i-dd3336e6",
            "bot_id": "admin_bot",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "provenance": {
                "spec_id": "p-abcd1234",
                "spec_version": "2026.05.20-1.0",
                "source_pod_id": None,
            },
            "realized_files": [],
            "status": "active",
        }))
        return shared, str(workspace), "i-dd3336e6"

    def test_v7_arc_instance_loads_via_hydration(self, tmp_path, monkeypatch):
        from evolve_admin.applications import manifest as mod
        shared, ws, app_id = self._seed(tmp_path)
        monkeypatch.setattr(
            "evolve_admin.config.get_bot_workspace",
            lambda bot_id, user=None: Path(ws),
        )
        m = load_manifest(app_id, "admin_bot", shared)
        assert m is not None, "load_manifest must hydrate v7-arc before from_dict"
        assert m.id == "i-dd3336e6"
        assert m.name == "Inquiry Management"


# ── S2.8: root-required check ────────────────────────────────────────────────

class TestRootRequiredForApply:
    """--apply must refuse to run as a non-root user."""

    def test_apply_refused_as_non_root(self, monkeypatch, capsys):
        from evolve_admin.applications import migrate_v7
        monkeypatch.setattr(migrate_v7.os, "geteuid", lambda: 501)
        rc = migrate_v7.main(["--apply"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "requires running as root" in err
        assert "sudo PYTHONPATH" in err
        # The wrong invocation pattern is called out specifically
        assert "NOT: sudo -u evolve" in err

    def test_dry_run_works_as_non_root(self, monkeypatch, tmp_path, capsys):
        # Dry-run is read-only; no root needed.
        from evolve_admin.applications import migrate_v7
        # Stub out the heavy lifting so the test just exercises the gate.
        monkeypatch.setattr(migrate_v7.os, "geteuid", lambda: 501)
        # Empty shared_dir → no bots, no gallery → migration does nothing but returns 0.
        rc = migrate_v7.main(["--shared-dir", str(tmp_path)])
        assert rc == 0  # dry-run path doesn't hit the gate

    def test_rollback_works_as_non_root_in_dry_run(self, monkeypatch, tmp_path, capsys):
        # --rollback without --apply is dry-run; no root needed.
        from evolve_admin.applications import migrate_v7
        monkeypatch.setattr(migrate_v7.os, "geteuid", lambda: 501)
        # Backup doesn't exist; rollback errors but not on the root check.
        rc = migrate_v7.main(["--shared-dir", str(tmp_path), "--rollback", "nonexistent"])
        assert rc == 1
        err = capsys.readouterr().err
        # Should be the "can't read backup manifest" error, NOT the root check
        assert "requires running as root" not in err
