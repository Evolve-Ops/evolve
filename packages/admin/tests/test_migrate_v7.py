"""
Tests for the v13 → v7-arc manifest migration.

Covers the five corner cases Step 0 surfaced against real production
manifests, plus end-to-end JSON Schema validation of the migrated outputs.

Synthetic fixtures used here rather than copies of real manifests so the
suite doesn't carry production bot identifiers or operator-authored prose.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest

from evolve_admin.applications.migrate_v7 import (
    BACKUP_MANIFEST_VERSION,
    INITIAL_SPEC_VERSION,
    SEEDED_FROM_PKG_SHA256_KEY,
    SEEDED_FROM_PKG_VERSION_KEY,
    BackupRun,
    GalleryReseedResult,
    MigrationResult,
    RollbackResult,
    _build_blueprint,
    _build_dependencies,
    _build_integrations,
    _build_realized_files,
    _chown_paths_to_evolve,
    _extract_instance,
    _extract_spec,
    _infer_privacy,
    _list_bots_from_network,
    _migrate_app_dependencies,
    _migrate_status,
    _new_instance_id,
    _new_spec_id,
    _normalize_file_id,
    _normalize_v13_file_entry,
    _resolve_local_pod_id,
    _resolve_spec_id,
    migrate_gallery_package,
    migrate_instance,
    reseed_builtin_specs,
    rewrite_markers,
    rollback_migration,
    version_sort_key,
)
from evolve_admin.applications.provenance import (
    ProvenanceMarker,
    _parse_marker_str,
    embed_marker,
    format_marker_string,
    parse_marker,
)


# ── Schema loading ────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_DIR = _REPO_ROOT / "docs" / "schemas"

SPEC_SCHEMA = json.loads((_SCHEMA_DIR / "manifest-v7-spec.schema.json").read_text())
INSTANCE_SCHEMA = json.loads((_SCHEMA_DIR / "manifest-v7-instance.schema.json").read_text())
LESSONS_SCHEMA = json.loads((_SCHEMA_DIR / "manifest-v7-lessons.schema.json").read_text())


def _validate(data: dict, schema: dict) -> list[str]:
    """Return list of error paths (empty = clean)."""
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message[:120]}"
        for err in validator.iter_errors(data)
    ]


# ── Synthetic fixtures ────────────────────────────────────────────────────────

def _baseline_v13() -> dict:
    """Minimal valid v13 manifest. All other fixtures derive from this."""
    return {
        "id": "test-app",
        "name": "Test App",
        "bot_id": "testbot",
        "description": "A minimal test app",
        "status": "active",
        "schema_version": 13,
        "objective": "Do the thing",
        "files": [
            {"path": "scripts/run.py", "description": "Main runner", "file_id": "f-abc12345"},
        ],
        "crons": [
            {"name": "daily", "schedule": "0 9 * * *", "command": "scripts/run.py", "description": "Daily run"},
        ],
        "created_at": "2026-05-01T00:00:00Z",
    }


# ── Bug-fix tests ─────────────────────────────────────────────────────────────

class TestNetworkParsing:
    """Step 0 Finding #1: network.json bots is a dict, not a list."""

    def test_dict_shape(self, tmp_path: Path):
        net = tmp_path / "network.json"
        net.write_text(json.dumps({"bots": {"team_bot_a": {}, "evo": {}, "admin_bot": {}}}))
        assert sorted(_list_bots_from_network(tmp_path)) == ["admin_bot", "evo", "team_bot_a"]

    def test_list_shape_backwards_compat(self, tmp_path: Path):
        net = tmp_path / "network.json"
        net.write_text(json.dumps({"bots": [{"id": "team_bot_a"}, {"id": "evo"}]}))
        assert sorted(_list_bots_from_network(tmp_path)) == ["evo", "team_bot_a"]

    def test_missing_or_malformed(self, tmp_path: Path):
        assert _list_bots_from_network(tmp_path) == []  # no file
        (tmp_path / "network.json").write_text("not json")
        assert _list_bots_from_network(tmp_path) == []
        (tmp_path / "network.json").write_text(json.dumps({"bots": "string"}))
        assert _list_bots_from_network(tmp_path) == []


class TestPodIdResolution:
    """Step 0 Finding #2: pod_id is at top-level networkId, not pod.id."""

    def test_reads_network_id(self, tmp_path: Path):
        (tmp_path / "network.json").write_text(
            json.dumps({"networkId": "pod-test-123", "pod": {"ssh_target": "x"}})
        )
        assert _resolve_local_pod_id(tmp_path) == "pod-test-123"

    def test_missing_falls_back(self, tmp_path: Path):
        (tmp_path / "network.json").write_text(json.dumps({"pod": {}}))
        assert _resolve_local_pod_id(tmp_path) == "pod-unknown"


class TestFileIdNormalization:
    """Step 0 Finding #3: real markers come in four shapes."""

    def _result(self):
        return MigrationResult(source_path=Path("/tmp/x"), dry_run=True)

    def test_canonical_passthrough(self):
        r = self._result()
        out = _normalize_file_id("f-abc12345@2026.05.20-1.0", "x", r)
        assert out == "f-abc12345@2026.05.20-1.0"
        assert not r.warnings

    def test_bare_gets_version_suffix(self):
        r = self._result()
        out = _normalize_file_id("f-abc12345", "x", r)
        assert out == f"f-abc12345@{INITIAL_SPEC_VERSION}"
        assert not r.warnings

    def test_old_dot_form_rewritten(self):
        r = self._result()
        out = _normalize_file_id("f-abc12345@2026.05.20.1", "x", r)
        assert out == "f-abc12345@2026.05.20-1.0"
        assert not r.warnings

    def test_non_conformant_minted_with_warning(self):
        r = self._result()
        # Real-world example from evolve-cve.json: human label misused as file_id
        out = _normalize_file_id("proc-cve-scan", "scripts/x.md", r)
        assert out.startswith("f-")
        assert out.endswith(f"@{INITIAL_SPEC_VERSION}")
        assert any("non-conformant" in w for w in r.warnings)

    def test_empty_minted_with_warning(self):
        r = self._result()
        out = _normalize_file_id("", "scripts/x.py", r)
        assert out.startswith("f-")
        assert any("empty" in w for w in r.warnings)


class TestStatusMigration:
    """Step 0 Finding #4: v13 'approved' status not in v7 enum."""

    def test_approved_maps_to_active(self):
        assert _migrate_status("approved") == "active"

    def test_v7_values_passthrough(self):
        for s in ("active", "paused", "draft", "deprecated"):
            assert _migrate_status(s) == s

    def test_empty_defaults_to_active(self):
        assert _migrate_status(None) == "active"
        assert _migrate_status("") == "active"


class TestPrivacyConsentNoticeShape:
    """Step 0 Finding #5: consent_notice may be list in v13."""

    def test_list_joined_with_bullets(self):
        v13 = {"constraints": {"privacy": ["No PII logged.", "Encrypted at rest."]}}
        r = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        priv = _infer_privacy(v13, r)
        assert "- No PII logged." in priv["consent_notice"]
        assert "- Encrypted at rest." in priv["consent_notice"]
        assert isinstance(priv["consent_notice"], str)

    def test_string_passthrough(self):
        v13 = {"constraints": {"privacy": "Plain string notice."}}
        r = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        priv = _infer_privacy(v13, r)
        assert priv["consent_notice"] == "Plain string notice."

    def test_empty_no_consent_notice(self):
        v13 = {"constraints": {"privacy": ""}}
        r = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        priv = _infer_privacy(v13, r)
        assert "consent_notice" not in priv

    def test_list_with_garbage_filtered(self):
        v13 = {"constraints": {"privacy": ["ok", None, "", 42, "also ok"]}}
        r = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        priv = _infer_privacy(v13, r)
        # only the two string entries should be present as bullets
        assert priv["consent_notice"].count("- ") == 2


# ── End-to-end migration + schema validation ──────────────────────────────────

class TestEndToEndMigration:
    """Each fixture is migrated, then validated against the v7 schemas."""

    def _migrate(self, v13: dict) -> tuple[dict, dict, MigrationResult]:
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _new_spec_id()
        instance_id = _new_instance_id()
        spec = _extract_spec(v13, spec_id, result)
        instance = _extract_instance(v13, instance_id, spec_id, "testbot", result)
        return spec, instance, result

    def test_minimal_manifest_validates(self):
        v13 = _baseline_v13()
        spec, instance, _ = self._migrate(v13)
        assert _validate(spec, SPEC_SCHEMA) == [], "Spec must validate"
        assert _validate(instance, INSTANCE_SCHEMA) == [], "Instance must validate"

    def test_approved_status_fixture_validates(self):
        v13 = _baseline_v13() | {"status": "approved"}
        _, instance, _ = self._migrate(v13)
        assert instance["status"] == "active"
        assert _validate(instance, INSTANCE_SCHEMA) == []

    def test_list_privacy_fixture_validates(self):
        v13 = _baseline_v13() | {
            "constraints": {"privacy": ["No PII", "Encrypted at rest"]}
        }
        spec, _, _ = self._migrate(v13)
        assert isinstance(spec["privacy"]["consent_notice"], str)
        assert _validate(spec, SPEC_SCHEMA) == []

    def test_bare_file_id_fixture_validates(self):
        # baseline already uses bare 'f-abc12345' — confirms the normalization
        v13 = _baseline_v13()
        _, instance, _ = self._migrate(v13)
        assert instance["realized_files"][0]["file_id"].endswith(f"@{INITIAL_SPEC_VERSION}")
        assert _validate(instance, INSTANCE_SCHEMA) == []

    def test_non_conformant_file_id_fixture_validates(self):
        v13 = _baseline_v13()
        v13["files"][0]["file_id"] = "human-label"  # non-conformant
        _, instance, r = self._migrate(v13)
        assert instance["realized_files"][0]["file_id"].startswith("f-")
        assert any("non-conformant" in w for w in r.warnings)
        assert _validate(instance, INSTANCE_SCHEMA) == []

    def test_bot_guidance_extraction_validates(self):
        v13 = _baseline_v13() | {
            "build_spec": "## Section A\n\nDo A.\n\n## Section B\n\nDo B.",
        }
        spec, _, r = self._migrate(v13)
        guidance = spec.get("bot_guidance", [])
        assert len(guidance) == 2
        assert guidance[0]["section"] == "## Section A"
        assert _validate(spec, SPEC_SCHEMA) == []


