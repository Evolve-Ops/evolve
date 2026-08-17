"""tests/test_validation_results_migration.py — Migration from forge-results/ to
validation-results/ and from forge_notes → validation_notes.

Covers scripts/migrate-validation-results.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load the migration script as a module (it lives in scripts/, not on sys.path)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_PATH = _REPO_ROOT / "scripts" / "migrate-validation-results.py"
_spec = importlib.util.spec_from_file_location("migrate_validation_results", _MIGRATION_PATH)
migrate_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_mod)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _legacy_result(proposal_id: str, note: str = "ok") -> dict:
    return {
        "proposal_id": proposal_id,
        "result": "pass",
        "recommendation": "promote",
        "forge_notes": note,
        "tests_run": [],
    }


class TestMigrate:
    def test_moves_legacy_files_and_renames_field(self, tmp_path: Path):
        legacy = tmp_path / "proposals" / "forge-results"
        new = tmp_path / "proposals" / "validation-results"
        _write(legacy / "prop-001.json", _legacy_result("prop-001", "boot ok"))
        _write(legacy / "prop-002.json", _legacy_result("prop-002", "syntax ok"))

        migrate_mod.migrate(tmp_path)

        # Old dir is gone (or empty)
        assert not legacy.exists() or not any(legacy.iterdir())

        # New files have validation_notes, no forge_notes
        moved_001 = json.loads((new / "prop-001.json").read_text())
        assert moved_001["validation_notes"] == "boot ok"
        assert "forge_notes" not in moved_001

        moved_002 = json.loads((new / "prop-002.json").read_text())
        assert moved_002["validation_notes"] == "syntax ok"
        assert "forge_notes" not in moved_002

    def test_rewrites_field_for_files_already_in_new_dir(self, tmp_path: Path):
        new = tmp_path / "proposals" / "validation-results"
        _write(new / "prop-003.json", _legacy_result("prop-003", "already moved"))

        migrate_mod.migrate(tmp_path)

        result = json.loads((new / "prop-003.json").read_text())
        assert result["validation_notes"] == "already moved"
        assert "forge_notes" not in result

    def test_legacy_duplicate_prefers_new_dir(self, tmp_path: Path):
        legacy = tmp_path / "proposals" / "forge-results"
        new = tmp_path / "proposals" / "validation-results"
        # Same proposal id in both dirs; new dir's content should win.
        _write(legacy / "prop-004.json", _legacy_result("prop-004", "stale legacy"))
        new_data = {
            "proposal_id": "prop-004",
            "result": "pass",
            "recommendation": "promote",
            "validation_notes": "fresh new",
            "tests_run": [],
        }
        _write(new / "prop-004.json", new_data)

        migrate_mod.migrate(tmp_path)

        result = json.loads((new / "prop-004.json").read_text())
        assert result["validation_notes"] == "fresh new"
        # Legacy file removed
        assert not (legacy / "prop-004.json").exists()

    def test_idempotent(self, tmp_path: Path):
        legacy = tmp_path / "proposals" / "forge-results"
        _write(legacy / "prop-005.json", _legacy_result("prop-005", "first"))

        # Run twice — second pass should be a no-op
        migrate_mod.migrate(tmp_path)
        migrate_mod.migrate(tmp_path)

        new = tmp_path / "proposals" / "validation-results" / "prop-005.json"
        result = json.loads(new.read_text())
        assert result["validation_notes"] == "first"
        assert "forge_notes" not in result

    def test_no_legacy_dir_is_safe(self, tmp_path: Path):
        # Just an empty proposals/ — migrate should not blow up
        (tmp_path / "proposals").mkdir()
        migrate_mod.migrate(tmp_path)

    def test_dry_run_does_not_modify(self, tmp_path: Path):
        legacy = tmp_path / "proposals" / "forge-results"
        _write(legacy / "prop-006.json", _legacy_result("prop-006", "should stay"))

        migrate_mod.migrate(tmp_path, dry_run=True)

        # Legacy still has the file with the original field
        assert (legacy / "prop-006.json").exists()
        original = json.loads((legacy / "prop-006.json").read_text())
        assert original["forge_notes"] == "should stay"
        # New dir was not populated
        new = tmp_path / "proposals" / "validation-results"
        assert not new.exists() or not any(new.iterdir())


class TestRewriteField:
    def test_renames_when_only_legacy_present(self):
        d = {"forge_notes": "x"}
        assert migrate_mod._rewrite_field(d) is True
        assert d == {"validation_notes": "x"}

    def test_drops_legacy_when_both_present(self):
        d = {"forge_notes": "old", "validation_notes": "new"}
        assert migrate_mod._rewrite_field(d) is True
        assert d == {"validation_notes": "new"}

    def test_noop_when_only_new(self):
        d = {"validation_notes": "x"}
        assert migrate_mod._rewrite_field(d) is False
        assert d == {"validation_notes": "x"}

    def test_noop_when_neither(self):
        d = {"unrelated": "x"}
        assert migrate_mod._rewrite_field(d) is False
        assert d == {"unrelated": "x"}
