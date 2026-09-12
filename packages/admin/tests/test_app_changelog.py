"""App changelog tests — PR 11.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §11.5.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_changelog import (  # noqa: E402
    KIND_DECISION_RECORDED,
    KIND_MANIFEST_CHANGE,
    KIND_PROVENANCE_CHANGE,
    KIND_REPAIR_APPLIED,
    KIND_REPAIR_FAILED,
    KIND_STRUCTURAL_ADDITION,
    KIND_STRUCTURAL_REMOVAL,
    VALID_KINDS,
    append_to_trail,
    build_decision_recorded_entry,
    build_manifest_change_entry,
    build_provenance_change_entry,
    build_repair_applied_entry,
    build_repair_failed_entry,
    build_structural_addition_entry,
    build_structural_removal_entry,
    filter_structural_only,
    read_trail,
    summarize_diff,
)


# ── Entry kinds ──────────────────────────────────────────────────────────

def test_all_kinds_in_valid_set() -> None:
    for k in (KIND_MANIFEST_CHANGE, KIND_STRUCTURAL_ADDITION,
              KIND_STRUCTURAL_REMOVAL, KIND_PROVENANCE_CHANGE,
              KIND_DECISION_RECORDED, KIND_REPAIR_APPLIED,
              KIND_REPAIR_FAILED):
        assert k in VALID_KINDS


# ── Entry builders ───────────────────────────────────────────────────────

def test_manifest_change_entry_shape() -> None:
    entry = build_manifest_change_entry(
        field="description", before="old", after="new",
    )
    assert entry["kind"] == KIND_MANIFEST_CHANGE
    assert entry["field"] == "description"
    assert entry["before"] == "old"
    assert entry["after"] == "new"
    assert entry["ts"]


def test_structural_addition_code_file_logs() -> None:
    """code-layer file → logged."""
    entry = build_structural_addition_entry(
        kind="file", id="scripts/main.py", layer="code",
    )
    assert entry is not None
    assert entry["kind"] == KIND_STRUCTURAL_ADDITION
    assert entry["element_kind"] == "file"
    assert entry["layer"] == "code"


def test_structural_addition_config_file_logs() -> None:
    entry = build_structural_addition_entry(
        kind="file", id="config/settings.yaml", layer="config",
    )
    assert entry is not None


def test_structural_addition_data_file_filtered_out() -> None:
    """Spec §11.5.1: data-layer files are NOT logged (noise)."""
    entry = build_structural_addition_entry(
        kind="file", id="data/2026-06-06.md", layer="data",
    )
    assert entry is None   # filtered


def test_structural_addition_log_file_filtered_out() -> None:
    entry = build_structural_addition_entry(
        kind="file", id="logs/audit.log", layer="log",
    )
    assert entry is None


def test_structural_addition_state_file_filtered_out() -> None:
    entry = build_structural_addition_entry(
        kind="file", id="state/session.lock", layer="state",
    )
    assert entry is None


def test_structural_addition_content_file_filtered_out() -> None:
    """Content files come and go by design; not logged unless they're
    contract-shaping. Caller passes layer=content; returns None."""
    entry = build_structural_addition_entry(
        kind="file", id="articles/topic.md", layer="content",
    )
    assert entry is None


def test_structural_addition_cron_logs_regardless() -> None:
    """Non-file element kinds (cron / action / integration / volatile_path)
    always log — they're contract-shaping regardless of layer."""
    entry = build_structural_addition_entry(
        kind="cron", id="backup", layer=None,
    )
    assert entry is not None


def test_structural_removal_symmetric() -> None:
    """Removal honors the same layer filter as addition."""
    code_removal = build_structural_removal_entry(
        kind="file", id="scripts/dead.py", layer="code",
    )
    assert code_removal is not None
    data_removal = build_structural_removal_entry(
        kind="file", id="data/old.md", layer="data",
    )
    assert data_removal is None


def test_provenance_change_entry_shape() -> None:
    entry = build_provenance_change_entry(
        field="description", from_source="observational",
        to_source="user_authored", by="operator", via="manifest_editor",
    )
    assert entry["kind"] == KIND_PROVENANCE_CHANGE
    assert entry["field"] == "description"
    assert entry["from"] == "observational"
    assert entry["to"] == "user_authored"
    assert entry["by"] == "operator"


def test_decision_recorded_entry_shape() -> None:
    entry = build_decision_recorded_entry(
        finding_id="C-A1", decision="approve",
        by="operator", rationale="aspirational; not drift",
    )
    assert entry["kind"] == KIND_DECISION_RECORDED
    assert entry["finding_id"] == "C-A1"
    assert entry["decision"] == "approve"


def test_repair_applied_entry_shape() -> None:
    entry = build_repair_applied_entry(
        request_id="repair-abc",
        transformations=[{"kind": "reinstall_cron_from_manifest"}],
        proposals=[],
    )
    assert entry["kind"] == KIND_REPAIR_APPLIED
    assert entry["request_id"] == "repair-abc"
    assert len(entry["transformations"]) == 1


