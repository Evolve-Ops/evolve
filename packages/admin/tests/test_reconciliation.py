"""Reconciliation pass tests — PR 4 of the coherence + reconciliation
framework.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §7.2, §7.4.

The reconciliation pass is the first module where the spec's central
doctrine — *silent for observational fields, chip for authored fields*
— becomes operationally visible. Tests pin both halves of the gate
plus each of the four checks (missing_files, missing_crons,
missing_actions, extra_files).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.reconciliation import (  # noqa: E402
    apply_reconciliation,
    check_extra_files,
    check_missing_actions,
    check_missing_crons,
    check_missing_files,
    is_field_authored,
)


# ── Provenance gate helper ─────────────────────────────────────────────────

def test_is_field_authored_returns_true_for_authored_sources() -> None:
    for src in ("forge_built", "user_authored", "bot_authored", "confirmed"):
        manifest = {"provenance": {"field_origins": {
            "files": {"source": src}
        }}}
        assert is_field_authored(manifest, "files") is True


def test_is_field_authored_returns_false_for_observational() -> None:
    manifest = {"provenance": {"field_origins": {
        "files": {"source": "observational"}
    }}}
    assert is_field_authored(manifest, "files") is False


def test_is_field_authored_returns_false_when_absent() -> None:
    """Spec §4.2: fields absent from field_origins default to observational."""
    manifest = {"provenance": {"field_origins": {}}}
    assert is_field_authored(manifest, "files") is False


def test_is_field_authored_returns_false_when_provenance_missing() -> None:
    """No provenance block at all → safe default observational."""
    assert is_field_authored({}, "files") is False


def test_is_field_authored_case_insensitive_source() -> None:
    """Defensive: source values are normalised to lowercase before
    comparison."""
    manifest = {"provenance": {"field_origins": {
        "files": {"source": "USER_AUTHORED"}
    }}}
    assert is_field_authored(manifest, "files") is True


# ── Missing files: observational → silent drop ────────────────────────────

def test_missing_files_observational_drop_silently(tmp_path: Path) -> None:
    """The canonical observational case: scanner finds the manifest
    claims a file that's not on disk, files[] is observational → drop
    silently. No chip."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # No files created — the manifest claim won't resolve.
    manifest = {
        "id": "test", "files": [
            {"path": "scripts/gone.py", "layer": "code"}
        ],
        # provenance block absent → defaults to observational.
    }
    summary = apply_reconciliation(manifest, workspace)
    # The entry was silently dropped from files[].
    assert manifest["files"] == []
    assert summary.missing_files_silent_dropped == 1
    assert summary.missing_files_staged == 0
    # Reconciliation block is empty/ok — no staged entries.
    assert manifest["reconciliation"]["status"] == "ok"
    assert manifest["reconciliation"]["missing_files"] == []


# ── Missing files: authored → stage chip ──────────────────────────────────

def test_missing_files_authored_stages_chip(tmp_path: Path) -> None:
    """When files[] is authored, the missing entry stays in files[] AND
    appears in reconciliation.missing_files[] for the operator to
    Approve / Repair / Defer."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {
            "files": {"source": "forge_built"}
        }},
        "files": [
            {"path": "scripts/gone.py", "layer": "code"}
        ],
    }
    summary = apply_reconciliation(manifest, workspace)
    # The entry is NOT dropped from files[].
    assert len(manifest["files"]) == 1
    # And reconciliation.missing_files[] has it staged.
    assert summary.missing_files_staged == 1
    assert summary.missing_files_silent_dropped == 0
    staged = manifest["reconciliation"]["missing_files"]
    assert len(staged) == 1
    assert staged[0]["path"] == "scripts/gone.py"
    # Status reflects the drift.
    assert manifest["reconciliation"]["status"] == "drift_repair_needed"


def test_missing_files_authored_existing_file_no_finding(tmp_path: Path) -> None:
    """Existing file on disk → no finding regardless of provenance."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "main.py").write_text("# ok")
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {
            "files": {"source": "forge_built"}
        }},
        "files": [
            {"path": "scripts/main.py", "layer": "code"}
        ],
    }
    summary = apply_reconciliation(manifest, workspace)
    assert summary.missing_files_staged == 0
    assert summary.missing_files_silent_dropped == 0


