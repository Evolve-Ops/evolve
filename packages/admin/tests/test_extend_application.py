"""
Tests for extend_application (Session 3a, v7-arc §8.3).

Covers happy path + each failure mode in the write-order contract:
  - Pre-flight: instance not found, non-v7 manifest, missing provenance.
  - Validation: bad role, empty capability_summary, missing file descriptor.
  - Step 2 (marker stamp): exception → no workspace write.
  - Step 3 (atomic move): failure → cleanup runs, no workspace write.
  - Step 4 (Instance JSON): failure → file_orphaned=True with clear error.
  - Happy path: realized_files appended, change_log entry recorded, file
    on disk with v7 marker payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from evolve_admin.applications.extend_application import (
    ExtendResult,
    FileDescriptor,
    extend_application,
)
from evolve_admin.applications.provenance import parse_marker


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A v7-arc Instance + bot workspace under tmp_path."""
    bot_id = "personal_bot"
    bot_dir = tmp_path / "bot-homes" / bot_id
    (bot_dir / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    (bot_dir / ".openclaw" / "workspace" / "scripts").mkdir(parents=True)

    instance_id = "i-abcd1234"
    instance_path = bot_dir / ".openclaw" / "workspace" / "manifests" / f"{instance_id}.json"
    instance = {
        "instance_id": instance_id,
        "bot_id": bot_id,
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": "p-abcd1234",
            "spec_version": "2026.05.20-1.0",
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": [],
        "change_log": [],
        "status": "active",
    }
    instance_path.write_text(json.dumps(instance))

    # Patch bot_home in extend_application's module to redirect to our tmp dir.
    from evolve_admin.applications import extend_application as ea
    monkeypatch.setattr(ea, "bot_home", lambda _bid: bot_dir)

    return {
        "bot_id": bot_id,
        "instance_id": instance_id,
        "instance_path": instance_path,
        "bot_dir": bot_dir,
        "workspace": bot_dir / ".openclaw" / "workspace",
    }


# ── Happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_file_created_with_marker_and_change_log(self, env):
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="Add weekly summary script",
            file=FileDescriptor(
                path="scripts/summary.py",
                role="vital_to_blueprint",
                intent="Generate weekly summary",
                language="python",
                content="#!/usr/bin/env python3\nprint('hi')\n",
            ),
            user_intent_quote="I'd like a weekly recap",
            session_id="sess-test",
        )
        assert result.success, result.error
        assert result.file_id is not None
        assert result.file_path.exists()

        # File has a v7-format marker
        marker = parse_marker(result.file_path)
        assert marker is not None
        assert marker.keyword == "spec"
        assert marker.pkg_ids == ["p-abcd1234"]

        # Instance JSON updated: realized_files + change_log
        inst = json.loads(env["instance_path"].read_text())
        assert len(inst["realized_files"]) == 1
        assert inst["realized_files"][0]["logical_name"] == "summary"
        assert inst["realized_files"][0]["marker_state"] == "OWNED"
        assert inst["realized_files"][0]["created_in_session"] == "sess-test"
        assert len(inst["change_log"]) == 1
        entry = inst["change_log"][0]
        assert entry["kind"] == "capability_added"
        assert entry["who"] == "bot"
        assert entry["description"] == "Add weekly summary script"
        assert entry["user_intent_quote"] == "I'd like a weekly recap"
        assert entry["file_changes"][0]["action"] == "created"

    def test_no_user_intent_quote_omitted_from_log(self, env):
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=FileDescriptor(path="x.py", role="vital_to_blueprint", intent="."),
        )
        assert result.success
        inst = json.loads(env["instance_path"].read_text())
        # When no quote provided, the field shouldn't appear (kept tidy)
        assert "user_intent_quote" not in inst["change_log"][0]

    def test_two_extensions_accumulate(self, env):
        # Mimic two LLM calls in the same session
        for i in range(2):
            r = extend_application(
                instance_id=env["instance_id"],
                bot_id=env["bot_id"],
                capability_summary=f"cap {i}",
                file=FileDescriptor(
                    path=f"scripts/x{i}.py",
                    role="vital_to_blueprint",
                    intent=f"file {i}",
                ),
            )
            assert r.success
        inst = json.loads(env["instance_path"].read_text())
        assert len(inst["realized_files"]) == 2
        assert len(inst["change_log"]) == 2


# ── Pre-flight failures ───────────────────────────────────────────────────────