# ── provenance.py extension tests ─────────────────────────────────────────────

class TestProvenanceKeywordExtension:
    """Step 0 Session 1 work: provenance.py must accept both 'pkg=' and 'spec='."""

    def test_v6_pkg_form_parses_with_keyword(self):
        m = _parse_marker_str("# evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15-1.3")
        assert m is not None
        assert m.keyword == "pkg"
        assert m.pkg_ids == ["p-a3f91c8b"]
        # v7 aliases work
        assert m.spec_ids == ["p-a3f91c8b"]
        assert m.spec_refs == m.pkg_refs

    def test_v7_spec_form_parses(self):
        m = _parse_marker_str("# evolve: spec=p-a3f91c8b@2026.05.20-1.0 file=f-d4e8f901@2026.05.20-1.0")
        assert m is not None
        assert m.keyword == "spec"
        assert m.spec_ids == ["p-a3f91c8b"]

    def test_format_default_is_pkg(self):
        s = format_marker_string(["p-a3f91c8b"], "f-d4e8f901")
        assert s.startswith("evolve: pkg=")

    def test_format_keyword_spec(self):
        s = format_marker_string(["p-a3f91c8b"], "f-d4e8f901", keyword="spec")
        assert s.startswith("evolve: spec=")

    def test_format_invalid_keyword_raises(self):
        with pytest.raises(ValueError, match="must be 'pkg' or 'spec'"):
            format_marker_string(["p-a3f91c8b"], "f-d4e8f901", keyword="bogus")

    def test_roundtrip_v7(self):
        s = format_marker_string(
            ["p-a3f91c8b"],
            "f-d4e8f901",
            pkg_versions={"p-a3f91c8b": "2026.05.20-1.0"},
            file_version="2026.05.20-1.0",
            keyword="spec",
        )
        m = _parse_marker_str(f"# {s}")
        assert m is not None
        assert m.keyword == "spec"
        assert m.pkg_version("p-a3f91c8b") == "2026.05.20-1.0"

    def test_shared_file_with_multiple_pkg_ids(self):
        m = _parse_marker_str("# evolve: spec=p-aaa11111,p-bbb22222 file=f-ccc33333")
        assert m is not None
        assert m.spec_ids == ["p-aaa11111", "p-bbb22222"]


# ── Empty Lessons stub ────────────────────────────────────────────────────────

class TestLessonsStub:
    """Empty Lessons stub created at migration must validate against the schema."""

    def test_empty_stub_validates(self, tmp_path: Path):
        from evolve_admin.applications.migrate_v7 import _empty_lessons_stub
        (tmp_path / "network.json").write_text(json.dumps({"networkId": "pod-test"}))
        lessons = _empty_lessons_stub("p-abc12345", "testbot", tmp_path)
        assert _validate(lessons, LESSONS_SCHEMA) == []
        assert lessons["source_pod_id"] == "pod-test"


# ── Session 2: legacy pkg_id preservation ─────────────────────────────────────