# ── Missing crons ──────────────────────────────────────────────────────────

def test_missing_crons_observational_drop_silently(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "crons": [{"label": "gone", "schedule": "0 0 * * *",
                   "script": "scripts/gone.sh"}],
    }
    summary = apply_reconciliation(
        manifest, workspace,
        live_cron_labels=set(),   # nothing live
    )
    assert manifest["crons"] == []
    assert summary.missing_crons_silent_dropped == 1


def test_missing_crons_authored_stages_chip(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {"crons": {"source": "user_authored"}}},
        "crons": [{"label": "missing-cron", "schedule": "0 0 * * *",
                   "script": "scripts/x.sh"}],
    }
    summary = apply_reconciliation(
        manifest, workspace,
        live_cron_labels=set(),
    )
    assert len(manifest["crons"]) == 1
    assert summary.missing_crons_staged == 1
    assert manifest["reconciliation"]["missing_crons"][0]["cron_id"] == \
        "missing-cron"
    # Missing crons are a quiet failure — operator value high.
    assert manifest["reconciliation"]["status"] == "quiet_failure"


def test_missing_crons_skipped_when_live_unavailable(tmp_path: Path) -> None:
    """When ``live_cron_labels`` is None (transient CLI failure), the
    check is skipped — no false positives during outages."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {"crons": {"source": "forge_built"}}},
        "crons": [{"label": "x", "schedule": "0 0 * * *", "script": "y.sh"}],
    }
    summary = apply_reconciliation(
        manifest, workspace,
        live_cron_labels=None,   # CLI unavailable
    )
    assert summary.missing_crons_staged == 0
    assert summary.missing_crons_silent_dropped == 0


def test_present_crons_no_finding(tmp_path: Path) -> None:
    """A cron present in the live list produces no finding."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "crons": [{"label": "backup", "schedule": "0 3 * * *",
                   "script": "scripts/backup.sh"}],
    }
    summary = apply_reconciliation(
        manifest, workspace,
        live_cron_labels={"backup"},
    )
    assert summary.missing_crons_staged == 0
    assert summary.missing_crons_silent_dropped == 0


# ── Missing scheduled_actions (evidence anchors) ──────────────────────────

def test_missing_action_anchor_stages_when_authored(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "HEARTBEAT.md").write_text("# Heartbeat\n\nUnrelated content.")
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {
            "scheduled_actions": {"source": "forge_built"}
        }},
        "scheduled_actions": [{
            "id": "protein", "state": "active",
            "quality": "extracted",
            "trigger": {
                "kind": "heartbeat",
                "evidence_path": "HEARTBEAT.md",
                "evidence_locator": "Protein Reminder Section",
            },
        }],
    }
    summary = apply_reconciliation(manifest, workspace)
    assert summary.missing_actions_staged == 1
    assert manifest["reconciliation"]["missing_actions"][0]["reason"] == \
        "anchor_not_in_file"


def test_present_action_anchor_no_finding(tmp_path: Path) -> None:
    """When the anchor text is in the file, no finding."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "HEARTBEAT.md").write_text(
        "# Heartbeat\n\n## Protein Reminder Section\n\nCheck protein."
    )
    manifest = {
        "id": "test",
        "scheduled_actions": [{
            "id": "p", "state": "active", "quality": "extracted",
            "trigger": {
                "kind": "heartbeat",
                "evidence_path": "HEARTBEAT.md",
                "evidence_locator": "Protein Reminder Section",
            },
        }],
    }
    summary = apply_reconciliation(manifest, workspace)
    assert summary.missing_actions_staged == 0


def test_disabled_action_skipped(tmp_path: Path) -> None:
    """state != active → not checked. The protein-DISABLED pattern."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "scheduled_actions": [{
            "id": "p", "state": "disabled",
            "trigger": {
                "kind": "heartbeat",
                "evidence_path": "MISSING.md",
                "evidence_locator": "section that doesn't exist",
            },
        }],
    }
    summary = apply_reconciliation(manifest, workspace)
    assert summary.missing_actions_staged == 0


