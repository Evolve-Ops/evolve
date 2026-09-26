"""Unit tests for app_audit_structural — the pure-function Tier-2 assertions.

These tests construct synthetic manifests + temp workspaces and assert that
each check function produces the expected Finding shape. No filesystem hooks,
no Signal store, no LLM — just the assertion logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

# Ensure the analyzer dir is on sys.path so we can import the under-test module
# the same way the LaunchDaemon plist would.
_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    Finding,
    check_cron_schedules,
    check_cron_scripts_exist,
    check_crons_installed,
    check_files_exist,
    check_files_sha,
    check_python_packages,
    run_all,
    _parse_cron_schedule,
    _normalize_cron_line,
    _cron_soft_match,
)


# ── check_files_exist ────────────────────────────────────────────────────────


def test_files_exist_passes_when_all_present(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/journal.py").write_text("print('hi')")
    manifest = {"files": [{"path": "scripts/journal.py"}]}
    findings = check_files_exist(manifest, {"workspace": tmp_path})
    assert findings == []


def test_files_exist_flags_missing_as_critical(tmp_path: Path) -> None:
    manifest = {"files": [{"path": "scripts/missing.py"}]}
    findings = check_files_exist(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].assertion_id == "file_missing"
    assert findings[0].evidence["path"] == "scripts/missing.py"


def test_files_exist_skips_empty_file_records(tmp_path: Path) -> None:
    """Empty / malformed entries should not produce findings; they're a
    manifest-quality issue handled elsewhere, not a structural break."""
    manifest = {"files": [{"path": ""}, {}, "not-a-dict", {"path": None}]}
    assert check_files_exist(manifest, {"workspace": tmp_path}) == []


# ── check_files_sha ──────────────────────────────────────────────────────────


def _write_with_sha(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_sha_matches_passes(tmp_path: Path) -> None:
    sha = _write_with_sha(tmp_path / "scripts/journal.py", b"print('hi')\n")
    manifest = {"files": [{"path": "scripts/journal.py", "sha256": sha}]}
    assert check_files_sha(manifest, {"workspace": tmp_path}) == []


def test_sha_mismatch_is_major(tmp_path: Path) -> None:
    _write_with_sha(tmp_path / "scripts/journal.py", b"print('mutated')\n")
    manifest = {"files": [{"path": "scripts/journal.py", "sha256": "0" * 64}]}
    findings = check_files_sha(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR
    assert findings[0].assertion_id == "file_sha_mismatch"
    assert findings[0].evidence["expected_sha"] == "0" * 64


def test_sha_skips_data_layer(tmp_path: Path) -> None:
    """Data and state layer files are expected to change between audits."""
    _write_with_sha(tmp_path / "data/journal.jsonl", b"line1\nline2\n")
    manifest = {
        "files": [{
            "path": "data/journal.jsonl",
            "sha256": "0" * 64,
            "layer": "data",
        }]
    }
    assert check_files_sha(manifest, {"workspace": tmp_path}) == []


def test_sha_no_recorded_value_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/journal.py").write_text("x")
    manifest = {"files": [{"path": "scripts/journal.py"}]}  # no sha256 key
    assert check_files_sha(manifest, {"workspace": tmp_path}) == []


def test_sha_does_not_flag_missing_files(tmp_path: Path) -> None:
    """check_files_exist owns the missing-file finding; sha check must not duplicate."""
    manifest = {"files": [{"path": "scripts/missing.py", "sha256": "a" * 64}]}
    assert check_files_sha(manifest, {"workspace": tmp_path}) == []


# ── check_cron_scripts_exist ─────────────────────────────────────────────────


def test_cron_script_exists_dict_form(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/cron.py").write_text("x")
    manifest = {"crons": [{"schedule": "0 2 * * *", "script": "scripts/cron.py"}]}
    assert check_cron_scripts_exist(manifest, {"workspace": tmp_path}) == []


def test_cron_script_missing_is_critical(tmp_path: Path) -> None:
    manifest = {"crons": [{"schedule": "0 2 * * *", "script": "scripts/missing.py"}]}
    findings = check_cron_scripts_exist(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].assertion_id == "cron_script_missing"


def test_cron_script_handles_string_form(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/cron.py").write_text("x")
    manifest = {"crons": ["0 2 * * * python3 scripts/cron.py"]}
    assert check_cron_scripts_exist(manifest, {"workspace": tmp_path}) == []


# ── check_cron_schedules ─────────────────────────────────────────────────────


@pytest.mark.parametrize("schedule", [
    "0 2 * * *",
    "*/5 * * * *",
    "0,30 9-17 * * 1-5",
    "@daily",
    "@reboot",
    "@hourly",
])
def test_cron_schedule_valid_forms_parse(schedule: str) -> None:
    assert _parse_cron_schedule(schedule)


@pytest.mark.parametrize("schedule", [
    "",
    "not a cron",
    "0 2 * *",        # too few fields
    "0 2 * * * *",    # too many fields
    "@bogus",
    "0 2 abc * *",
])
def test_cron_schedule_invalid_forms_rejected(schedule: str) -> None:
    assert not _parse_cron_schedule(schedule)


def test_cron_schedules_finding(tmp_path: Path) -> None:
    manifest = {"crons": [{"schedule": "garbage", "script": "scripts/x.py"}]}
    findings = check_cron_schedules(manifest, {"workspace": tmp_path})
    assert len(findings) == 1
    assert findings[0].assertion_id == "cron_schedule_unparseable"
    assert findings[0].severity == SEVERITY_MAJOR


# ── check_crons_installed ────────────────────────────────────────────────────


def test_cron_installed_strict_match() -> None:
    manifest = {"crons": [{
        "schedule": "0 2 * * *",
        "script": "scripts/journal.py",
    }]}
    ctx = {
        "workspace": Path("/tmp"),
        "crontab_lines": ["0 2 * * * scripts/journal.py"],
    }
    assert check_crons_installed(manifest, ctx) == []


def test_cron_installed_soft_match_with_prefix() -> None:
    """Real crontab lines often have ``cd $HOME && python3 ...`` wrappers.

    The soft match should recognize the schedule + script-basename
    combination even when the live line has prefix tokens.
    """
    manifest = {"crons": [{
        "schedule": "0 2 * * *",
        "script": "scripts/journal.py",
    }]}
    ctx = {
        "workspace": Path("/tmp"),
        "crontab_lines": [
            "0 2 * * * cd $HOME && /opt/homebrew/bin/python3 scripts/journal.py >> log 2>&1"
        ],
    }
    assert check_crons_installed(manifest, ctx) == []


def test_cron_installed_flags_missing() -> None:
    manifest = {"crons": [{
        "schedule": "0 2 * * *",
        "script": "scripts/journal.py",
    }]}
    ctx = {"workspace": Path("/tmp"), "crontab_lines": []}
    findings = check_crons_installed(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].assertion_id == "cron_not_in_crontab"
    assert findings[0].severity == SEVERITY_MAJOR


# 2026-06-08: check_test_command tests removed with the app-test surface.

# ── check_python_packages ────────────────────────────────────────────────────


def test_python_packages_required_missing_is_major() -> None:
    manifest = {"requirements": {"python_packages": [
        {"import": "not_a_real_package_xyz123", "pip_name": "fake", "required": True},
    ]}}
    findings = check_python_packages(manifest, {"workspace": Path("/")})
    assert len(findings) == 1
    assert findings[0].assertion_id == "python_package_import_failed"
    assert findings[0].severity == SEVERITY_MAJOR


def test_python_packages_optional_is_skipped() -> None:
    manifest = {"requirements": {"python_packages": [
        {"import": "not_a_real_package_xyz123", "required": False},
    ]}}
    assert check_python_packages(manifest, {"workspace": Path("/")}) == []


def test_python_packages_stdlib_passes() -> None:
    manifest = {"requirements": {"python_packages": [
        {"import": "json", "required": True},
    ]}}
    assert check_python_packages(manifest, {"workspace": Path("/")}) == []


# ── run_all (aggregation + exception isolation) ──────────────────────────────


def test_run_all_collects_findings_across_assertions(tmp_path: Path) -> None:
    """A manifest with multiple structural problems gets every relevant finding."""
    manifest = {
        "files": [{"path": "scripts/missing.py"}],
        "crons": [{"schedule": "garbage", "script": "scripts/missing.py"}],
    }
    findings = run_all(manifest, {"workspace": tmp_path, "crontab_lines": []})
    ids = {f.assertion_id for f in findings}
    assert "file_missing" in ids
    assert "cron_schedule_unparseable" in ids
    assert "cron_script_missing" in ids


def test_run_all_isolates_assertion_crash() -> None:
    """A broken assertion shouldn't abort the run — its crash should land
    as an info finding and the rest of the assertions should still execute."""
    def _broken(manifest, ctx):
        raise RuntimeError("oh no")

    findings = run_all(
        {}, {"workspace": Path("/"), "crontab_lines": []}, assertions=(_broken,),
    )
    assert len(findings) == 1
    assert findings[0].assertion_id == "assertion_crashed"


# ── Finding.signature ────────────────────────────────────────────────────────


def test_finding_signature_dedupes_per_evidence_key() -> None:
    f1 = Finding(
        assertion_id="file_missing", severity=SEVERITY_CRITICAL,
        summary="x", evidence={"path": "a.py"},
    )
    f2 = Finding(
        assertion_id="file_missing", severity=SEVERITY_CRITICAL,
        summary="x", evidence={"path": "b.py"},
    )
    assert f1.signature("team_bot_a", "journal") != f2.signature("team_bot_a", "journal")
    # Same evidence → same signature (the dedup contract)
    f3 = Finding(
        assertion_id="file_missing", severity=SEVERITY_CRITICAL,
        summary="re-run", evidence={"path": "a.py"},
    )
    assert f1.signature("team_bot_a", "journal") == f3.signature("team_bot_a", "journal")


# ── soft cron matching helper ────────────────────────────────────────────────


def test_cron_soft_match_finds_wrapped_invocation() -> None:
    line = _normalize_cron_line(
        "0 2 * * * cd $HOME && /opt/homebrew/bin/python3 scripts/journal.py"
    )
    assert _cron_soft_match("0 2 * * *", "scripts/journal.py", line)


def test_cron_soft_match_rejects_different_schedule() -> None:
    line = _normalize_cron_line(
        "0 3 * * * cd $HOME && python3 scripts/journal.py"
    )
    assert not _cron_soft_match("0 2 * * *", "scripts/journal.py", line)
