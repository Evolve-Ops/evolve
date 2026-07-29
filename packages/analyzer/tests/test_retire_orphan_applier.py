"""tests/test_retire_orphan_applier.py — RetireOrphan applier (PR9).

The applier archives a workspace orphan's content and appends the path
to the bot's orphan_exclusions.json. The actual file in the bot's
workspace is *not* unlinked — evolve has no delete grant on bot
workspaces, and physical removal would require new infrastructure
(per-bot launchd helper or new sudoers grant) that's out of scope.

Tests pin:
  - Archive + exclusions add (happy path)
  - Idempotent re-apply (same path twice → no-op the second time)
  - File-missing case still completes (exclusion still applied)
  - Path-traversal refusal (action.path resolving outside workspace)
  - Snapshot captures prior exclusions; revert restores them
  - load_exclusions module entry point works for posture-review use
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.appliers import get_applier  # noqa: E402
from arbiter.appliers.retire_orphan import (  # noqa: E402
    load_exclusions, set_shared_dir,
)
from schema.proposal import RetireOrphan  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path):
    """Tmp shared_dir wired into the applier; restored after test."""
    set_shared_dir(tmp_path)
    yield tmp_path
    set_shared_dir(Path("/Users/Shared/evolve"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Pretend the bot's workspace is at tmp_path/bot-fake-home/.openclaw/
    workspace. The applier resolves /Users/<bot>/.openclaw/workspace by
    convention; for tests we monkeypatch the resolver to point at our tmp."""
    fake_workspace = tmp_path / "bot-fake-home" / ".openclaw" / "workspace"
    fake_workspace.mkdir(parents=True)
    import arbiter.appliers.retire_orphan as ro
    monkeypatch.setattr(ro, "_bot_workspace", lambda b: fake_workspace)
    return fake_workspace


def _read_exclusions_file(shared_dir: Path, bot_id: str) -> list:
    path = shared_dir / "app_posture" / bot_id / "orphan_exclusions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


# ── Apply (happy path) ──────────────────────────────────────────────────────


class TestApplyHappyPath:
    def test_archives_file_content_and_adds_exclusion(self, shared_dir, workspace):
        # Workspace orphan with content.
        (workspace / "loose.md").write_text("orphan content\n")

        applier = get_applier("RetireOrphan")
        action = RetireOrphan(bot_id="admin_bot", path="loose.md")
        result = applier.apply(action, "admin_bot")

        assert result.ok, result.message
        # The exclusion landed.
        assert _read_exclusions_file(shared_dir, "admin_bot") == ["loose.md"]
        # The archive file exists with original content.
        archive_dir = shared_dir / "app_posture" / "admin_bot" / "orphan_archive"
        assert archive_dir.exists()
        archived = list(archive_dir.glob("*loose*"))
        assert len(archived) == 1
        assert archived[0].read_text() == "orphan content\n"
        # And the workspace file is left in place (we don't delete).
        assert (workspace / "loose.md").exists()

    def test_archive_filename_is_filesystem_safe(self, shared_dir, workspace):
        """Workspace paths can contain slashes and unusual chars; the
        archive filename must be safe for any POSIX filesystem."""
        nested = workspace / "ops" / "tools"
        nested.mkdir(parents=True)
        (nested / "x.md").write_text("nested\n")

        applier = get_applier("RetireOrphan")
        result = applier.apply(
            RetireOrphan(bot_id="admin_bot", path="ops/tools/x.md"),
            "admin_bot",
        )
        assert result.ok
        archived = list(
            (shared_dir / "app_posture" / "admin_bot" / "orphan_archive").glob("*")
        )
        assert len(archived) == 1
        # Slashes converted to a portable separator; no shell-special
        # characters left in the basename.
        assert "/" not in archived[0].name
        assert archived[0].read_text() == "nested\n"

    def test_appends_when_other_exclusions_exist(self, shared_dir, workspace):
        """Existing exclusions from prior cycles are preserved on apply."""
        # Pre-populate exclusions.
        excl = shared_dir / "app_posture" / "admin_bot"
        excl.mkdir(parents=True)
        (excl / "orphan_exclusions.json").write_text(
            json.dumps(["earlier.md", "older.txt"])
        )
        (workspace / "newone.md").write_text("x\n")

        applier = get_applier("RetireOrphan")
        result = applier.apply(
            RetireOrphan(bot_id="admin_bot", path="newone.md"), "admin_bot",
        )
        assert result.ok
        assert sorted(_read_exclusions_file(shared_dir, "admin_bot")) == sorted(
            ["earlier.md", "newone.md", "older.txt"]
        )


# ── Idempotency ─────────────────────────────────────────────────────────────


class TestIdempotent:
    def test_double_apply_is_no_op(self, shared_dir, workspace):
        """If a path is already in exclusions (e.g. prior week's apply),
        the second apply should succeed without duplicate entries and
        flag itself as a no-op so operators don't think something
        changed when nothing did."""
        (workspace / "x.md").write_text("hi\n")
        applier = get_applier("RetireOrphan")
        action = RetireOrphan(bot_id="admin_bot", path="x.md")

        first = applier.apply(action, "admin_bot")
        assert first.ok
        assert _read_exclusions_file(shared_dir, "admin_bot") == ["x.md"]

        second = applier.apply(action, "admin_bot")
        assert second.ok
        # Already-retired flag set so the operator can see it was a no-op.
        assert second.details.get("already_retired") is True
        # No duplicate entry.
        assert _read_exclusions_file(shared_dir, "admin_bot") == ["x.md"]


# ── Failure-soft: archive miss, file gone ───────────────────────────────────