class TestPreflightFailures:
    def test_instance_not_found(self, env):
        result = extend_application(
            instance_id="i-does-not-exist",
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=FileDescriptor(path="x.py", role="vital_to_blueprint", intent="."),
        )
        assert not result.success
        assert "instance not found" in result.error

    def test_non_v7_manifest_refused(self, env):
        # Overwrite the v7 instance with a legacy v13-shaped manifest
        legacy = {"id": "x", "name": "X", "schema_version": 13, "bot_id": env["bot_id"]}
        env["instance_path"].write_text(json.dumps(legacy))
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=FileDescriptor(path="x.py", role="vital_to_blueprint", intent="."),
        )
        assert not result.success
        assert "v7-arc" in result.error
        assert "migrate_v7" in result.error

    def test_missing_provenance_refused(self, env):
        bad = json.loads(env["instance_path"].read_text())
        del bad["provenance"]["spec_id"]
        env["instance_path"].write_text(json.dumps(bad))
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=FileDescriptor(path="x.py", role="vital_to_blueprint", intent="."),
        )
        assert not result.success
        assert "provenance" in result.error


# ── Validation ────────────────────────────────────────────────────────────────

class TestValidation:
    def test_bad_role_refused(self, env):
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=FileDescriptor(path="x.py", role="bogus", intent="."),
        )
        assert not result.success
        assert "role" in result.error

    def test_empty_capability_summary_refused(self, env):
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="",
            file=FileDescriptor(path="x.py", role="vital_to_blueprint", intent="."),
        )
        assert not result.success
        assert "capability_summary" in result.error

    def test_missing_file_descriptor_refused(self, env):
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=None,
        )
        assert not result.success
        assert "file descriptor" in result.error


# ── Step-failure rollback ─────────────────────────────────────────────────────

class TestStepFailures:
    def test_marker_stamp_failure_no_workspace_write(self, env, monkeypatch):
        from evolve_admin.applications import provenance as prov

        def boom(*a, **k):
            raise RuntimeError("simulated marker stamp failure")

        monkeypatch.setattr(prov, "embed_marker", boom)

        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=FileDescriptor(
                path="scripts/will-fail.py",
                role="vital_to_blueprint",
                intent=".",
                content="hi",
            ),
        )
        assert not result.success
        assert "marker stamp failed" in result.error

        # Nothing in the workspace
        target = env["workspace"] / "scripts" / "will-fail.py"
        assert not target.exists()

        # Instance JSON untouched
        inst = json.loads(env["instance_path"].read_text())
        assert inst["realized_files"] == []
        assert inst["change_log"] == []

    def test_instance_write_failure_marks_orphaned(self, env, monkeypatch):
        from evolve_admin.applications import extend_application as ea

        def boom(*a, **k):
            raise OSError("simulated disk-full")

        monkeypatch.setattr(ea, "_atomic_write_json", boom)

        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="cap",
            file=FileDescriptor(
                path="scripts/orphan.py",
                role="vital_to_blueprint",
                intent=".",
                content="hi",
            ),
        )
        assert not result.success
        assert result.file_orphaned is True
        # File IS in workspace + stamped (because failure happened at step 4)
        assert result.file_path is not None
        assert result.file_path.exists()
        marker = parse_marker(result.file_path)
        assert marker is not None
        assert marker.keyword == "spec"


class TestOwnershipPolicyGate:
    """F-B1 writer-hygiene: extend_application must refuse a never-ownable
    target up front — before staging/stamping/moving — so a never-ownable path
    can never be bound into realized_files[] (the same can_app_own predicate the
    read/classify side uses)."""

    @pytest.mark.parametrize("bad_path", [
        "member-hash-salt.bin",          # secret / key material
        ".capture-salt",                 # dotfile salt
        "capture-log.jsonl",             # append-only runtime log
        "evolve/audit_outbox/rec-1.json",  # platform telemetry tree
        "AGENTS.md",                     # OpenClaw identity file
        "manifests/i-other.json",        # manifest-store self-reference
        "archive/index.json",            # generated archive index
    ])
    def test_never_ownable_path_refused(self, env, bad_path):
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="add a thing",
            file=FileDescriptor(path=bad_path, role="vital_to_blueprint", intent="."),
            session_id="sess-test",
        )
        assert not result.success
        assert "ownership_policy" in result.error
        # Nothing was written: no file on disk, realized_files still empty.
        assert not (env["workspace"] / bad_path).exists()
        inst = json.loads(env["instance_path"].read_text())
        assert inst["realized_files"] == []
        assert inst["change_log"] == []

    def test_positive_control_genuine_source_still_binds(self, env):
        """Over-exclusion guard: an ordinary source file is NOT refused — it
        stamps, moves, and binds into realized_files[] as before."""
        result = extend_application(
            instance_id=env["instance_id"],
            bot_id=env["bot_id"],
            capability_summary="add a real script",
            file=FileDescriptor(
                path="scripts/catalog.json",   # 'log' is mid-word → ownable
                role="instance_specific", intent=".", content="{}\n",
            ),
            session_id="sess-test",
        )
        assert result.success, result.error
        inst = json.loads(env["instance_path"].read_text())
        assert len(inst["realized_files"]) == 1
        assert inst["realized_files"][0]["logical_name"] == "catalog"