class TestLegacyPkgIdPreservation:
    """spec_id should preserve the legacy v13 pkg_id when conformant (spec §10.1)."""

    def _result(self):
        return MigrationResult(source_path=Path("/tmp/x"), dry_run=True)

    def test_conformant_legacy_preserved(self):
        r = self._result()
        out = _resolve_spec_id({"pkg_id": "p-c4b4cd0a"}, r)
        assert out == "p-c4b4cd0a"
        assert not r.warnings

    def test_missing_pkg_id_minted_silently(self):
        r = self._result()
        out = _resolve_spec_id({}, r)
        assert out.startswith("p-") and len(out) == 10
        assert not r.warnings

    def test_empty_pkg_id_minted_silently(self):
        r = self._result()
        out = _resolve_spec_id({"pkg_id": ""}, r)
        assert out.startswith("p-")
        assert not r.warnings

    def test_non_conformant_pkg_id_minted_with_warning(self):
        r = self._result()
        # Real-world: pkg_id was sometimes a free-form string
        out = _resolve_spec_id({"pkg_id": "WRONG-FORMAT"}, r)
        assert out.startswith("p-") and len(out) == 10
        assert any("non-conformant" in w or "doesn't match" in w for w in r.warnings)

    def test_end_to_end_legacy_carries_to_spec(self):
        v13 = {
            "id": "app", "name": "App", "bot_id": "team_bot_a", "description": "x",
            "status": "active", "schema_version": 13,
            "pkg_id": "p-a3f91c8b", "files": [], "crons": [],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert spec["spec_id"] == "p-a3f91c8b"


# ── Session 2: v4 (list[str]) file-shape compatibility ─────────────────────────

class TestV4FileShapeCompat:
    """v13 manifests carry files as either list[str] (v4) or list[dict] (v5+)."""

    def test_normalize_str_entry(self):
        out = _normalize_v13_file_entry("tools/x.py")
        assert out == {"path": "tools/x.py", "description": "", "file_id": ""}

    def test_normalize_dict_entry(self):
        original = {"path": "a.py", "file_id": "f-12345678", "description": "y"}
        out = _normalize_v13_file_entry(original)
        assert out is original  # pass-through

    def test_normalize_garbage_entry(self):
        # Real-world manifests sometimes have None or numbers; don't crash
        out = _normalize_v13_file_entry(None)
        assert out == {"path": "", "description": "", "file_id": ""}
        out2 = _normalize_v13_file_entry(42)
        assert out2["path"] == ""

    def test_v4_shape_migrates_clean(self):
        # docx-generator.json shape from admin_bot
        v13 = {
            "id": "tool", "name": "Tool", "bot_id": "admin_bot", "description": "x",
            "status": "active", "schema_version": 13,
            "files": ["tools/x.py", "example/y.py"],
            "crons": [],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        instance_id = _new_instance_id()
        spec = _extract_spec(v13, spec_id, result)
        instance = _extract_instance(v13, instance_id, spec_id, "admin_bot", result)
        assert _validate(spec, SPEC_SCHEMA) == []
        assert _validate(instance, INSTANCE_SCHEMA) == []
        assert len(instance["realized_files"]) == 2


# ── Session 2: rewrite_markers ────────────────────────────────────────────────

class TestRewriteMarkers:
    """rewrite_markers converts v6 pkg= → v7 spec= with version stamps."""

    def _make_file(self, path: Path, marker: str, body: str = 'print("hi")\n') -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/usr/bin/env python3\n# {marker}\n\n{body}")
        return path

    def test_dry_run_counts_but_does_not_write(self, tmp_path: Path):
        f = self._make_file(
            tmp_path / "scripts" / "x.py",
            "evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15-1.3",
        )
        before = f.read_text()
        count = rewrite_markers(tmp_path, spec_id_map={}, dry_run=True)
        assert count == 1
        assert f.read_text() == before  # unchanged

    def test_real_run_switches_pkg_to_spec(self, tmp_path: Path):
        f = self._make_file(
            tmp_path / "scripts" / "x.py",
            "evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15-1.3",
        )
        warnings = []
        count = rewrite_markers(tmp_path, spec_id_map={}, dry_run=False, warnings=warnings)
        assert count == 1
        assert not warnings
        m = parse_marker(f)
        assert m.keyword == "spec"
        assert m.pkg_ids == ["p-a3f91c8b"]
        assert m.file_id == "f-d4e8f901"
        # Versions normalized to INITIAL_SPEC_VERSION
        assert INITIAL_SPEC_VERSION in m.file_ref

    def test_idempotent_second_run_skips(self, tmp_path: Path):
        f = self._make_file(
            tmp_path / "x.py",
            "evolve: pkg=p-a3f91c8b file=f-d4e8f901",
        )
        rewrite_markers(tmp_path, spec_id_map={}, dry_run=False)
        # second run sees v7 form and skips
        count2 = rewrite_markers(tmp_path, spec_id_map={}, dry_run=False)
        assert count2 == 0

    def test_spec_id_map_translates(self, tmp_path: Path):
        f = self._make_file(
            tmp_path / "x.py",
            "evolve: pkg=p-old11111 file=f-d4e8f901",
        )
        count = rewrite_markers(
            tmp_path,
            spec_id_map={"p-old11111": "p-new22222"},
            dry_run=False,
        )
        assert count == 1
        m = parse_marker(f)
        assert m.pkg_ids == ["p-new22222"]

    def test_unmarker_file_skipped(self, tmp_path: Path):
        f = tmp_path / "plain.py"
        f.write_text("print('no marker here')\n")
        count = rewrite_markers(tmp_path, spec_id_map={}, dry_run=False)
        assert count == 0

    def test_skip_dirs_honored(self, tmp_path: Path):
        # File inside __pycache__ should be skipped even if it has a marker
        f = self._make_file(
            tmp_path / "__pycache__" / "cached.py",
            "evolve: pkg=p-a3f91c8b file=f-d4e8f901",
        )
        count = rewrite_markers(tmp_path, spec_id_map={}, dry_run=False)
        assert count == 0
        # Marker untouched
        assert "pkg=" in f.read_text()

    def test_shared_file_multiple_pkg_ids(self, tmp_path: Path):
        f = self._make_file(
            tmp_path / "shared.py",
            "evolve: pkg=p-aaa11111,p-bbb22222 file=f-ccc33333",
        )
        count = rewrite_markers(tmp_path, spec_id_map={}, dry_run=False)
        assert count == 1
        m = parse_marker(f)
        assert m.keyword == "spec"
        assert set(m.pkg_ids) == {"p-aaa11111", "p-bbb22222"}

    def test_json_file_marker_rewrite(self, tmp_path: Path):
        """JSON files carry the marker as a _evolve key, not a comment line."""
        f = tmp_path / "config.json"
        f.write_text(json.dumps({
            "_evolve": {"pkg": "p-a3f91c8b@2026.04.15-1.3", "file": "f-d4e8f901@2026.04.15-1.3"},
            "real_data": [1, 2, 3],
        }))
        count = rewrite_markers(tmp_path, spec_id_map={}, dry_run=False)
        assert count == 1
        data = json.loads(f.read_text())
        assert "spec" in data["_evolve"]
        assert "pkg" not in data["_evolve"]
        assert data["real_data"] == [1, 2, 3]  # preserved


# ── Session 2.5: app_dependencies dict shape (ea-pack) ────────────────────────

class TestAppDependenciesDictShape:
    """v13 app_dependencies is list[str] OR list[dict] (ea-pack carries dicts)."""

    def test_bare_string_entries_preserved(self):
        out = _migrate_app_dependencies(["p-a3f91c8b", "p-b2e04d1a"])
        assert out == [
            {"spec_id": "p-a3f91c8b", "required": True},
            {"spec_id": "p-b2e04d1a", "required": True},
        ]

    def test_non_conformant_string_filtered_out(self):
        # Garbage in v13 (e.g., a display name accidentally in the field)
        out = _migrate_app_dependencies(["p-a3f91c8b", "Task Manager", "p-WRONG"])
        assert out == [{"spec_id": "p-a3f91c8b", "required": True}]

    def test_dict_shape_ea_pack(self):
        # Real shape from gallery/ea-pack/p-aab5e569.json
        raw = [{
            "pkg_id": "p-9bfa1c84",
            "display_name": "Task Manager",
            "required": True,
            "reason": "Morning brief reads tasks.json from the task manager.",
        }]
        out = _migrate_app_dependencies(raw)
        assert len(out) == 1
        assert out[0]["spec_id"] == "p-9bfa1c84"
        assert out[0]["required"] is True
        assert "Morning brief" in out[0]["purpose"]

    def test_dict_with_spec_id_key(self):
        # Some shapes use spec_id directly
        raw = [{"spec_id": "p-12345678", "required": False, "purpose": "Optional integration"}]
        out = _migrate_app_dependencies(raw)
        assert out == [
            {"spec_id": "p-12345678", "required": False, "purpose": "Optional integration"}
        ]

    def test_dict_missing_pkg_id_filtered(self):
        # Dict with no pkg_id or spec_id is dropped
        raw = [{"display_name": "ghost", "required": True}]
        out = _migrate_app_dependencies(raw)
        assert out == []

    def test_dict_non_conformant_pkg_id_filtered(self):
        raw = [{"pkg_id": "Task Manager", "required": True}]
        out = _migrate_app_dependencies(raw)
        assert out == []

    def test_dict_no_reason_synthesizes_purpose(self):
        # When reason/purpose missing, build one from display_name
        raw = [{"pkg_id": "p-12345678", "display_name": "Pizza Oven"}]
        out = _migrate_app_dependencies(raw)
        assert len(out) == 1
        assert "Pizza Oven" in out[0]["purpose"]

    def test_end_to_end_ea_pack_shape_migrates_clean(self):
        # Reproduces the actual mini crash
        v13 = {
            "id": "ea-pack", "name": "EA Pack", "bot_id": "evolve",
            "description": "Executive Assistant pack", "status": "active",
            "schema_version": 13,
            "pkg_id": "p-aab5e569",
            "files": [],
            "crons": [],
            "app_dependencies": [{
                "pkg_id": "p-9bfa1c84",
                "display_name": "Task Manager",
                "required": True,
                "reason": "Morning brief reads tasks.json from the task manager.",
            }],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert _validate(spec, SPEC_SCHEMA) == []
        # The dependency carries through
        assert len(spec["dependencies"]["apps"]) == 1
        assert spec["dependencies"]["apps"][0]["spec_id"] == "p-9bfa1c84"


# ── Session 2.6: backup + rollback ─────────────────────────────────────────────

class TestBackupRun:
    """BackupRun captures destructive ops for safe --apply with auto-rollback."""

    def test_create_makes_dir_structure(self, tmp_path: Path):
        run = BackupRun.create(tmp_path)
        assert run.backup_dir.exists()
        assert run.backup_dir.parent.name == BACKUP_MANIFEST_VERSION
        assert (run.backup_dir / "originals").is_dir()
        assert run.timestamp  # populated

    def test_backup_source_copies_to_originals(self, tmp_path: Path):
        run = BackupRun.create(tmp_path)
        src = tmp_path / "src.json"
        src.write_text(json.dumps({"hello": "world"}))
        backup_path = run.backup_source(src)
        assert backup_path.exists()
        assert backup_path.parent.name == "originals"
        assert json.loads(backup_path.read_text()) == {"hello": "world"}

    def test_record_writes_manifest_after_each_op(self, tmp_path: Path):
        run = BackupRun.create(tmp_path)
        target = tmp_path / "new.json"
        run.record_creation(target, {"kind": "test"})
        manifest = json.loads((run.backup_dir / "manifest.json").read_text())
        assert len(manifest["operations"]) == 1
        assert manifest["operations"][0]["action"] == "delete"
        assert manifest["operations"][0]["target"] == str(target)

    def test_record_unlink_with_backup_reference(self, tmp_path: Path):
        run = BackupRun.create(tmp_path)
        src = tmp_path / "src.json"
        src.write_text("{}")
        bp = run.backup_source(src)
        run.record_unlink(src, bp, {"kind": "v13_source"})
        manifest = json.loads((run.backup_dir / "manifest.json").read_text())
        assert manifest["operations"][0]["action"] == "restore"
        assert "originals/" in manifest["operations"][0]["backup"]


class TestRollback:
    """rollback_migration reverses operations in LIFO order."""

    def _seed_backup(self, tmp_path: Path) -> tuple[BackupRun, Path, Path]:
        """Build a minimal backup scenario: 1 created file + 1 unlinked source."""
        run = BackupRun.create(tmp_path)
        # Simulate a v13 source that the migration unlinked
        v13_src = tmp_path / "team_bot_a-manifests" / "journal.json"
        v13_src.parent.mkdir(parents=True)
        v13_src.write_text(json.dumps({"id": "journal", "schema_version": 13}))
        bp = run.backup_source(v13_src)
        v13_src.unlink()
        run.record_unlink(v13_src, bp, {"kind": "v13_source"})
        # Simulate a v7 instance file the migration created
        new_inst = tmp_path / "team_bot_a-manifests" / "i-abcd1234.json"
        new_inst.write_text(json.dumps({"instance_id": "i-abcd1234"}))
        run.record_creation(new_inst, {"kind": "instance"})
        return run, v13_src, new_inst

    def test_rollback_restores_unlinked_and_deletes_created(self, tmp_path: Path):
        run, v13_src, new_inst = self._seed_backup(tmp_path)
        # Pre-state: original gone, new exists
        assert not v13_src.exists()
        assert new_inst.exists()
        res = rollback_migration(tmp_path, run.timestamp, dry_run=False)
        # Post-state: original back, new gone
        assert v13_src.exists()
        assert json.loads(v13_src.read_text())["id"] == "journal"
        assert not new_inst.exists()
        assert res.restored == 1
        assert res.deleted == 1

    def test_rollback_dry_run_does_not_modify(self, tmp_path: Path):
        run, v13_src, new_inst = self._seed_backup(tmp_path)
        res = rollback_migration(tmp_path, run.timestamp, dry_run=True)
        # Counts as if it would happen
        assert res.restored == 1
        assert res.deleted == 1
        # But files unchanged
        assert not v13_src.exists()
        assert new_inst.exists()

    def test_rollback_idempotent_after_full_restore(self, tmp_path: Path):
        run, v13_src, new_inst = self._seed_backup(tmp_path)
        rollback_migration(tmp_path, run.timestamp, dry_run=False)
        # Second rollback: everything already in target state
        res = rollback_migration(tmp_path, run.timestamp, dry_run=False)
        assert res.restored == 0  # already restored
        assert res.deleted == 0   # already deleted
        assert len(res.skipped) == 2

    def test_rollback_missing_backup_recorded(self, tmp_path: Path):
        run, v13_src, new_inst = self._seed_backup(tmp_path)
        # Remove the backup file to simulate corruption
        for bp in (run.backup_dir / "originals").iterdir():
            bp.unlink()
        res = rollback_migration(tmp_path, run.timestamp, dry_run=False)
        # delete op still runs (new_inst removed); restore can't, recorded missing
        assert res.deleted == 1
        assert res.restored == 0
        assert len(res.missing) == 1

    def test_rollback_unknown_timestamp_raises(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="can't read backup manifest"):
            rollback_migration(tmp_path, "nonexistent", dry_run=False)

    def test_gallery_migration_records_creation_for_rollback(self, tmp_path: Path):
        """Defense-in-depth: gallery promotions must be tracked so rollback
        cleans them up. Without backup tracking, a partial-failure migration
        left orphan Specs in gallery/builtin/ after rollback."""
        from evolve_admin.applications.migrate_v7 import migrate_gallery_package

        # Seed a fake gallery package + shared_dir
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "network.json").write_text(json.dumps({"networkId": "pod-test"}))
        pkg_path = tmp_path / "fake-gallery-pkg.json"
        pkg_path.write_text(json.dumps({
            "id": "x", "name": "X", "bot_id": "team_bot_a", "description": "y",
            "status": "active", "schema_version": 13,
            "pkg_id": "p-abcd1234", "files": [], "crons": [],
        }))

        run = BackupRun.create(shared_dir)
        res = migrate_gallery_package(pkg_path, shared_dir, dry_run=False, backup=run)
        assert res.succeeded, res.errors
        assert res.spec_path.exists()

        # Rollback removes the gallery Spec
        rb = rollback_migration(shared_dir, run.timestamp, dry_run=False)
        assert rb.deleted >= 1
        assert not res.spec_path.exists()


class TestMigrateInstanceWithBackup:
    """End-to-end: migrate_instance with backup, then rollback, recovers original."""

    def _v13(self) -> dict:
        return {
            "id": "journal",
            "name": "Journal",
            "bot_id": "personal_bot",  # use personal_bot to avoid bot_home complications
            "description": "Daily journal",
            "status": "active",
            "schema_version": 13,
            "pkg_id": "p-abcd1234",
            "files": [],
            "crons": [],
        }

    def test_migrate_then_rollback_recovers_original(self, tmp_path: Path, monkeypatch):
        # Stand up a fake bot workspace + shared_dir under tmp_path.
        # Lessons no longer live in the bot workspace — they go under
        # {shared_dir}/lessons/<bot_id>/ which the migration creates on demand.
        bot_workspace = tmp_path / "bot-home" / ".openclaw" / "workspace"
        (bot_workspace / "manifests").mkdir(parents=True)
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "network.json").write_text(json.dumps({"networkId": "pod-test"}))

        # Monkey-patch bot_home to return our fake workspace dir
        from evolve_admin.applications import migrate_v7 as mv7
        monkeypatch.setattr(mv7, "bot_home", lambda _: tmp_path / "bot-home")

        # Seed a v13 manifest
        v13_path = bot_workspace / "manifests" / "journal.json"
        v13_data = self._v13()
        v13_path.write_text(json.dumps(v13_data))

        # Create backup + migrate
        backup = BackupRun.create(shared_dir)
        result = migrate_instance(
            v13_path, shared_dir, "personal_bot", dry_run=False, backup=backup
        )
        assert result.succeeded, result.errors

        # Post-migrate state
        assert not v13_path.exists()
        assert result.instance_path.exists()
        assert result.spec_path.exists()
        assert result.lessons_path.exists()
        # Lessons land under shared_dir, not the bot workspace
        assert str(result.lessons_path).startswith(str(shared_dir / "lessons"))
        assert f"/lessons/personal_bot/" in str(result.lessons_path)

        # Rollback
        rb = rollback_migration(shared_dir, backup.timestamp, dry_run=False)
        assert rb.restored >= 1  # v13 source
        assert rb.deleted >= 3   # spec, instance, lessons

        # Post-rollback: original back, v7 artifacts gone
        assert v13_path.exists()
        assert json.loads(v13_path.read_text())["id"] == "journal"
        assert not result.instance_path.exists()
        assert not result.spec_path.exists()
        assert not result.lessons_path.exists()


class TestMigrateAllSkipsHiddenAndUnderscoreFiles:
    """S2.10 — migrate_all's manifest_dir.glob('*.json') must skip .scan-status.json
    and _history/_*.json that share the directory with real manifests. The first
    real-pod --apply migrated 7 .scan-status.json files because this filter was
    missing; this test prevents recurrence."""

    def test_dotfile_and_underscore_files_skipped(self, tmp_path: Path, monkeypatch):
        from evolve_admin.applications import migrate_v7 as mv7

        bot_home_dir = tmp_path / "bot-home"
        manifests = bot_home_dir / ".openclaw" / "workspace" / "manifests"
        manifests.mkdir(parents=True)
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "network.json").write_text(
            json.dumps({"networkId": "pod-test", "bots": {"personal_bot": {}}})
        )

        monkeypatch.setattr(mv7, "bot_home", lambda _bid: bot_home_dir)

        # Real v13 manifest (should migrate)
        (manifests / "journal.json").write_text(json.dumps({
            "id": "journal", "name": "Journal", "bot_id": "personal_bot",
            "description": ".", "status": "active", "schema_version": 13,
            "pkg_id": "p-abcd1234", "files": [], "crons": [],
        }))
        # Scanner state (must be skipped)
        (manifests / ".scan-status.json").write_text(
            json.dumps({"last_scan_at": "2026-05-22T00:00:00Z"})
        )
        # Underscore-prefixed (defensive — _history/ won't match *.json itself
        # but a stray _whatever.json must also be skipped)
        (manifests / "_history-backup.json").write_text(json.dumps({"old": True}))

        agg = mv7.migrate_all(
            shared_dir=shared_dir, bot_ids=["personal_bot"], dry_run=True,
        )
        # Only the one real manifest should be migrated
        assert len(agg.instance_results) == 1, [
            (r.source_path.name, r.errors) for r in agg.instance_results
        ]
        assert agg.instance_results[0].source_path.name == "journal.json"


