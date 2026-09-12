"""tests/test_rsi_migration.py — v1 → v2 proposal migration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from migrations.proposal_v1_to_v2 import (  # noqa: E402
    is_v2,
    migrate_directory,
    migrate_one,
)
from schema.proposal import PROPOSAL_SCHEMA_VERSION, Proposal


def _v1_config_change():
    return {
        "id": "legacy-001",
        "bot_id": "team_bot_a",
        "type": "config_change",
        "pattern_key": "tier_drift",
        "problem": "Tier drifted from expected",
        "proposed_change": {
            "target_path": "openclaw.json",
            "operation": "set",
            "value": {"tier": "sonnet"},
        },
        "confidence": 0.7,
        "created_at": "2026-03-01T12:00:00Z",
        "evolve_sig": "legacy-sig-abc",
    }


def _v1_workflow_change():
    return {
        "id": "legacy-002",
        "bot_id": "admin_bot",
        "type": "workflow_change",
        "problem": "Establish a check-in convention",
        "proposed_change": {
            "path": "evolve/conventions.md",
            "content": "# Conventions\n...",
        },
        "confidence": 0.6,
    }


def _v1_investigation():
    return {
        "id": "legacy-003",
        "bot_id": "team_bot_a",
        "type": "investigation",
        "problem": "Something weird in the metrics",
        "confidence": 0.5,
    }


def test_is_v2_detects_schema_version():
    assert not is_v2({"schema_version": 1})
    assert is_v2({"schema_version": 2})
    assert not is_v2({})


def test_migrate_config_change():
    v2_dict = migrate_one(_v1_config_change())
    assert v2_dict["schema_version"] == PROPOSAL_SCHEMA_VERSION
    assert v2_dict["action"]["kind"] == "ConfigPatch"
    assert v2_dict["dimension"] == "substrate_health"
    assert v2_dict["generator_id"] == "legacy_analyzer"
    assert v2_dict["approval_audience"] == "pod_operator"
    assert v2_dict["signature"] == "legacy-sig-abc"
    # Round-trip through Proposal to prove structural validity
    p = Proposal.from_dict(v2_dict)
    assert p.action.kind == "ConfigPatch"


def test_migrate_workflow_change():
    v2_dict = migrate_one(_v1_workflow_change())
    assert v2_dict["action"]["kind"] == "WorkflowInstruction"
    assert v2_dict["dimension"] == "utility"
    p = Proposal.from_dict(v2_dict)
    assert p.action.kind == "WorkflowInstruction"
    assert p.action.content.startswith("# Conventions")


def test_migrate_investigation():
    v2_dict = migrate_one(_v1_investigation())
    assert v2_dict["action"]["kind"] == "Investigation"
    p = Proposal.from_dict(v2_dict)
    assert p.action.context


def test_migrate_idempotent_for_v2_input():
    v2_dict = migrate_one(_v1_config_change())
    # Pass it back through: should be unchanged
    assert migrate_one(v2_dict) is v2_dict


def test_migrate_preserves_original_id():
    v2_dict = migrate_one(_v1_config_change())
    assert v2_dict["id"] == "legacy-001"


def test_migrate_directory_scans_and_writes(tmp_path):
    proposals_dir = tmp_path / "proposals" / "pending"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "a.json").write_text(json.dumps(_v1_config_change()))
    (proposals_dir / "b.json").write_text(json.dumps(_v1_workflow_change()))
    # Already v2 → should be skipped
    v2_sample = migrate_one(_v1_config_change())
    (proposals_dir / "c.json").write_text(json.dumps(v2_sample))

    report = migrate_directory(tmp_path / "proposals", dry_run=False)
    assert report["scanned"] == 3
    assert report["already_v2"] == 1
    assert report["migrated"] == 2
    assert report["errors"] == []

    # Verify on-disk files are now v2
    for name in ["a", "b"]:
        with (proposals_dir / f"{name}.json").open() as fh:
            data = json.load(fh)
        assert is_v2(data)


def test_migrate_directory_dry_run_doesnt_write(tmp_path):
    proposals_dir = tmp_path / "proposals" / "pending"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "a.json").write_text(json.dumps(_v1_config_change()))

    report = migrate_directory(tmp_path / "proposals", dry_run=True)
    assert report["migrated"] == 1

    with (proposals_dir / "a.json").open() as fh:
        data = json.load(fh)
    assert not is_v2(data)  # still v1 on disk


def test_migrate_directory_collects_errors(tmp_path):
    proposals_dir = tmp_path / "proposals" / "pending"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "broken.json").write_text("{not valid json")

    report = migrate_directory(tmp_path / "proposals", dry_run=False)
    assert len(report["errors"]) == 1
    assert "broken.json" in report["errors"][0][0]