def test_repair_failed_entry_shape() -> None:
    entry = build_repair_failed_entry(
        request_id="repair-abc", error="LLM timeout",
    )
    assert entry["kind"] == KIND_REPAIR_FAILED
    assert entry["error"] == "LLM timeout"


# ── append_to_trail ──────────────────────────────────────────────────────

def test_append_creates_dir_and_file(tmp_path: Path) -> None:
    entry = build_manifest_change_entry(field="x", after="y")
    ok = append_to_trail(tmp_path, "myapp", entry)
    assert ok
    target = tmp_path / "myapp" / "trail.jsonl"
    assert target.exists()


def test_append_writes_one_line_per_entry(tmp_path: Path) -> None:
    for i in range(3):
        entry = build_manifest_change_entry(field=f"f{i}", after=i)
        append_to_trail(tmp_path, "myapp", entry)
    lines = (tmp_path / "myapp" / "trail.jsonl").read_text().splitlines()
    assert len(lines) == 3


def test_append_returns_false_for_none_entry(tmp_path: Path) -> None:
    """A filtered-out builder returns None; append handles gracefully."""
    none_entry = build_structural_addition_entry(
        kind="file", id="data/x.md", layer="data",
    )
    assert none_entry is None
    ok = append_to_trail(tmp_path, "myapp", none_entry)
    assert ok is False


def test_append_returns_false_for_unknown_kind(tmp_path: Path) -> None:
    """Defensive: only known kinds are written."""
    ok = append_to_trail(tmp_path, "myapp",
                         {"ts": "x", "kind": "random_kind"})
    assert ok is False


# ── read_trail ───────────────────────────────────────────────────────────

def test_read_trail_returns_newest_first(tmp_path: Path) -> None:
    entries = [
        build_manifest_change_entry(field=f"f{i}", after=i,
                                     at=f"2026-06-0{i+1}T00:00:00Z")
        for i in range(3)
    ]
    for e in entries:
        append_to_trail(tmp_path, "myapp", e)
    out = read_trail(tmp_path, "myapp")
    assert len(out) == 3
    # Newest first.
    assert out[0]["field"] == "f2"
    assert out[2]["field"] == "f0"


def test_read_trail_respects_limit(tmp_path: Path) -> None:
    for i in range(5):
        append_to_trail(
            tmp_path, "myapp",
            build_manifest_change_entry(field=f"f{i}", after=i),
        )
    out = read_trail(tmp_path, "myapp", limit=2)
    assert len(out) == 2


def test_read_trail_filters_by_kind(tmp_path: Path) -> None:
    append_to_trail(tmp_path, "myapp",
                    build_manifest_change_entry(field="x"))
    append_to_trail(tmp_path, "myapp",
                    build_provenance_change_entry(
                        field="y", from_source="observational",
                        to_source="user_authored"))
    out = read_trail(tmp_path, "myapp",
                     kinds={KIND_PROVENANCE_CHANGE})
    assert len(out) == 1
    assert out[0]["kind"] == KIND_PROVENANCE_CHANGE


def test_read_trail_returns_empty_when_missing(tmp_path: Path) -> None:
    assert read_trail(tmp_path, "noapp") == []


def test_read_trail_tolerates_malformed_lines(tmp_path: Path) -> None:
    target = tmp_path / "myapp" / "trail.jsonl"
    target.parent.mkdir()
    target.write_text("not valid json\n" +
                      json.dumps(build_manifest_change_entry(
                          field="x")) + "\n")
    out = read_trail(tmp_path, "myapp")
    assert len(out) == 1
    assert out[0]["field"] == "x"


# ── filter_structural_only ──────────────────────────────────────────────

def test_filter_structural_only_drops_unknown_kinds() -> None:
    entries = [
        {"kind": KIND_MANIFEST_CHANGE},
        {"kind": "audit_run"},   # existing audit framework kind
        {"kind": KIND_PROVENANCE_CHANGE},
    ]
    out = filter_structural_only(entries)
    assert len(out) == 2
    assert all(e["kind"] in VALID_KINDS for e in out)


# ── summarize_diff ───────────────────────────────────────────────────────

def test_summarize_diff_counts_recent_entries(tmp_path: Path) -> None:
    append_to_trail(tmp_path, "myapp",
                    build_manifest_change_entry(
                        field="x", at="2026-06-01T00:00:00Z"))
    append_to_trail(tmp_path, "myapp",
                    build_manifest_change_entry(
                        field="y", at="2026-06-05T00:00:00Z"))
    append_to_trail(tmp_path, "myapp",
                    build_provenance_change_entry(
                        field="z", from_source="observational",
                        to_source="user_authored",
                        at="2026-06-05T12:00:00Z"))
    summary = summarize_diff(tmp_path, "myapp",
                             since_iso="2026-06-03T00:00:00Z")
    assert summary["total"] == 2
    assert summary["by_kind"][KIND_MANIFEST_CHANGE] == 1
    assert summary["by_kind"][KIND_PROVENANCE_CHANGE] == 1


def test_summarize_diff_empty_when_nothing_recent(tmp_path: Path) -> None:
    summary = summarize_diff(tmp_path, "myapp",
                             since_iso="2026-06-06T00:00:00Z")
    assert summary["total"] == 0
    assert summary["by_kind"] == {}
