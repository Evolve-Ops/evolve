"""tests/test_manifest_reflex_runner.py — runner end-to-end.

Covers:
  1. process_bot creates a fresh manifest with source=bot_created
  2. update=true merges files/crons into existing manifest + appends history
  3. Queue is rewritten to drop applied rows; archive grows
  4. Bad rows go to archive as failed; queue advances
  5. dry-run keeps rows pending and writes nothing
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def home_override(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("EVOLVE_DEFER_HOME_OVERRIDE", tmp)
        yield Path(tmp)


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture(autouse=True)
def stub_workspace(monkeypatch):
    """No real workspace path resolution in tests — keep file paths as-passed."""
    import manifest_reflex_runner as mrr
    monkeypatch.setattr(mrr, "_resolve_workspace", lambda bot_id: None)


def _make_row(bot_id, app_id, **kwargs):
    from manifest_reflex_queue import new_row
    return new_row(bot_id, app_id, **kwargs)


class TestApplyRowCreate:
    def test_creates_manifest_with_bot_created_source(self, home_override, shared_dir):
        from manifest_reflex_runner import process_bot
        from evolve_admin.applications.manifest import (
            applications_dir, MANIFEST_SOURCE_BOT_CREATED,
        )

        row = _make_row(
            "admin_bot", "protein-tracker",
            name="Protein Tracker",
            purpose="Daily protein intake log",
            files=["workspace/ops/tools/protein.py", "workspace/ops/data/protein.json"],
            crons=[{"schedule": "0 21 * * *", "script": "workspace/ops/tools/protein.py"}],
            session_id="sess-abc",
        )
        summary = process_bot("admin_bot", [row], shared_dir)
        assert summary == {"checked": 1, "applied": 1, "failed": 0, "kept": 0}

        manifest_path = applications_dir(shared_dir, "admin_bot") / "protein-tracker.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["id"] == "protein-tracker"
        assert data["bot_id"] == "admin_bot"
        assert data["name"] == "Protein Tracker"
        assert data["source"] == MANIFEST_SOURCE_BOT_CREATED
        assert "session:sess-abc" in data["source_detail"]
        assert "ops/tools/protein.py" in data["files"]
        # workspace/ prefix was stripped during normalization
        assert data["files"][0] == "ops/tools/protein.py"
        assert len(data["crons"]) == 1
        assert data["crons"][0]["schedule"] == "0 21 * * *"
        assert data["confidence"] == 1.0
        assert data["status"] == "active"

    def test_strips_absolute_paths_when_workspace_known(self, monkeypatch, home_override, shared_dir):
        from manifest_reflex_runner import process_bot
        from evolve_admin.applications.manifest import applications_dir

        # Point _resolve_workspace at a fake workspace under the tmp.
        fake_ws = home_override / "admin_bot" / "workspace"
        fake_ws.mkdir(parents=True, exist_ok=True)
        import manifest_reflex_runner as mrr
        monkeypatch.setattr(mrr, "_resolve_workspace", lambda bot_id: fake_ws)

        row = _make_row(
            "admin_bot", "absolute-app",
            files=[str(fake_ws / "ops/script.py")],
        )
        process_bot("admin_bot", [row], shared_dir)
        data = json.loads((applications_dir(shared_dir, "admin_bot") / "absolute-app.json").read_text())
        assert data["files"] == ["ops/script.py"]


class TestApplyRowUpdate:
    def test_update_merges_files_and_appends_history(self, home_override, shared_dir):
        from manifest_reflex_runner import process_bot
        from evolve_admin.applications.manifest import applications_dir

        # First, create.
        first = _make_row("admin_bot", "habits", files=["ops/tools/habits.py"])
        process_bot("admin_bot", [first], shared_dir)

        # Now update with an additional file and a new cron.
        update_row = _make_row(
            "admin_bot", "habits",
            files=["ops/tools/habits.py", "ops/data/habits.json"],
            crons=[{"schedule": "@daily", "script": "ops/tools/habits.py"}],
            update=True,
            session_id="sess-update",
        )
        summary = process_bot("admin_bot", [update_row], shared_dir)
        assert summary["applied"] == 1

        data = json.loads((applications_dir(shared_dir, "admin_bot") / "habits.json").read_text())
        # Files: original + new (deduped)
        assert sorted(data["files"]) == ["ops/data/habits.json", "ops/tools/habits.py"]
        # Cron added
        assert any(c.get("schedule") == "@daily" for c in data["crons"])
        # History entry appended
        assert any(h.get("kind") == "manifest_reflex_update" for h in data.get("improvement_history", []))

    def test_update_when_no_existing_creates_fresh(self, home_override, shared_dir):
        from manifest_reflex_runner import process_bot
        from evolve_admin.applications.manifest import applications_dir

        row = _make_row("admin_bot", "no-prior", files=["x.py"], update=True)
        summary = process_bot("admin_bot", [row], shared_dir)
        # Should have created (not failed) — update on missing falls through to create.
        assert summary["applied"] == 1
        assert (applications_dir(shared_dir, "admin_bot") / "no-prior.json").exists()


class TestQueueLifecycle:
    def test_applied_rows_archived_and_dropped(self, home_override, shared_dir):
        from manifest_reflex_runner import run_once
        from manifest_reflex_queue import (
            append_row, archive_path, queue_path, read_queue,
        )

        for app_id in ("a", "b", "c"):
            append_row(_make_row("admin_bot", app_id))

        cfg = {"bots": {"admin_bot": {}}, "sharedDir": str(shared_dir)}
        totals = run_once(cfg)
        assert totals["applied"] == 3
        assert totals["failed"] == 0
        # Queue is empty after processing.
        assert read_queue("admin_bot") == []
        # Queue file may exist as empty or be rewritten; either way no rows.
        if queue_path("admin_bot").exists():
            assert queue_path("admin_bot").read_text().strip() == ""
        # Archive has three rows.
        archive = archive_path("admin_bot").read_text().strip().splitlines()
        assert len(archive) == 3
        assert all(json.loads(line)["status"] == "applied" for line in archive)

    def test_failed_row_archives_and_advances_queue(self, home_override, shared_dir):
        """A poison row archives as failed without blocking later rows."""
        from manifest_reflex_runner import process_bot
        from manifest_reflex_queue import archive_path, read_queue

        good = _make_row("admin_bot", "good")

        # Poison: monkeypatch _apply_row to fail for a specific app_id.
        import manifest_reflex_runner as mrr
        original_apply = mrr._apply_row

        def selective_apply(row, sd):
            if row.app_id == "poison":
                return False, "synthetic failure"
            return original_apply(row, sd)

        with patch.object(mrr, "_apply_row", side_effect=selective_apply):
            poison = _make_row("admin_bot", "poison")
            summary = process_bot("admin_bot", [poison, good], shared_dir)

        assert summary["applied"] == 1
        assert summary["failed"] == 1
        # Both archived, none kept on queue.
        assert read_queue("admin_bot") == []
        archive = archive_path("admin_bot").read_text().strip().splitlines()
        statuses = [json.loads(line)["status"] for line in archive]
        assert sorted(statuses) == ["applied", "failed"]


class TestDryRun:
    def test_dry_run_writes_nothing(self, home_override, shared_dir):
        from manifest_reflex_runner import run_once
        from manifest_reflex_queue import append_row, read_queue
        from evolve_admin.applications.manifest import applications_dir

        append_row(_make_row("admin_bot", "a"))
        cfg = {"bots": {"admin_bot": {}}, "sharedDir": str(shared_dir)}
        totals = run_once(cfg, dry_run=True)
        assert totals == {"bots": 1, "checked": 1, "applied": 0, "failed": 0, "kept": 1}
        # Manifest never written.
        assert not (applications_dir(shared_dir, "admin_bot") / "a.json").exists()
        # Row still pending in the queue (queue not rewritten on dry-run).
        rows = read_queue("admin_bot")
        assert len(rows) == 1
        assert rows[0].status == "pending"


class TestBotCreatedSignal:
    """Spec: docs/spec-manifest-reflex.md Resolution §2 — fresh bot_created
    manifests emit a learning Signal so a downstream generator can ask
    'why didn't I anticipate this?'."""

    def test_fresh_create_emits_signal(self, home_override, shared_dir):
        from manifest_reflex_runner import process_bot
        from signals.store import iter_active

        row = _make_row(
            "admin_bot", "habits",
            name="Habit Tracker",
            purpose="Track daily habits",
            files=["ops/tools/habits.py"],
            session_id="sess-xyz",
        )
        process_bot("admin_bot", [row], shared_dir)

        sigs = list(iter_active(shared_dir, producer="manifest_reflex_runner"))
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.type == "bot_created_app"
        assert sig.bot_id == "admin_bot"
        assert sig.signature == "bot_created_app:admin_bot:habits"
        assert sig.flavor == "activity"
        assert sig.severity == "info"
        assert sig.scope == "bot"
        assert sig.details["app_id"] == "habits"
        assert sig.details["session_id"] == "sess-xyz"
        assert sig.details["files"] == ["ops/tools/habits.py"]

    def test_update_does_not_emit_new_signal(self, home_override, shared_dir):
        """Updating an existing manifest is iteration, not a fresh
        missed-anticipation moment — no new Signal. (observe() may bump
        the existing one if signature happens to match, but we don't call
        observe() on the update path at all.)"""
        from manifest_reflex_runner import process_bot
        from signals.store import iter_active

        # Create.
        process_bot("admin_bot", [_make_row("admin_bot", "habits", files=["a.py"])], shared_dir)
        before = list(iter_active(shared_dir, producer="manifest_reflex_runner"))
        assert len(before) == 1
        before_obs = before[0].observation_count

        # Update.
        process_bot(
            "admin_bot",
            [_make_row("admin_bot", "habits", files=["b.py"], update=True)],
            shared_dir,
        )
        after = list(iter_active(shared_dir, producer="manifest_reflex_runner"))
        # Same signal, observation_count unchanged (no observe() call).
        assert len(after) == 1
        assert after[0].id == before[0].id
        assert after[0].observation_count == before_obs

    def test_failed_save_emits_no_signal(self, home_override, shared_dir, monkeypatch):
        """If save_manifest fails, no Signal — emission must come AFTER
        the manifest lands durably."""
        from manifest_reflex_runner import process_bot
        from signals.store import iter_active

        # Force save_manifest to fail.
        import evolve_admin.applications.manifest as mm
        def boom(*args, **kwargs):
            raise RuntimeError("simulated save failure")
        monkeypatch.setattr(mm, "save_manifest", boom)

        summary = process_bot("admin_bot", [_make_row("admin_bot", "habits")], shared_dir)
        assert summary["failed"] == 1
        assert list(iter_active(shared_dir, producer="manifest_reflex_runner")) == []

    def test_signal_emit_failure_is_swallowed(self, home_override, shared_dir, monkeypatch, capsys):
        """A signal-store error must not fail the row — manifest landing
        is the user-facing requirement; the Signal is bonus telemetry."""
        from manifest_reflex_runner import process_bot
        import signals.store as store

        def boom(*args, **kwargs):
            raise RuntimeError("simulated signal error")
        monkeypatch.setattr(store, "observe", boom)

        summary = process_bot("admin_bot", [_make_row("admin_bot", "habits")], shared_dir)
        assert summary == {"checked": 1, "applied": 1, "failed": 0, "kept": 0}
        captured = capsys.readouterr()
        assert "signal emit failed" in captured.err


class TestBotIdParsing:
    def test_dict_shape(self):
        from manifest_reflex_runner import _bot_ids
        assert _bot_ids({"bots": {"admin_bot": {}, "team_bot_a": {}}}) == ["admin_bot", "team_bot_a"]

    def test_list_shape(self):
        from manifest_reflex_runner import _bot_ids
        assert _bot_ids({"bots": [{"id": "admin_bot"}, {"id": "team_bot_a"}]}) == ["admin_bot", "team_bot_a"]

    def test_empty(self):
        from manifest_reflex_runner import _bot_ids
        assert _bot_ids({}) == []
