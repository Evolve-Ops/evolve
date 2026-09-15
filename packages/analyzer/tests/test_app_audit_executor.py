"""Tests for app_audit_executor — cross-app conflict detection.

The executor's transformation whitelist is empty in v1 (calibration mode
demotes auto_fix to propose before reaching the executor anyway). What we
test here is the conflict-detection logic, since that fires independent
of calibration mode and is the load-bearing safety guard against
regression loops between apps that share files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_executor import (  # noqa: E402
    ConflictReport,
    execute_auto_fix,
    find_conflicts,
    _normalize_path,
)


# ── _normalize_path ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("input_path,expected", [
    ("scripts/journal.py", "scripts/journal.py"),
    ("./scripts/journal.py", "scripts/journal.py"),
    ("/scripts/journal.py", "scripts/journal.py"),
    ("//scripts/journal.py", "scripts/journal.py"),
    ("", ""),
    ("  scripts/x.py  ", "scripts/x.py"),
])
def test_normalize_path(input_path: str, expected: str) -> None:
    assert _normalize_path(input_path) == expected


# ── find_conflicts: files registry matches ──────────────────────────────────


def test_no_conflict_when_no_other_app_references_path() -> None:
    manifests = [
        {"id": "journal", "files": [{"path": "scripts/journal.py"}]},
        {"id": "other", "files": [{"path": "scripts/other.py"}]},
    ]
    conflict = find_conflicts("scripts/journal.py", "journal", manifests)
    assert conflict is None


def test_conflict_detected_via_files_registry() -> None:
    manifests = [
        {"id": "journal", "files": [{"path": "scripts/shared.py"}]},
        {"id": "mood-tracker", "display_name": "Mood Tracker",
         "files": [{"path": "scripts/shared.py"}]},
    ]
    conflict = find_conflicts("scripts/shared.py", "journal", manifests)
    assert conflict is not None
    assert conflict.file_path == "scripts/shared.py"
    assert len(conflict.affected_apps) == 1
    assert conflict.affected_apps[0]["app_id"] == "mood-tracker"
    assert conflict.affected_apps[0]["role"] == "owner"
    assert conflict.affected_apps[0]["display_name"] == "Mood Tracker"


def test_conflict_detected_via_dependencies_list() -> None:
    """File appears in another app's dependencies (cross-app file share)."""
    manifests = [
        {"id": "morning-briefing",
         "dependencies": [{"path": "scripts/journal.py", "owned_by": "p-journal"}]},
    ]
    conflict = find_conflicts("scripts/journal.py", "journal", manifests)
    assert conflict is not None
    assert conflict.affected_apps[0]["role"] == "dependency"


def test_conflict_detected_via_cron_script() -> None:
    """A cron entry whose script is the target path triggers a conflict."""
    manifests = [
        {"id": "scheduler",
         "crons": [{"schedule": "0 8 * * *", "script": "scripts/shared.py"}]},
    ]
    conflict = find_conflicts("scripts/shared.py", "journal", manifests)
    assert conflict is not None
    assert conflict.affected_apps[0]["role"] == "cron_script"


def test_conflict_detected_via_cron_script_wrapped_invocation() -> None:
    """Cron 'python3 path/to/script.py' form gets matched by basename."""
    manifests = [
        {"id": "scheduler",
         "crons": [{"schedule": "@daily",
                    "script": "/opt/homebrew/bin/python3 scripts/shared.py"}]},
    ]
    conflict = find_conflicts("scripts/shared.py", "journal", manifests)
    assert conflict is not None


def test_conflict_detected_via_string_cron_form() -> None:
    """Raw crontab-string cron entries also get scanned."""
    manifests = [
        {"id": "legacy",
         "crons": ["0 5 * * * python3 scripts/shared.py >/dev/null"]},
    ]
    conflict = find_conflicts("scripts/shared.py", "journal", manifests)
    assert conflict is not None


def test_auditing_apps_own_files_dont_conflict() -> None:
    """A manifest's own files don't count as conflicts with themselves."""
    manifests = [
        {"id": "journal", "files": [{"path": "scripts/journal.py"}]},
    ]
    conflict = find_conflicts("scripts/journal.py", "journal", manifests)
    assert conflict is None


def test_deprecated_apps_excluded_from_conflicts() -> None:
    """Apps in deprecated/hidden/dormant states shouldn't block fixes."""
    manifests = [
        {"id": "old", "status": "deprecated",
         "files": [{"path": "scripts/shared.py"}]},
    ]
    conflict = find_conflicts("scripts/shared.py", "journal", manifests)
    assert conflict is None


def test_path_normalization_in_conflict_check() -> None:
    """Leading slashes / dotslash normalize so manifests authored differently
    still get caught."""
    manifests = [
        {"id": "other", "files": [{"path": "./scripts/x.py"}]},
    ]
    # Target with leading slash matches "./scripts/x.py"
    conflict = find_conflicts("/scripts/x.py", "journal", manifests)
    assert conflict is not None


def test_multiple_conflicting_apps_all_listed() -> None:
    manifests = [
        {"id": "a", "files": [{"path": "scripts/shared.py"}]},
        {"id": "b", "dependencies": [{"path": "scripts/shared.py"}]},
        {"id": "c", "crons": [{"schedule": "@daily", "script": "scripts/shared.py"}]},
    ]
    conflict = find_conflicts("scripts/shared.py", "journal", manifests)
    assert conflict is not None
    ids = {a["app_id"] for a in conflict.affected_apps}
    roles = {a["role"] for a in conflict.affected_apps}
    assert ids == {"a", "b", "c"}
    assert "owner" in roles
    assert "dependency" in roles
    assert "cron_script" in roles


# ── execute_auto_fix ────────────────────────────────────────────────────────


def test_execute_auto_fix_unknown_transformation_returns_not_applied() -> None:
    """In v1 the whitelist is empty; every transformation falls through to
    not-applied so the runner converts the auto_fix to a propose."""
    result = execute_auto_fix(
        transformation="manifest_path_update",
        manifest={"id": "j", "files": []},
        workspace=Path("/tmp"),
        evidence={"path": "scripts/x.py"},
        other_manifests=[],
    )
    assert result.applied is False
    assert "whitelist" in result.summary
    assert result.conflict is None


def test_execute_auto_fix_conflict_short_circuits_before_whitelist_check() -> None:
    """A cross-app conflict must surface even when the transformation isn't
    on the whitelist — the operator needs to know about the dependency."""
    result = execute_auto_fix(
        transformation="manifest_path_update",
        manifest={"id": "journal"},
        workspace=Path("/tmp"),
        evidence={"path": "scripts/shared.py"},
        other_manifests=[
            {"id": "other", "files": [{"path": "scripts/shared.py"}]},
        ],
    )
    assert result.applied is False
    assert result.conflict is not None
    assert result.conflict.file_path == "scripts/shared.py"
    assert "cross-app conflict" in result.summary


def test_execute_auto_fix_no_path_in_evidence_skips_conflict_check() -> None:
    """Some transformations don't touch a file path (e.g. manifest metadata).
    No path → no conflict check; the executor still falls through to whitelist
    rejection in v1."""
    result = execute_auto_fix(
        transformation="manifest_metadata_fix",
        manifest={"id": "j"},
        workspace=Path("/tmp"),
        evidence={"field": "last_reviewed"},   # no path
        other_manifests=[
            {"id": "other", "files": [{"path": "scripts/x.py"}]},
        ],
    )
    assert result.applied is False
    assert result.conflict is None