def test_suspect_action_skipped(tmp_path: Path) -> None:
    """quality=suspect → not checked. The legacy-noise filter."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "scheduled_actions": [{
            "id": "p", "state": "active", "quality": "suspect",
            "trigger": {
                "kind": "heartbeat",
                "evidence_path": "MISSING.md",
                "evidence_locator": "x",
            },
        }],
    }
    summary = apply_reconciliation(manifest, workspace)
    assert summary.missing_actions_staged == 0


# ── Extra files via volatile_paths match ──────────────────────────────────

def test_extra_files_observational_silent_add(tmp_path: Path) -> None:
    """Workspace file matching volatile_paths glob and files[] is
    observational → auto-add."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "files": [],
        "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
    }
    workspace_files = {"data/2026-06-06.md"}
    summary = apply_reconciliation(
        manifest, workspace,
        workspace_files=workspace_files,
    )
    assert summary.extra_files_silent_added == 1
    assert manifest["files"][0]["path"] == "data/2026-06-06.md"
    assert manifest["files"][0]["layer"] == "data"


def test_extra_files_authored_stages_chip(tmp_path: Path) -> None:
    """When files[] is authored, the new file is staged as an extra,
    not added directly."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {"files": {"source": "forge_built"}}},
        "files": [],
        "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
    }
    workspace_files = {"data/new.md"}
    summary = apply_reconciliation(
        manifest, workspace,
        workspace_files=workspace_files,
    )
    assert summary.extra_files_staged == 1
    assert summary.extra_files_silent_added == 0
    assert manifest["files"] == []
    assert manifest["reconciliation"]["extra_files"][0]["path"] == "data/new.md"
    # Status reflects the proposed expansion.
    assert manifest["reconciliation"]["status"] == "proposed_expansion"


def test_extra_files_no_attribution_skipped(tmp_path: Path) -> None:
    """A workspace file that doesn't match any volatile_paths glob is
    NOT staged or added — it'll be picked up by Phase 4's LLM discovery
    if it belongs here."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "files": [], "volatile_paths": [],
    }
    workspace_files = {"random/path.json"}
    summary = apply_reconciliation(
        manifest, workspace,
        workspace_files=workspace_files,
    )
    assert summary.extra_files_silent_added == 0
    assert summary.extra_files_staged == 0


def test_extra_files_skipped_when_workspace_files_none(tmp_path: Path) -> None:
    """``workspace_files=None`` (scanner didn't compute it) → no extras."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "volatile_paths": [{"glob": "data/*", "layer": "data"}],
    }
    summary = apply_reconciliation(
        manifest, workspace,
        workspace_files=None,
    )
    assert summary.extra_files_silent_added == 0
    assert summary.extra_files_staged == 0


# ── Reconciliation block bookkeeping ──────────────────────────────────────

def test_reconciliation_block_populated_with_timestamp(tmp_path: Path) -> None:
    """last_reconciled_at is set on every run, even when nothing changes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {"id": "test", "files": [], "crons": []}
    summary = apply_reconciliation(manifest, workspace)
    assert manifest["reconciliation"]["last_reconciled_at"]
    assert manifest["reconciliation"]["status"] == "ok"