class TestExtractSpecPreservesV13Identity:
    """S2.12 — v13's `identity` block carries richer prose (purpose,
    scope_includes/excludes, user) that v7's `objective` doesn't capture.
    Migration must preserve it so the UI's view modal stays populated."""

    def test_identity_block_passed_through(self):
        v13 = _baseline_v13() | {
            "identity": {
                "purpose": "Track session activity for accountability + debugging",
                "scope_includes": ["session start/end", "tool calls", "errors"],
                "scope_excludes": ["network capture"],
                "user": "team_bot_a bot operator",
            },
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert spec["identity"]["purpose"].startswith("Track session activity")
        assert "tool calls" in spec["identity"]["scope_includes"]

    def test_missing_identity_omits_field(self):
        v13 = _baseline_v13()  # no identity
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert "identity" not in spec


class TestExtractSpecPreservesV13Description:
    """v13's `description` is the operator-authored summary the UI shows on
    app cards and tiles. Migration must preserve it so the hydrated v7-arc
    Instance has something to display — otherwise the server's only fallback
    is `identity.purpose`, which doesn't exist on every Spec."""

    def test_description_passed_through(self):
        v13 = _baseline_v13() | {
            "description": "Daily CVE scan: LLM web search + Python finalizer dispatches alerts.",
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert spec["description"].startswith("Daily CVE scan")

    def test_missing_description_omits_field(self):
        v13 = _baseline_v13() | {"description": ""}  # empty falsy
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert "description" not in spec


class TestSuccessCriteriaPreservesV13Fields:
    """S2.12 — real v13 manifests use observable_outcomes / failure_signals /
    minimum_bar inside success_criteria. The original migration only copied
    behavioral + observable, silently dropping the rest."""

    def test_observable_outcomes_alias_falls_back_to_observable(self):
        # v13 used "observable_outcomes"; v7 canonical is "observable".
        # Migration should populate "observable" from observable_outcomes.
        v13 = _baseline_v13() | {
            "success_criteria": {
                "observable_outcomes": ["sub-500ms p99", "no orphan sessions"],
            },
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert "sub-500ms p99" in spec["success_criteria"]["observable"]
        # And keep the alias for v13-shape consumers
        assert "observable_outcomes" in spec["success_criteria"]

    def test_failure_signals_and_minimum_bar_preserved(self):
        v13 = _baseline_v13() | {
            "success_criteria": {
                "behavioral": ["logs everything"],
                "failure_signals": ["missing entries", "duplicate session_ids"],
                "minimum_bar": "Every session has at least one log entry",
            },
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        sc = spec["success_criteria"]
        assert sc["failure_signals"] == ["missing entries", "duplicate session_ids"]
        assert sc["minimum_bar"] == "Every session has at least one log entry"
        assert sc["behavioral"] == ["logs everything"]


class TestExtractSpecPreservesHighContentFields:
    """S2.13 — audit of 63 real test-pod manifests showed these fields carry
    operator-authored content in 8-78% of cases. Migration must preserve them."""

    def test_constraints_full_block_passed_through(self):
        # Real-world: admin_bot's job_search app has constraints.privacy (handled
        # by _infer_privacy) plus constraints.safety / .dependencies / etc.
        # 78% of real manifests populate non-privacy subfields.
        v13 = _baseline_v13() | {
            "constraints": {
                "privacy": ["No PII outside private/"],
                "safety": ["never delete jobs", "never modify resumes"],
                "dependencies": ["resume.docx exists"],
                "boundaries": ["resume + cover letter only"],
            },
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        # Full constraints block survives (privacy is also separately consolidated
        # into the top-level `privacy` block but the full dict stays addressable)
        assert spec["constraints"]["safety"] == ["never delete jobs", "never modify resumes"]
        assert spec["constraints"]["dependencies"] == ["resume.docx exists"]
        assert spec["constraints"]["boundaries"] == ["resume + cover letter only"]

    def test_test_cases_preserved(self):
        v13 = _baseline_v13() | {
            "test_cases": [
                {"trigger": "I applied to Acme for SE", "expected": "Logged with company, role, date"},
                {"trigger": "show jobs", "expected": "List sorted by date desc"},
            ],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert len(spec["test_cases"]) == 2
        assert spec["test_cases"][0]["trigger"].startswith("I applied to Acme")

    def test_example_triggers_preserved(self):
        v13 = _baseline_v13() | {
            "example_triggers": [
                "I just applied to a SE role at Company X",
                "Update my resume with a new project",
            ],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert spec["example_triggers"][0].startswith("I just applied")

    def test_owner_inputs_outputs_preserved(self):
        v13 = _baseline_v13() | {
            "owner": "admin_bot",
            "inputs": ["Gmail OAuth token", "message ID"],
            "outputs": ["parsed body", "decoded attachments"],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert spec["owner"] == "admin_bot"
        assert "Gmail OAuth token" in spec["inputs"]
        assert "parsed body" in spec["outputs"]

    def test_scheduled_actions_preserved(self):
        v13 = _baseline_v13() | {
            "scheduled_actions": [{
                "id": "protein-6pm-tally",
                "trigger": {"kind": "heartbeat", "schedule": "18:00 daily"},
                "summary": "Daily protein tally at 6pm",
            }],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        assert spec["scheduled_actions"][0]["id"] == "protein-6pm-tally"

    def test_empty_fields_not_added(self):
        # When a field is missing or falsy, don't pollute the Spec with empties
        v13 = _baseline_v13()
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        spec = _extract_spec(v13, spec_id, result)
        for k in ("constraints", "test_cases", "example_triggers",
                  "owner", "inputs", "outputs", "scheduled_actions"):
            assert k not in spec, f"{k} should not appear when v13 has no value"


class TestExtractInstancePreservesV13Fields:
    """S2.13 — Instance-side fields: evidence_files, audit telemetry, audit_trail_path."""

    def test_evidence_files_passed_through(self):
        v13 = _baseline_v13() | {
            "evidence_files": [
                "scripts/journal.py",
                "tests/test_journal.py",
                "HEARTBEAT.md",
            ],
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        instance_id = _new_instance_id()
        instance = _extract_instance(v13, instance_id, spec_id, "team_bot_a", result)
        assert "scripts/journal.py" in instance["evidence_files"]
        assert len(instance["evidence_files"]) == 3

    def test_audit_stamps_passed_through(self):
        v13 = _baseline_v13() | {
            "last_audit": {"verified_at": "2026-05-22T10:00:00Z", "status": "ok"},
            "last_structural_verify": {"verified_at": "2026-05-22T09:00:00Z",
                                        "status": "ok_with_minor"},
            "audit_trail_path": "/Users/team_bot_a/.openclaw/workspace/evolve/audits/journal/trail.jsonl",
        }
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        instance_id = _new_instance_id()
        instance = _extract_instance(v13, instance_id, spec_id, "team_bot_a", result)
        assert instance["last_audit"]["status"] == "ok"
        assert instance["last_structural_verify"]["status"] == "ok_with_minor"
        assert instance["audit_trail_path"].endswith("trail.jsonl")

    def test_empty_v13_fields_omitted(self):
        v13 = _baseline_v13()
        result = MigrationResult(source_path=Path("/tmp/x"), dry_run=True)
        spec_id = _resolve_spec_id(v13, result)
        instance_id = _new_instance_id()
        instance = _extract_instance(v13, instance_id, spec_id, "team_bot_a", result)
        for k in ("evidence_files", "last_audit",
                  "last_structural_verify", "audit_trail_path"):
            assert k not in instance, f"{k} should not appear when v13 has no value"


# ─────────────────────────────────────────────────────────────────────────────
# S2.14: post-migration chown back to evolve user
#
# When --apply runs as root (required since S2.8 because bots' workspace ACLs
# are inconsistent), any new files/dirs under {shared_dir}/ land root-owned.
# The admin server runs as evolve and writes there for share, Lessons
# compression, and Reflect. Without this chown the share endpoint silently
# 500s on first use — we hit this live and had to fix by hand.
# ─────────────────────────────────────────────────────────────────────────────


class TestChownPathsToEvolve:
    """Most behavior here can't be exercised on the dev laptop (no root, no
    evolve user). These tests pin the guards that protect the dev path."""

    def test_noop_when_not_root(self, tmp_path: Path):
        """We're not running as root in CI — function must return 0 cleanly."""
        target = tmp_path / "x"
        target.mkdir()
        assert _chown_paths_to_evolve([target]) == 0

    def test_noop_when_evolve_user_missing(self, tmp_path: Path, monkeypatch):
        """Pretend we're root but the host has no 'evolve' user (dev laptop).
        Function must warn + return 0, not crash."""
        import pwd
        monkeypatch.setattr("os.geteuid", lambda: 0)

        def _fake_getpwnam(name):
            raise KeyError(name)
        monkeypatch.setattr(pwd, "getpwnam", _fake_getpwnam)

        warnings: list[str] = []
        target = tmp_path / "x"
        target.mkdir()
        assert _chown_paths_to_evolve([target], log=warnings.append) == 0
        assert any("evolve" in w and "not found" in w for w in warnings)

    def test_skips_missing_paths(self, tmp_path: Path):
        """Gallery-only or bot-only runs may not touch every path. Function
        must skip paths that don't exist without crashing."""
        # Not-root path: just verify the early-return branch handles a mix.
        ghost = tmp_path / "does" / "not" / "exist"
        assert _chown_paths_to_evolve([ghost]) == 0

    def test_simulated_root_walks_tree(self, tmp_path: Path, monkeypatch):
        """Pretend we're root *and* evolve exists; verify the walk visits
        every file. Use the real test user's uid so os.chown is a no-op
        in practice (chown to self) and doesn't need elevated permissions."""
        import pwd
        import grp

        # Use the real current user so the chown call actually succeeds
        # (chown-to-self requires no privilege). We just want to verify the
        # walk covers nested files.
        real_uid = os.getuid()
        real_gid = os.getgid()
        monkeypatch.setattr("os.geteuid", lambda: 0)

        class _FakePwEntry:
            pw_uid = real_uid

        class _FakeGrpEntry:
            gr_gid = real_gid

        monkeypatch.setattr(pwd, "getpwnam",
                            lambda name: _FakePwEntry if name == "evolve" else (_ for _ in ()).throw(KeyError(name)))
        monkeypatch.setattr(grp, "getgrnam",
                            lambda name: _FakeGrpEntry if name == "wheel" else (_ for _ in ()).throw(KeyError(name)))

        # Build a small tree
        root = tmp_path / "gallery" / "local"
        root.mkdir(parents=True)
        (root / "a.json").write_text("{}")
        sub = root / "p-aaaa1111"
        sub.mkdir()
        (sub / "v1.json").write_text("{}")
        (sub / "v2.json").write_text("{}")

        chowned = _chown_paths_to_evolve([root])
        # 1 root dir + 1 top file + 1 subdir + 2 files = 5
        assert chowned == 5


# ── Schema-5 gallery enrichment (Tier-A publisher fix) ────────────────────────
#
# The gallery's in-repo schema-5 specs (e.g. gallery/email-integration/p-*.json)
# leave the v13 top-level `files[]` and `dependencies[]` fields empty and put
# their roster in `interface_contract` + `build_spec` plus their integrations in
# `requirements.integrations[]` (Atlas-shape lineage). The migrator's earlier
# `_build_blueprint` / `_build_dependencies` only read the empty v13 fields,
# producing empty v7 blueprints + empty integrations for every gallery Spec.
# These tests pin the new readers that pick up the schema-5 enrichments.

class TestBuildIntegrationsFromRequirements:
    """`requirements.integrations[]` → `dependencies.integrations[]`."""

    def test_minimal_entry(self):
        v13 = {"requirements": {"integrations": [{"id": "gmail"}]}}
        out = _build_integrations(v13)
        assert out == [{
            "integration_id": "gmail",
            "scopes": [],
            "required": True,
            "purpose": "Required by app (integration: gmail).",
        }]

    def test_full_schema5_entry(self):
        v13 = {"requirements": {"integrations": [{
            "id": "google_calendar",
            "display_name": "Google Calendar",
            "required": True,
            "check_path": "openclaw.json → integrations.google_calendar",
            "setup_doc": "docs/integrations/google-calendar.md",
            "reason": "Reads calendar events via Google Calendar API",
        }]}}
        out = _build_integrations(v13)
        assert len(out) == 1
        e = out[0]
        assert e["integration_id"] == "google_calendar"
        assert e["required"] is True
        assert e["scopes"] == []
        assert "Reads calendar events" in e["purpose"]
        assert "openclaw.json → integrations.google_calendar" in e["purpose"]
        assert "docs/integrations/google-calendar.md" in e["purpose"]

    def test_required_false_passthrough(self):
        v13 = {"requirements": {"integrations": [{"id": "github", "required": False}]}}
        out = _build_integrations(v13)
        assert out[0]["required"] is False

    def test_scopes_from_required_scopes(self):
        v13 = {"requirements": {"integrations": [{
            "id": "gmail",
            "required_scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        }]}}
        out = _build_integrations(v13)
        assert out[0]["scopes"] == [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ]

    def test_alternatives_summarized_into_purpose(self):
        v13 = {"requirements": {"integrations": [{
            "id": "gmail",
            "reason": "Reads email via Gmail API",
            "alternatives": [
                {"id": "google_path_c", "reason": "service-account auth"},
                {"id": "google_path_b", "reason": "delegated user"},
            ],
        }]}}
        out = _build_integrations(v13)
        assert "Alternatives:" in out[0]["purpose"]
        assert "google_path_c" in out[0]["purpose"]
        assert "google_path_b" in out[0]["purpose"]
        # Alternatives are NOT forked into separate integrations.
        assert len(out) == 1

    def test_alternatives_drop_malformed_entries(self):
        v13 = {"requirements": {"integrations": [{
            "id": "gmail",
            "alternatives": [
                {"id": ""},                  # blank id — drop
                "not-a-dict",                # wrong type — drop
                {"id": "google_path_c"},     # keep
            ],
        }]}}
        out = _build_integrations(v13)
        assert "google_path_c" in out[0]["purpose"]
        assert "not-a-dict" not in out[0]["purpose"]

    def test_missing_id_is_skipped(self):
        v13 = {"requirements": {"integrations": [
            {"id": ""},
            {"display_name": "Anonymous"},
            {"id": "gmail"},
        ]}}
        out = _build_integrations(v13)
        assert [e["integration_id"] for e in out] == ["gmail"]

    def test_no_requirements_returns_empty(self):
        assert _build_integrations({}) == []
        assert _build_integrations({"requirements": None}) == []
        assert _build_integrations({"requirements": "string-not-dict"}) == []
        assert _build_integrations({"requirements": {"integrations": None}}) == []

    def test_integrated_into_build_dependencies(self):
        # _build_dependencies must surface the integrations the new helper
        # produces — that's the public path migrate_v7 takes.
        v13 = {"requirements": {"integrations": [{"id": "gmail"}]}}
        deps = _build_dependencies(v13)
        assert len(deps["integrations"]) == 1
        assert deps["integrations"][0]["integration_id"] == "gmail"


class TestBuildBlueprintFromInterfaceContract:
    """Multi-source union: files[] + interface_contract + build_spec ## FILE blocks."""

    def _result(self):
        return MigrationResult(source_path=Path("/tmp/x"), dry_run=True)

    def test_legacy_v13_files_still_work(self):
        # Existing behaviour: v13 top-level files[] populates blueprint.files.
        v13 = {
            "files": [
                {"path": "scripts/run.py", "description": "Main runner"},
                {"path": "data/state.json", "description": "Run state"},
            ],
        }
        bp = _build_blueprint(v13, self._result())
        assert [f["expected_location"] for f in bp["files"]] == [
            "scripts/run.py", "data/state.json",
        ]
        assert bp["files"][0]["role"] == "vital_to_blueprint"
        assert bp["files"][1]["role"] == "instance_specific"
        assert bp["files"][0]["intent"] == "Main runner"

    def test_cli_subcommands_become_one_script_entry(self):
        # interface_contract.cli with 3 subcommands → 1 entry for the script.
        v13 = {
            "interface_contract": {
                "cli": [
                    {"command": "python3 scripts/email_sync.py sync"},
                    {"command": "python3 scripts/email_sync.py unread"},
                    {"command": "python3 scripts/email_sync.py search"},
                ],
            },
        }
        bp = _build_blueprint(v13, self._result())
        paths = [f["expected_location"] for f in bp["files"]]
        assert paths == ["scripts/email_sync.py"]
        intent = bp["files"][0]["intent"]
        assert "sync" in intent and "unread" in intent and "search" in intent

    def test_cli_handles_absolute_interpreter_path(self):
        v13 = {"interface_contract": {"cli": [
            {"command": "/usr/bin/python3 scripts/calendar_summary.py preview"},
        ]}}
        bp = _build_blueprint(v13, self._result())
        assert bp["files"][0]["expected_location"] == "scripts/calendar_summary.py"

    def test_cli_handles_bash_scripts(self):
        v13 = {"interface_contract": {"cli": [
            {"command": "bash scripts/cron.sh"},
        ]}}
        bp = _build_blueprint(v13, self._result())
        assert bp["files"][0]["expected_location"] == "scripts/cron.sh"
        assert bp["files"][0]["role"] == "vital_to_blueprint"

    def test_cli_rejects_non_script_invocations(self):
        # Not all command shapes are launch-an-interpreter — skip when no script path.
        v13 = {"interface_contract": {"cli": [
            {"command": "echo hello"},
            {"command": "evolve-admin deploy --bot=foo"},
        ]}}
        bp = _build_blueprint(v13, self._result())
        assert bp["files"] == []

    def test_data_files_become_instance_specific(self):
        v13 = {"interface_contract": {"data_files": [
            {"path": "memory/email-digest.json", "description": "Recent email digest"},
            {"path": "memory/email/threads/{thread_id}.json", "description": "Full thread"},
        ]}}
        bp = _build_blueprint(v13, self._result())
        paths = [f["expected_location"] for f in bp["files"]]
        assert paths == [
            "memory/email-digest.json",
            "memory/email/threads/{thread_id}.json",
        ]
        for f in bp["files"]:
            assert f["role"] == "instance_specific"
        assert bp["files"][0]["intent"] == "Recent email digest"

    def test_build_spec_file_blocks_picked_up(self):
        v13 = {"build_spec": (
            "## Overview\nA test app.\n\n"
            "## FILE: scripts/cron.sh\n```bash\necho ok\n```\n\n"
            "## FILE: /Library/LaunchDaemons/com.x.test.plist\n```xml\n<plist/>\n```\n"
        )}
        bp = _build_blueprint(v13, self._result())
        paths = [f["expected_location"] for f in bp["files"]]
        assert "scripts/cron.sh" in paths
        assert "/Library/LaunchDaemons/com.x.test.plist" in paths
        # .plist gets vital_to_blueprint (installer artifact).
        plist = next(f for f in bp["files"] if f["expected_location"].endswith(".plist"))
        assert plist["role"] == "vital_to_blueprint"

    def test_all_three_sources_unioned_and_deduped(self):
        # If a path appears in multiple sources, the first wins (files[] beats
        # interface_contract beats build_spec). De-dup preserves order.
        v13 = {
            "files": [
                {"path": "scripts/run.py", "description": "Authoritative description"},
            ],
            "interface_contract": {
                "cli": [
                    {"command": "python3 scripts/run.py go"},
                    {"command": "python3 scripts/other.py foo"},
                ],
                "data_files": [
                    {"path": "data/state.json", "description": "App state"},
                ],
            },
            "build_spec": "## FILE: scripts/cron.sh\nbash cron",
        }
        bp = _build_blueprint(v13, self._result())
        paths = [f["expected_location"] for f in bp["files"]]
        # 4 unique paths; scripts/run.py only appears once.
        assert paths.count("scripts/run.py") == 1
        assert set(paths) == {
            "scripts/run.py", "scripts/other.py", "data/state.json", "scripts/cron.sh",
        }
        # Source 1 wins for intent.
        run_py = next(f for f in bp["files"] if f["expected_location"] == "scripts/run.py")
        assert run_py["intent"] == "Authoritative description"

    def test_empty_sources_yields_empty_blueprint(self):
        bp = _build_blueprint({}, self._result())
        assert bp == {"files": []}

    def test_gallery_shape_yields_non_empty_blueprint(self):
        # End-to-end check on a synthetic schema-5 gallery shape: top-level
        # files[] empty, but interface_contract + build_spec carry the roster.
        # This is the bug we're fixing — pre-fix this produced files==[].
        v13 = {
            "files": [],
            "requirements": {
                "integrations": [{"id": "gmail", "required": True}],
            },
            "interface_contract": {
                "cli": [
                    {"command": "python3 scripts/email_sync.py sync"},
                    {"command": "python3 scripts/email_sync.py unread"},
                ],
                "data_files": [
                    {"path": "memory/email-digest.json", "description": "Digest"},
                ],
            },
            "build_spec": "## FILE: scripts/email-sync-cron.sh\nbash",
        }
        bp = _build_blueprint(v13, self._result())
        paths = [f["expected_location"] for f in bp["files"]]
        assert paths == [
            "scripts/email_sync.py",
            "memory/email-digest.json",
            "scripts/email-sync-cron.sh",
        ]


class TestExtractSpecGallerySchemaFiveEndToEnd:
    """End-to-end: a schema-5 gallery-shape v13 produces a populated v7 Spec."""

    def _schema5_v13(self) -> dict:
        return {
            "pkg_id": "p-aabbccdd",
            "name": "Test App",
            "description": "A schema-5 style gallery spec",
            "objective": "Do the thing",
            "schema_version": 5,
            "files": [],                 # empty — schema-5 lineage hallmark
            "dependencies": [],          # empty
            "requirements": {
                "integrations": [{
                    "id": "gmail",
                    "required": True,
                    "check_path": "openclaw.json → integrations.gmail",
                    "setup_doc": "docs/integrations/gmail.md",
                    "reason": "Reads email via Gmail API",
                }],
            },
            "interface_contract": {
                "cli": [
                    {"command": "python3 scripts/email_sync.py sync"},
                    {"command": "python3 scripts/email_sync.py unread"},
                ],
                "data_files": [
                    {"path": "memory/email-digest.json", "description": "Recent emails"},
                ],
            },
            "build_spec": "## Overview\nIt works.\n\n## FILE: scripts/cron.sh\nbash\n",
            "identity": {
                "purpose": "Pull Gmail into local JSON",
                "scope_includes": ["sync inbox"],
                "user": "personal-bot user",
            },
            "success_criteria": {
                "observable_outcomes": ["email-digest.json updated every 30 min"],
                "failure_signals": ["empty digest after sync"],
            },
            "constraints": {"safety": ["read-only"]},
            "scheduled_actions": [{
                "id": "email-sync",
                "mechanism": "launchd",
                "trigger": {"kind": "launchd"},
            }],
        }

    def test_blueprint_picks_up_all_files(self):
        v13 = self._schema5_v13()
        r = MigrationResult(source_path=Path("x"), dry_run=True)
        spec = _extract_spec(v13, "p-aabbccdd", r)
        paths = [f["expected_location"] for f in spec["blueprint"]["files"]]
        assert paths == [
            "scripts/email_sync.py",
            "memory/email-digest.json",
            "scripts/cron.sh",
        ]

    def test_integrations_populated(self):
        v13 = self._schema5_v13()
        r = MigrationResult(source_path=Path("x"), dry_run=True)
        spec = _extract_spec(v13, "p-aabbccdd", r)
        assert len(spec["dependencies"]["integrations"]) == 1
        assert spec["dependencies"]["integrations"][0]["integration_id"] == "gmail"

    def test_passthrough_fields_preserved(self):
        v13 = self._schema5_v13()
        r = MigrationResult(source_path=Path("x"), dry_run=True)
        spec = _extract_spec(v13, "p-aabbccdd", r)
        assert spec.get("identity", {}).get("purpose") == "Pull Gmail into local JSON"
        assert spec.get("description") == "A schema-5 style gallery spec"
        assert spec.get("scheduled_actions", []) and spec["scheduled_actions"][0]["id"] == "email-sync"
        assert spec.get("constraints", {}).get("safety") == ["read-only"]

    def test_spec_validates_against_schema(self):
        v13 = self._schema5_v13()
        r = MigrationResult(source_path=Path("x"), dry_run=True)
        spec = _extract_spec(v13, "p-aabbccdd", r)
        assert _validate(spec, SPEC_SCHEMA) == []


# ── Builtin Spec re-seed (deploy-time gallery propagation) ────────────────────

def _make_gallery_pkg(
    pkg_id: str = "p-a9a74bf7",
    pkg_version: str = "2026.06.12-2.3",
    objective: str = "deliver via openclaw message send",
) -> dict:
    """A minimal schema-5 gallery package the re-seed reads."""
    return {
        "pkg_id": pkg_id,
        "pkg_version": pkg_version,
        "gallery_version": pkg_version,
        "schema_version": 5,
        "name": "Morning Briefing",
        "objective": objective,
        "build_spec": "## Delivery\nUse `openclaw message send`.",
        "files": [],
        "crons": [],
    }


def _seed_repo_and_shared(
    tmp_path: Path, pkg: dict, *, name: str = "morning-briefing",
) -> tuple[Path, Path, Path]:
    """Lay out a fake repo gallery + shared dir; return (gallery_root,
    shared_dir, builtin_path) for the package's spec_id."""
    gallery_root = tmp_path / "repo" / "gallery" / name
    gallery_root.mkdir(parents=True)
    (gallery_root / f"{pkg['pkg_id']}.json").write_text(json.dumps(pkg))
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "network.json").write_text(json.dumps({"networkId": "pod-test"}))
    builtin_path = (
        shared_dir / "gallery" / "builtin" / pkg["pkg_id"]
        / f"{INITIAL_SPEC_VERSION}.json"
    )
    return tmp_path / "repo" / "gallery", shared_dir, builtin_path


class TestSeedProvenanceStamp:
    """migrate_gallery_package stamps the seed-provenance the re-seed reads."""

    def test_gallery_promotion_stamps_version_and_hash(self, tmp_path: Path):
        pkg = _make_gallery_pkg()
        pkg_path = tmp_path / "p-a9a74bf7.json"
        pkg_path.write_text(json.dumps(pkg))
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "network.json").write_text(json.dumps({"networkId": "pod-test"}))

        res = migrate_gallery_package(pkg_path, shared_dir, dry_run=False)
        assert res.succeeded, res.errors
        spec = json.loads(res.spec_path.read_text())
        assert spec[SEEDED_FROM_PKG_VERSION_KEY] == "2026.06.12-2.3"
        # 64-hex sha256
        assert len(spec[SEEDED_FROM_PKG_SHA256_KEY]) == 64

    def test_stamped_spec_is_schema_valid(self, tmp_path: Path):
        pkg = _make_gallery_pkg()
        pkg_path = tmp_path / "p-a9a74bf7.json"
        pkg_path.write_text(json.dumps(pkg))
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "network.json").write_text(json.dumps({"networkId": "pod-test"}))
        res = migrate_gallery_package(pkg_path, shared_dir, dry_run=False)
        spec = json.loads(res.spec_path.read_text())
        # The two provenance fields are declared in the schema, so a stamped
        # builtin Spec still validates.
        assert _validate(spec, SPEC_SCHEMA) == []


class TestReseedBuiltinSpecs:
    """reseed_builtin_specs — the deploy-time propagation healer.

    Root cause it closes: a repo gallery edit (e.g. #2695's delivery-endpoint
    migration) never reaches a deployed pod's bound builtin Spec, because a
    gallery install binds the pre-existing builtin and never re-reads the repo
    package. (The 2026-06-12 U1 morning-briefing delivery bug; #2792.)
    """

    def test_no_builtin_tier_is_noop(self, tmp_path: Path):
        """Un-migrated pod (no gallery/builtin/) — nothing to keep in sync."""
        pkg = _make_gallery_pkg()
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        r = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert r.reseeded == [] and r.skipped == []
        assert not builtin_path.exists()

    def test_missing_builtin_left_to_install_mint(self, tmp_path: Path):
        """A package with no builtin (added post-migration) is NOT seeded here —
        install-time mint owns it, so the local-vs-builtin binding is unchanged."""
        pkg = _make_gallery_pkg()
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        # builtin tier exists (a DIFFERENT spec is seeded) but not this one.
        other = shared_dir / "gallery" / "builtin" / "p-deadbeef" / f"{INITIAL_SPEC_VERSION}.json"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps({"spec_id": "p-deadbeef"}))
        r = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert pkg["pkg_id"] not in r.reseeded
        assert not builtin_path.exists()

    def test_legacy_builtin_without_provenance_is_reseeded(self, tmp_path: Path):
        """The stranded class (#2792): a builtin generated before this change
        carries no seed-provenance → re-seed once, stamping it and picking up
        the repo's corrected content."""
        pkg = _make_gallery_pkg(objective="deliver via openclaw message send")
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        # Stranded builtin: old DEAD-endpoint content, no provenance fields.
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({
            "spec_id": pkg["pkg_id"],
            "spec_version": INITIAL_SPEC_VERSION,
            "objective": {"primary": "POST plain text to /api/message"},
        }))
        r = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert r.reseeded == [pkg["pkg_id"]], r.details
        spec = json.loads(builtin_path.read_text())
        assert spec[SEEDED_FROM_PKG_VERSION_KEY] == "2026.06.12-2.3"
        assert "openclaw message send" in spec["objective"]["primary"]

    def test_idempotent_after_seed(self, tmp_path: Path):
        pkg = _make_gallery_pkg()
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({"spec_id": pkg["pkg_id"]}))  # legacy
        first = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert first.reseeded == [pkg["pkg_id"]]
        # Second pass writes nothing.
        second = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert second.reseeded == [] and second.skipped == [pkg["pkg_id"]]

    def test_newer_pkg_version_reseeds(self, tmp_path: Path):
        pkg = _make_gallery_pkg(pkg_version="2026.06.12-2.3")
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({"spec_id": pkg["pkg_id"]}))
        reseed_builtin_specs(shared_dir, gallery_root=gallery_root)  # seed + stamp
        # Bump the repo package and rewrite the on-disk file.
        pkg2 = _make_gallery_pkg(pkg_version="2026.06.13-1.0", objective="bumped")
        (gallery_root / "morning-briefing" / f"{pkg['pkg_id']}.json").write_text(json.dumps(pkg2))
        r = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert r.reseeded == [pkg["pkg_id"]]
        assert json.loads(builtin_path.read_text())[SEEDED_FROM_PKG_VERSION_KEY] == "2026.06.13-1.0"

    def test_older_pkg_version_does_not_downgrade(self, tmp_path: Path):
        """A repo package OLDER than the recorded seed (rollback / race) must
        not clobber a newer builtin."""
        pkg = _make_gallery_pkg(pkg_version="2026.06.13-1.0")
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({"spec_id": pkg["pkg_id"]}))
        reseed_builtin_specs(shared_dir, gallery_root=gallery_root)  # seed at 2026.06.13-1.0
        # Repo now carries an OLDER package version.
        older = _make_gallery_pkg(pkg_version="2026.05.20-1.0", objective="OLD")
        (gallery_root / "morning-briefing" / f"{pkg['pkg_id']}.json").write_text(json.dumps(older))
        r = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert r.reseeded == [] and r.skipped == [pkg["pkg_id"]]
        # Builtin still on the newer version.
        assert json.loads(builtin_path.read_text())[SEEDED_FROM_PKG_VERSION_KEY] == "2026.06.13-1.0"

    def test_same_version_content_drift_reseeds(self, tmp_path: Path):
        """Forgot-to-bump safety: same pkg_version but changed source content
        still re-seeds (content-hash mismatch)."""
        pkg = _make_gallery_pkg(pkg_version="2026.06.12-2.3", objective="original")
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({"spec_id": pkg["pkg_id"]}))
        reseed_builtin_specs(shared_dir, gallery_root=gallery_root)  # seed + stamp hash
        # Edit content WITHOUT bumping pkg_version.
        drifted = _make_gallery_pkg(pkg_version="2026.06.12-2.3", objective="edited in place")
        (gallery_root / "morning-briefing" / f"{pkg['pkg_id']}.json").write_text(json.dumps(drifted))
        r = reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert r.reseeded == [pkg["pkg_id"]]
        assert "edited in place" in json.loads(builtin_path.read_text())["objective"]["primary"]

    def test_dry_run_writes_nothing(self, tmp_path: Path):
        pkg = _make_gallery_pkg()
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({"spec_id": pkg["pkg_id"]}))  # legacy → needs reseed
        before = builtin_path.read_text()
        r = reseed_builtin_specs(shared_dir, gallery_root=gallery_root, dry_run=True)
        assert r.reseeded == [pkg["pkg_id"]]                 # would re-seed
        assert builtin_path.read_text() == before            # but didn't write

    def test_local_specs_never_touched(self, tmp_path: Path):
        """Operator-edited gallery/local/ Specs are authoritative — re-seed
        only writes the builtin tier."""
        pkg = _make_gallery_pkg()
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({"spec_id": pkg["pkg_id"]}))
        local = (
            shared_dir / "gallery" / "local" / pkg["pkg_id"]
            / f"{INITIAL_SPEC_VERSION}.json"
        )
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"operator": "edited", "do_not": "touch"}))
        reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert json.loads(local.read_text()) == {"operator": "edited", "do_not": "touch"}

    def test_non_conformant_pkg_id_skipped(self, tmp_path: Path):
        """A package whose pkg_id isn't a stable p-<hex> can't map to a builtin
        (migrate_all would mint a random id) — skip it entirely."""
        pkg = _make_gallery_pkg(pkg_id="not-a-spec-id")
        gallery_root = tmp_path / "repo" / "gallery" / "weird"
        gallery_root.mkdir(parents=True)
        (gallery_root / "weird.json").write_text(json.dumps(pkg))
        shared_dir = tmp_path / "shared"
        (shared_dir / "gallery" / "builtin").mkdir(parents=True)
        (shared_dir / "network.json").write_text(json.dumps({"networkId": "pod-test"}))
        r = reseed_builtin_specs(shared_dir, gallery_root=tmp_path / "repo" / "gallery")
        assert r.reseeded == [] and r.skipped == [] and r.errors == []

    def test_missing_gallery_root_is_noop(self, tmp_path: Path):
        shared_dir = tmp_path / "shared"
        (shared_dir / "gallery" / "builtin").mkdir(parents=True)
        r = reseed_builtin_specs(shared_dir, gallery_root=tmp_path / "does-not-exist")
        assert r.reseeded == [] and r.errors == []

    def test_reseeded_builtin_is_schema_valid(self, tmp_path: Path):
        pkg = _make_gallery_pkg()
        gallery_root, shared_dir, builtin_path = _seed_repo_and_shared(tmp_path, pkg)
        builtin_path.parent.mkdir(parents=True)
        builtin_path.write_text(json.dumps({"spec_id": pkg["pkg_id"]}))
        reseed_builtin_specs(shared_dir, gallery_root=gallery_root)
        assert _validate(json.loads(builtin_path.read_text()), SPEC_SCHEMA) == []


