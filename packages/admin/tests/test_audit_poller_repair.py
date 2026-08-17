"""Audit-poller dispatch tests for repair_applied / repair_failed records.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §11.3.

The bot's app_repair_runner writes outbox records of kind
``repair_applied`` / ``repair_failed``; the admin's poller turns them
into changelog entries and (for repair_applied) emits arbiter Proposals
for leftover non-mechanical fixes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402
from evolve_admin.applications.app_changelog import (  # noqa: E402
    KIND_REPAIR_APPLIED,
    KIND_REPAIR_FAILED,
    read_trail,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch) -> Path:
    """Re-point audit_poller's /Users paths into tmp_path."""
    def _audit_outbox(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
            / "evolve" / "audit_outbox"
        )

    def _audit_ingested(bot_user: str) -> Path:
        return _audit_outbox(bot_user) / "_ingested"

    def _audits_dir(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
            / "evolve" / "audits"
        )

    monkeypatch.setattr(audit_poller, "_audit_outbox_dir", _audit_outbox)
    monkeypatch.setattr(
        audit_poller, "_audit_outbox_ingested", _audit_ingested,
    )
    monkeypatch.setattr(audit_poller, "_audits_dir_for_bot", _audits_dir)
    return tmp_path


def _outbox(tmp_root: Path, bot_user: str) -> Path:
    d = (
        tmp_root / "Users" / bot_user / ".openclaw" / "workspace"
        / "evolve" / "audit_outbox"
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audits(tmp_root: Path, bot_user: str) -> Path:
    return (
        tmp_root / "Users" / bot_user / ".openclaw" / "workspace"
        / "evolve" / "audits"
    )


def _write_repair_applied(
    outbox: Path, *, bot_id: str, app_id: str,
    request_id: str = "repair-abc12345",
    transformations: list | None = None,
    proposals: list | None = None,
) -> Path:
    rec = {
        "kind":                    "repair_applied",
        "request_id":              request_id,
        "app_id":                  app_id,
        "bot_id":                  bot_id,
        "ts":                      "2026-06-06T12:00:00Z",
        "runner_version":          "1.0.0",
        "status":                  "ok",
        "applied_transformations": transformations if transformations is not None else [
            {"kind":       "reinstall_cron_from_manifest",
             "finding_id": "f1",
             "summary":    "installed 1 cron entry",
             "trail_entry": {"added_lines": ["0 6 * * * python3 /tmp/run.py"]},
             "applied_at":  "2026-06-06T12:00:01Z"},
        ],
        "proposals":               proposals or [],
        "error":                   "",
        "duration_ms":             1234,
        "tokens":                  {"input": 0, "output": 0, "total": 2500},
    }
    p = outbox / f"repair-applied-{request_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def _write_repair_failed(
    outbox: Path, *, bot_id: str, app_id: str,
    request_id: str = "repair-fail0001",
    error: str = "LLM dispatch timeout after 180s",
) -> Path:
    rec = {
        "kind":                    "repair_failed",
        "request_id":              request_id,
        "app_id":                  app_id,
        "bot_id":                  bot_id,
        "ts":                      "2026-06-06T12:00:00Z",
        "runner_version":          "1.0.0",
        "status":                  "failed",
        "applied_transformations": [],
        "proposals":               [],
        "error":                   error,
        "duration_ms":             180_000,
        "tokens":                  {"input": 0, "output": 0, "total": 0},
    }
    p = outbox / f"repair-failed-{request_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


# ── repair_applied ────────────────────────────────────────────────────────


def test_repair_applied_writes_changelog_entry(tmp_root: Path) -> None:
    bot = "team_bot_a"
    out = _outbox(tmp_root, bot)
    _write_repair_applied(
        out, bot_id=bot, app_id="journal", request_id="repair-ABC123",
    )
    shared = tmp_root / "shared"
    shared.mkdir()

    result = audit_poller.poll_bot(bot, bot, shared)

    assert result.repair_applied == 1
    assert result.files_processed == 1

    trail = read_trail(_audits(tmp_root, bot), "journal")
    repair_entries = [e for e in trail if e.get("kind") == KIND_REPAIR_APPLIED]
    assert len(repair_entries) == 1
    assert repair_entries[0]["request_id"] == "repair-ABC123"
    assert len(repair_entries[0]["transformations"]) == 1


def test_repair_applied_archives_file(tmp_root: Path) -> None:
    bot = "team_bot_a"
    out = _outbox(tmp_root, bot)
    path = _write_repair_applied(
        out, bot_id=bot, app_id="journal", request_id="repair-DEF456",
    )
    shared = tmp_root / "shared"
    shared.mkdir()

    audit_poller.poll_bot(bot, bot, shared)
    assert not path.exists()


# ── repair_failed ─────────────────────────────────────────────────────────


def test_repair_failed_writes_changelog_entry(tmp_root: Path) -> None:
    bot = "team_bot_a"
    out = _outbox(tmp_root, bot)
    _write_repair_failed(
        out, bot_id=bot, app_id="journal", request_id="repair-FAIL01",
        error="LLM dispatch timeout after 180s",
    )
    shared = tmp_root / "shared"
    shared.mkdir()

    result = audit_poller.poll_bot(bot, bot, shared)
    assert result.repair_failed == 1
    assert result.files_processed == 1

    trail = read_trail(_audits(tmp_root, bot), "journal")
    failed_entries = [e for e in trail if e.get("kind") == KIND_REPAIR_FAILED]
    assert len(failed_entries) == 1
    assert failed_entries[0]["request_id"] == "repair-FAIL01"
    assert "timeout" in failed_entries[0]["error"]


def test_repair_record_missing_required_fields_left_in_outbox(
    tmp_root: Path,
) -> None:
    """A repair_applied record missing app_id is treated as a failed ingest
    and the file stays in the outbox (so an operator can investigate)."""
    bot = "team_bot_a"
    out = _outbox(tmp_root, bot)
    # Missing app_id
    rec = {
        "kind":       "repair_applied",
        "request_id": "repair-incomplete",
        "bot_id":     bot,
        "status":     "ok",
        "applied_transformations": [],
        "proposals":  [],
    }
    path = out / "repair-applied-incomplete.json"
    path.write_text(json.dumps(rec))

    shared = tmp_root / "shared"
    shared.mkdir()

    result = audit_poller.poll_bot(bot, bot, shared)
    # Ingest returns False → archive doesn't fire → file stays put.
    assert path.exists()
    assert result.repair_applied == 0