def test_operator_decisions_preserved_across_runs(tmp_path: Path) -> None:
    """operator_decisions[] survives the rewrite. Operator clicks aren't
    undone by a re-scan."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test", "files": [],
        "reconciliation": {
            "operator_decisions": [
                {"kind": "approve", "target": "old", "decided_at": "..."}
            ],
        },
    }
    apply_reconciliation(manifest, workspace)
    assert manifest["reconciliation"]["operator_decisions"] == [
        {"kind": "approve", "target": "old", "decided_at": "..."}
    ]


# ── Idempotency ───────────────────────────────────────────────────────────

def test_idempotent_on_no_changes(tmp_path: Path) -> None:
    """Running the pass twice when nothing's changed yields the same
    result the second time."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {"id": "test", "files": [], "crons": []}
    apply_reconciliation(manifest, workspace)
    first = dict(manifest["reconciliation"])
    apply_reconciliation(manifest, workspace)
    second = dict(manifest["reconciliation"])
    # last_reconciled_at advances but everything else is identical.
    assert first.keys() == second.keys()
    assert first["status"] == second["status"]
    assert first["missing_files"] == second["missing_files"]
    assert first["operator_decisions"] == second["operator_decisions"]


def test_idempotent_silent_drops(tmp_path: Path) -> None:
    """After a silent drop, a re-run finds nothing to drop."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        "files": [{"path": "missing.py", "layer": "code"}],
    }
    apply_reconciliation(manifest, workspace)
    assert manifest["files"] == []
    summary = apply_reconciliation(manifest, workspace)
    assert summary.missing_files_silent_dropped == 0
    assert summary.missing_files_staged == 0


# ── Drift events (Bite 3 feed) ─────────────────────────────────────────────

def test_drift_events_capture_silent_and_staged(tmp_path: Path) -> None:
    """The drift_events feed normalizes BOTH silent (observational) and staged
    (authored) deltas across all four checks — independent of the gate."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test",
        # files authored → staged; crons observational → silent drop.
        "provenance": {"field_origins": {"files": {"source": "forge_built"}}},
        "files": [{"path": "scripts/gone.py", "layer": "code"}],
        "crons": [{"label": "nightly", "schedule": "0 0 * * *",
                   "script": "scripts/n.sh"}],
    }
    summary = apply_reconciliation(manifest, workspace, live_cron_labels=set())
    events = summary.drift_events
    # one file remove (staged) + one cron remove (silent)
    file_removes = [e for e in events if e["target_type"] == "file"]
    cron_removes = [e for e in events if e["target_type"] == "cron"]
    assert len(file_removes) == 1 and file_removes[0]["kind"] == "remove"
    assert file_removes[0]["target"] == "scripts/gone.py"
    assert len(cron_removes) == 1 and cron_removes[0]["target"] == "nightly"
    # And the summary serializes the feed.
    assert summary.to_dict()["drift_events"] == events


def test_drift_events_action_anchor_is_modify(tmp_path: Path) -> None:
    """A staged action whose evidence anchor moved → a MODIFY event with its
    reason in ``detail``; a vanished evidence file → REMOVE."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "HEARTBEAT.md").write_text("# Heartbeat\n\nUnrelated.")
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {
            "scheduled_actions": {"source": "forge_built"}}},
        "scheduled_actions": [{
            "id": "protein", "state": "active", "quality": "extracted",
            "trigger": {"kind": "heartbeat", "evidence_path": "HEARTBEAT.md",
                        "evidence_locator": "Missing Section"},
        }],
    }
    summary = apply_reconciliation(manifest, workspace)
    actions = [e for e in summary.drift_events if e["target_type"] == "action"]
    assert len(actions) == 1
    assert actions[0]["kind"] == "modify"
    assert actions[0]["detail"] == "anchor_not_in_file"


def test_drift_events_extra_file_is_add(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "id": "test", "files": [],
        "volatile_paths": [{"glob": "plugins/*.py", "layer": "code"}],
    }
    summary = apply_reconciliation(
        manifest, workspace, workspace_files={"plugins/x.py"})
    adds = [e for e in summary.drift_events if e["kind"] == "add"]
    assert len(adds) == 1
    assert adds[0]["target"] == "plugins/x.py"


def test_drift_events_empty_when_no_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    summary = apply_reconciliation({"id": "test", "files": []}, workspace)
    assert summary.drift_events == []