class TestVersionSortKey:
    """Canonical version grammar shared with native_write (dedup home)."""

    def test_numeric_minor_ordering(self):
        assert version_sort_key("2026.06.12-2.10") > version_sort_key("2026.06.12-2.3")

    def test_date_ordering(self):
        assert version_sort_key("2026.06.13-1.0") > version_sort_key("2026.06.12-9.9")

    def test_non_conformant_sorts_lowest(self):
        assert version_sort_key("garbage") == (0, 0, 0, 0, 0)
        assert version_sort_key("") == (0, 0, 0, 0, 0)


class TestRealizedFilesOwnershipGate:
    """F-B1 writer-hygiene: ``_build_realized_files`` must drop never-ownable
    ``files[]`` entries so a legacy manifest's polluted file list never mints an
    invalid claim into ``realized_files[]``. The shared ``can_app_own`` predicate
    is the gate — the SAME one the read/classify side (recon_ledger invalid_claim)
    uses, so the writer and reader agree."""

    def _result(self) -> MigrationResult:
        return MigrationResult(source_path=Path("/tmp/x"), dry_run=True)

    def test_never_ownable_entries_dropped_legit_retained(self):
        v13 = {"files": [
            # ── Never-ownable (must be DROPPED) ──
            {"path": "member-hash-salt.bin", "file_id": "f-aaaa1111"},   # secret
            {"path": ".capture-salt", "file_id": "f-bbbb2222"},          # dot-salt
            {"path": "capture-log.jsonl", "file_id": "f-cccc3333"},      # append-log
            {"path": "manifests/i-self.json", "file_id": "f-dddd4444"},  # store self-ref
            {"path": "evolve/audit_outbox/rec-1.json", "file_id": "f-eeee5555"},  # telemetry
            {"path": "AGENTS.md", "file_id": "f-ffff6666"},              # OC identity
            {"path": "archive/index.json", "file_id": "f-aaaa7777"},     # archive index
            # ── Genuinely ownable (must be RETAINED) ──
            {"path": "scripts/summary.py", "file_id": "f-bbbb8888"},
            {"path": "data/catalog.json", "file_id": "f-cccc9999"},      # 'log' mid-word
            {"path": "config/settings.yaml", "file_id": "f-dddd0000"},
        ]}
        result = self._result()
        out = _build_realized_files(v13, result)

        kept = {rf["path"] for rf in out}
        assert kept == {
            "scripts/summary.py", "data/catalog.json", "config/settings.yaml",
        }
        # Each drop is recorded as a warning naming the path + the policy.
        dropped_warns = [w for w in result.warnings if "ownership_policy" in w]
        assert len(dropped_warns) == 7
        assert any("member-hash-salt.bin" in w for w in dropped_warns)
        assert any("AGENTS.md" in w for w in dropped_warns)

    def test_clean_file_list_passes_through_unchanged(self):
        v13 = {"files": [
            {"path": "scripts/a.py", "file_id": "f-aaaa1111"},
            {"path": "scripts/b.py", "file_id": "f-bbbb2222"},
        ]}
        result = self._result()
        out = _build_realized_files(v13, result)
        assert [rf["path"] for rf in out] == ["scripts/a.py", "scripts/b.py"]
        assert not [w for w in result.warnings if "ownership_policy" in w]

    def test_empty_file_list_yields_empty(self):
        result = self._result()
        assert _build_realized_files({}, result) == []
        assert _build_realized_files({"files": []}, result) == []
