"""
Tests for Lessons compression (S4d, v7-arc §6 + §8.1.4).

Covers:
  - happy path: capability_added with user_request → new Lesson
  - per-kind minimum enforcement (no quantitative evidence → skip)
  - no reason_signals → skip (silent narration rejection)
  - unmappable kinds → skip (data_refresh, scanner_inferred, etc.)
  - idempotency: re-running picks up only entries since last window.end
  - proposed_spec_change emitted with right shape per kind
  - schema validation against manifest-v7-lessons.schema.json
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from evolve_admin.applications.lessons_compress import (
    LessonsCompressionResult,
    compress_changelog,
)


# ── Schema for validating produced Lessons ────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[3]
LESSONS_SCHEMA = json.loads(
    (_REPO_ROOT / "docs" / "schemas" / "manifest-v7-lessons.schema.json").read_text()
)


def _validate(data: dict) -> list[str]:
    validator = jsonschema.Draft7Validator(LESSONS_SCHEMA)
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message[:120]}"
        for err in validator.iter_errors(data)
    ]


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _empty_lessons(spec_id="p-abcd1234", bot_id="team_bot_a",
                   spec_version="2026.05.20-1.0") -> dict:
    return {
        "lessons_id": "l-1234abcd",
        "spec_id": spec_id,
        "spec_version_observed": spec_version,
        "source_pod_id": "pod-test",
        "source_bot_id": bot_id,
        "observation_window": {
            "start": "2026-05-20T00:00:00Z",
            "end": "2026-05-20T00:00:00Z",
            "instance_runs": 0,
        },
        "lessons": [],
        "redaction_applied": False,
    }


def _instance_with_changelog(entries: list[dict], spec_id="p-abcd1234",
                              bot_id="team_bot_a") -> dict:
    return {
        "instance_id": "i-test1234",
        "bot_id": bot_id,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": "2026.05.20-1.0",
        },
        "change_log": entries,
    }


def _entry(
    kind: str,
    timestamp: str,
    description: str = "did the thing",
    reason_signals: list | None = None,
    user_intent_quote: str = "",
    file_changes: list | None = None,
    entry_id: str | None = None,
) -> dict:
    e: dict = {
        "entry_id": entry_id or f"log-{timestamp.replace(':', '').replace('-', '')}-{kind[:3]}",
        "timestamp": timestamp,
        "kind": kind,
        "who": "bot",
        "session_id": "sess-x",
        "description": description,
    }
    if reason_signals is not None:
        e["reason_signals"] = reason_signals
    if user_intent_quote:
        e["user_intent_quote"] = user_intent_quote
    if file_changes is not None:
        e["file_changes"] = file_changes
    return e


# ── Happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_capability_added_with_user_request_produces_new_capability_lesson(self):
        instance = _instance_with_changelog([
            _entry(
                "capability_added",
                "2026-05-21T10:00:00Z",
                description="Added fiber tracking",
                reason_signals=[{"signal_type": "user_request", "ref": "msg-abc"}],
                user_intent_quote="also track my fiber",
            ),
        ])
        lessons = _empty_lessons()
        updated, result = compress_changelog(instance, spec={}, lessons=lessons)

        assert result.new_lessons_count == 1
        assert result.skipped_no_evidence == 0
        assert len(updated["lessons"]) == 1
        l = updated["lessons"][0]
        assert l["kind"] == "new_capability"
        assert "fiber" in l["summary"].lower()
        assert "user_intent_quote" not in l  # quote folded into summary
        assert l["evidence"][0]["type"] == "user_request"
        assert l["evidence"][0]["ref"] == "msg-abc"
        assert l["source_change_log_entry_id"]
        # proposed_spec_change is emitted for capability_added
        assert l["proposed_spec_change"]["kind"] == "capability_addition"

    def test_blueprint_correction_with_error_count_produces_correction_lesson(self):
        instance = _instance_with_changelog([
            _entry(
                "blueprint_correction",
                "2026-05-22T11:00:00Z",
                description="Decimal handling in ingest regex",
                reason_signals=[{"signal_type": "error_count", "ref": "usage.errors",
                                 "value": 14}],
                file_changes=[{"action": "modified", "path": "scripts/ingest.py"}],
            ),
        ])
        updated, result = compress_changelog(instance, {}, _empty_lessons())
        assert result.new_lessons_count == 1
        l = updated["lessons"][0]
        assert l["kind"] == "blueprint_correction"
        assert l["evidence"][0]["type"] == "error_count"
        assert l["evidence"][0]["value"] == 14
        assert l["proposed_spec_change"]["kind"] == "fix"
        assert "ingest.py" in l["proposed_spec_change"]["target"]

    def test_produced_lessons_file_validates_against_schema(self):
        instance = _instance_with_changelog([
            _entry("capability_added", "2026-05-21T10:00:00Z",
                   description="Add fiber tracking",
                   reason_signals=[{"signal_type": "user_request", "ref": "msg-1"}]),
            _entry("blueprint_correction", "2026-05-22T11:00:00Z",
                   description="Regex fix",
                   reason_signals=[{"signal_type": "error_count", "ref": "errs",
                                    "value": 5}],
                   file_changes=[{"action": "modified", "path": "x.py"}]),
        ])
        updated, _ = compress_changelog(instance, {}, _empty_lessons())
        errors = _validate(updated)
        assert errors == [], "\n".join(errors)


# ── Per-kind evidence minimums ────────────────────────────────────────────────

class TestPerKindMinimums:
    def test_new_capability_without_user_evidence_skipped(self):
        # error_count is NOT enough for new_capability — only user_request/complaint
        instance = _instance_with_changelog([
            _entry("capability_added", "2026-05-21T10:00:00Z",
                   description="Added fiber",
                   reason_signals=[{"signal_type": "error_count", "ref": "errs", "value": 5}]),
        ])
        _, result = compress_changelog(instance, {}, _empty_lessons())
        assert result.new_lessons_count == 0
        assert result.skipped_per_kind_minimum == 1

    def test_blueprint_correction_with_only_user_request_skipped(self):
        # user_request is NOT enough for blueprint_correction
        instance = _instance_with_changelog([
            _entry("blueprint_correction", "2026-05-21T10:00:00Z",
                   reason_signals=[{"signal_type": "user_request", "ref": "msg"}]),
        ])
        _, result = compress_changelog(instance, {}, _empty_lessons())
        assert result.new_lessons_count == 0
        assert result.skipped_per_kind_minimum == 1

    def test_user_complaint_satisfies_new_capability(self):
        # Per spec §6 hard rule: either user_request OR user_complaint counts
        instance = _instance_with_changelog([
            _entry("capability_added", "2026-05-21T10:00:00Z",
                   reason_signals=[{"signal_type": "user_complaint", "ref": "msg"}]),
        ])
        _, result = compress_changelog(instance, {}, _empty_lessons())
        assert result.new_lessons_count == 1


# ── Filter rejections ────────────────────────────────────────────────────────

class TestRejections:
    def test_no_reason_signals_skipped(self):
        # The strict rule: "LLM narration without one of these is rejected"
        instance = _instance_with_changelog([
            _entry("capability_added", "2026-05-21T10:00:00Z",
                   description="we learned X works better",
                   reason_signals=[]),
        ])
        _, result = compress_changelog(instance, {}, _empty_lessons())
        assert result.new_lessons_count == 0
        assert result.skipped_no_evidence == 1

    def test_unmappable_kinds_skipped(self):
        # data_refresh / scanner_inferred / forge_rebuild / user_redirect
        # don't have a Lesson-kind mapping
        instance = _instance_with_changelog([
            _entry("data_refresh", "2026-05-21T10:00:00Z",
                   reason_signals=[{"signal_type": "user_request", "ref": "x"}]),
            _entry("scanner_inferred", "2026-05-21T11:00:00Z",
                   reason_signals=[{"signal_type": "error_count", "ref": "x", "value": 1}]),
            _entry("user_redirect", "2026-05-21T12:00:00Z",
                   reason_signals=[{"signal_type": "user_request", "ref": "x"}]),
        ])
        _, result = compress_changelog(instance, {}, _empty_lessons())
        assert result.new_lessons_count == 0
        assert result.skipped_unmappable_kind == 3


# ── Idempotency ──────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_window_end_advances_and_skips_already_seen(self):
        lessons = _empty_lessons()
        # First run picks up the early entry
        instance = _instance_with_changelog([
            _entry("capability_added", "2026-05-21T10:00:00Z",
                   reason_signals=[{"signal_type": "user_request", "ref": "msg-1"}]),
        ])
        updated, r1 = compress_changelog(instance, {}, lessons)
        assert r1.new_lessons_count == 1
        assert updated["observation_window"]["end"] == "2026-05-21T10:00:00Z"

        # Second run on the same data is a no-op
        _, r2 = compress_changelog(instance, {}, updated)
        assert r2.new_lessons_count == 0

        # Adding a later entry, then re-running, picks up only the new one
        instance["change_log"].append(_entry(
            "capability_added", "2026-05-22T10:00:00Z",
            reason_signals=[{"signal_type": "user_request", "ref": "msg-2"}],
        ))
        updated, r3 = compress_changelog(instance, {}, updated)
        assert r3.new_lessons_count == 1
        assert updated["observation_window"]["end"] == "2026-05-22T10:00:00Z"
        # Total lessons accumulates
        assert len(updated["lessons"]) == 2


# ── End-to-end (file I/O) ─────────────────────────────────────────────────────

class TestFileIO:
    def test_compress_instance_writes_lessons_file(self, tmp_path, monkeypatch):
        """compress_instance reads disk + writes the Lessons file."""
        from evolve_admin.applications import lessons_compress as lc

        # Stand up an Instance file
        instance_path = tmp_path / "i-test.json"
        instance_path.write_text(json.dumps(_instance_with_changelog([
            _entry("capability_added", "2026-05-21T10:00:00Z",
                   description="Add weekly recap",
                   reason_signals=[{"signal_type": "user_request", "ref": "msg-x"}]),
        ])))

        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        result = lc.compress_instance(instance_path, shared_dir, dry_run=False)
        assert result is not None
        assert result.new_lessons_count == 1

        # Lessons file landed at the expected pod-wide path
        expected_path = shared_dir / "lessons" / "team_bot_a" / "p-abcd1234.json"
        assert expected_path.exists()
        lessons = json.loads(expected_path.read_text())
        assert len(lessons["lessons"]) == 1
        assert lessons["lessons"][0]["kind"] == "new_capability"

    def test_dry_run_does_not_write(self, tmp_path):
        from evolve_admin.applications import lessons_compress as lc

        instance_path = tmp_path / "i-test.json"
        instance_path.write_text(json.dumps(_instance_with_changelog([
            _entry("capability_added", "2026-05-21T10:00:00Z",
                   reason_signals=[{"signal_type": "user_request", "ref": "msg"}]),
        ])))
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        result = lc.compress_instance(instance_path, shared_dir, dry_run=True)
        assert result.new_lessons_count == 1  # would have written
        # But the file doesn't exist
        assert not (shared_dir / "lessons" / "team_bot_a" / "p-abcd1234.json").exists()

    def test_skip_non_v7_instance(self, tmp_path):
        from evolve_admin.applications import lessons_compress as lc

        # Legacy v13 — should be skipped (return None)
        instance_path = tmp_path / "legacy.json"
        instance_path.write_text(json.dumps({
            "id": "legacy", "name": "Legacy", "schema_version": 13,
        }))
        result = lc.compress_instance(instance_path, tmp_path, dry_run=False)
        assert result is None
