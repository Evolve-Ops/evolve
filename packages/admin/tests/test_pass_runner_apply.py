"""Tests for the apply_all_pure_passes mutation entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.pass_runner import (  # noqa: E402
    apply_all_pure_passes,
)


def test_apply_writes_coherence_block_on_empty_manifest() -> None:
    manifest = {"id": "x", "description": "no recurring claim"}
    summary = apply_all_pure_passes(manifest)
    # Pass A populates coherence
    assert "coherence" in manifest
    assert "findings" in manifest["coherence"]
    assert manifest["coherence"]["status"] == "ok"
    assert summary["pass_a_count"] == 0


def test_apply_emits_pass_a_finding_for_recurring_without_trigger() -> None:
    manifest = {
        "id": "x",
        "description": "Sends a daily briefing every morning.",
        "scheduled_actions": [], "crons": [],
    }
    summary = apply_all_pure_passes(manifest)
    assert summary["pass_a_count"] >= 1
    assert summary["status"] == "incoherent"  # critical finding present
    # Recorded on the manifest itself.
    findings = manifest["coherence"]["findings"]
    assert any(f.get("id") == "C-A1" for f in findings)


def test_apply_preserves_accepted_signatures() -> None:
    """Operator-accepted finding signatures survive re-run."""
    manifest = {
        "id": "x",
        "description": "Sends a daily briefing every morning.",
        "scheduled_actions": [], "crons": [],
        "coherence": {
            "coherence_accepted": [{"signature": "preserved-sig"}],
        },
    }
    apply_all_pure_passes(manifest)
    assert manifest["coherence"]["coherence_accepted"] == [
        {"signature": "preserved-sig"}
    ]


def test_apply_runs_c1_when_workspace_root_passed(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main.py").write_text("import os\n")
    manifest = {
        "id": "x",
        "scheduled_actions": [{
            "id": "a", "summary": "summarizes the data",
            "implemented_by": "scripts/main.py",
        }],
    }
    summary = apply_all_pure_passes(manifest, workspace_root=tmp_path)
    assert summary["pass_c1_count"] >= 1
    findings = manifest["coherence"]["findings"]
    # Pass C1 finding present alongside any Pass A findings.
    assert any((f.get("check_id") or "").startswith("C1-") for f in findings)


def test_apply_skips_c1_when_no_workspace_root() -> None:
    manifest = {
        "id": "x",
        "scheduled_actions": [{
            "id": "a", "summary": "summarizes things",
            "implemented_by": "scripts/main.py",
        }],
    }
    summary = apply_all_pure_passes(manifest)
    assert summary["pass_c1_count"] == 0


def test_apply_writes_orphan_history(tmp_path: Path) -> None:
    """Orphan check runs and persists verdict to reconciliation."""
    manifest = {
        "id": "x",
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
        },
    }
    apply_all_pure_passes(manifest)
    # Pending (first tick) — should have been recorded.
    history = manifest["reconciliation"].get("orphan_check_history") or []
    assert len(history) >= 1


def test_apply_is_idempotent_for_status() -> None:
    """Running twice produces the same status."""
    manifest = {
        "id": "x",
        "description": "Sends a daily briefing every morning.",
        "scheduled_actions": [], "crons": [],
    }
    s1 = apply_all_pure_passes(manifest)
    s2 = apply_all_pure_passes(manifest)
    assert s1["status"] == s2["status"]
    assert s1["pass_a_count"] == s2["pass_a_count"]