class TestArchiveMisses:
    def test_missing_workspace_file_still_excludes(self, shared_dir, workspace):
        """If the workspace file is already gone (operator manually
        deleted it between proposal-creation and apply), the apply
        should still record the exclusion — that's the load-bearing
        outcome the operator approved. Archive is best-effort."""
        applier = get_applier("RetireOrphan")
        result = applier.apply(
            RetireOrphan(bot_id="admin_bot", path="never-existed.md"),
            "admin_bot",
        )
        assert result.ok
        assert _read_exclusions_file(shared_dir, "admin_bot") == ["never-existed.md"]
        # Message acknowledges the archive miss.
        assert "archive skipped" in result.message
        assert result.details["archive_ok"] is False

    def test_target_is_directory_not_file(self, shared_dir, workspace):
        """Pointing the action at a directory shouldn't crash; archive
        is skipped (we can't copy a directory atomically with shutil.copy2)
        but the exclusion still applies — same posture as missing file."""
        (workspace / "actually-a-dir").mkdir()
        applier = get_applier("RetireOrphan")
        result = applier.apply(
            RetireOrphan(bot_id="admin_bot", path="actually-a-dir"),
            "admin_bot",
        )
        assert result.ok
        assert "actually-a-dir" in _read_exclusions_file(shared_dir, "admin_bot")


# ── Path-traversal refusal ──────────────────────────────────────────────────


class TestPathTraversalRefusal:
    def test_refuses_path_outside_workspace(self, shared_dir, workspace):
        """The LLM might emit a path with ../../ trying to escape the
        workspace. The applier resolves and verifies containment before
        doing anything, refusing rather than archiving outside files."""
        applier = get_applier("RetireOrphan")
        result = applier.apply(
            RetireOrphan(bot_id="admin_bot", path="../../etc/passwd"),
            "admin_bot",
        )
        assert not result.ok
        assert "outside" in result.message
        # No exclusion recorded on refusal.
        assert _read_exclusions_file(shared_dir, "admin_bot") == []


# ── Snapshot + revert ───────────────────────────────────────────────────────


class TestSnapshotAndRevert:
    def test_snapshot_captures_prior_exclusions(self, shared_dir, workspace):
        # Pre-existing exclusions.
        excl = shared_dir / "app_posture" / "admin_bot"
        excl.mkdir(parents=True)
        (excl / "orphan_exclusions.json").write_text(
            json.dumps(["prior.md"])
        )
        applier = get_applier("RetireOrphan")
        action = RetireOrphan(bot_id="admin_bot", path="newone.md")

        snap = applier.capture_snapshot(action, "admin_bot")
        assert snap["prior_exclusions"] == ["prior.md"]
        assert snap["bot_id"] == "admin_bot"

    def test_revert_restores_prior_exclusions(self, shared_dir, workspace):
        (workspace / "x.md").write_text("y\n")
        applier = get_applier("RetireOrphan")
        action = RetireOrphan(bot_id="admin_bot", path="x.md")

        snap = applier.capture_snapshot(action, "admin_bot")
        applier.apply(action, "admin_bot")
        # Apply added the entry.
        assert _read_exclusions_file(shared_dir, "admin_bot") == ["x.md"]

        revert = applier.revert(snap, "admin_bot")
        assert revert.ok
        # Back to (empty) prior state.
        assert _read_exclusions_file(shared_dir, "admin_bot") == []

    def test_revert_leaves_archive_in_place(self, shared_dir, workspace):
        """Revert undoes the exclusion but doesn't delete the archive
        copy — the snapshot is cheap to leave around and useful for
        audit. (Operators wanting to remove the archive can do so
        manually.)"""
        (workspace / "x.md").write_text("y\n")
        applier = get_applier("RetireOrphan")
        action = RetireOrphan(bot_id="admin_bot", path="x.md")

        snap = applier.capture_snapshot(action, "admin_bot")
        applier.apply(action, "admin_bot")
        archive = list(
            (shared_dir / "app_posture" / "admin_bot" / "orphan_archive").glob("*")
        )
        assert len(archive) == 1

        applier.revert(snap, "admin_bot")
        # Archive still present.
        archive_after = list(
            (shared_dir / "app_posture" / "admin_bot" / "orphan_archive").glob("*")
        )
        assert archive_after == archive


# ── Public load_exclusions API (posture review uses this) ───────────────────


class TestLoadExclusionsAPI:
    def test_returns_set_of_paths(self, shared_dir):
        excl = shared_dir / "app_posture" / "admin_bot"
        excl.mkdir(parents=True)
        (excl / "orphan_exclusions.json").write_text(
            json.dumps(["a.md", "b.txt"])
        )
        result = load_exclusions("admin_bot", shared_dir=shared_dir)
        assert result == {"a.md", "b.txt"}

    def test_missing_file_returns_empty_set(self, shared_dir):
        assert load_exclusions("admin_bot", shared_dir=shared_dir) == set()

    def test_malformed_json_returns_empty_set(self, shared_dir):
        excl = shared_dir / "app_posture" / "admin_bot"
        excl.mkdir(parents=True)
        (excl / "orphan_exclusions.json").write_text("not-json")
        assert load_exclusions("admin_bot", shared_dir=shared_dir) == set()

    def test_dict_envelope_is_read_supported(self, shared_dir):
        """Forward-compat: read tolerates a dict envelope shape
        (e.g. ``{"paths": [...]}``) so future PRs can add metadata
        without breaking older readers."""
        excl = shared_dir / "app_posture" / "admin_bot"
        excl.mkdir(parents=True)
        (excl / "orphan_exclusions.json").write_text(
            json.dumps({"paths": ["x.md"], "retired_at": "2026-05-09"})
        )
        assert load_exclusions("admin_bot", shared_dir=shared_dir) == {"x.md"}
