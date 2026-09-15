"""tests/test_manifest_update_applier.py — ManifestUpdate set_fields op.

Covers the generic ManifestUpdate applier: snapshot, apply, revert.
Capability-surface ops (add_tag, broaden_scope, …) are intentionally
not implemented and return ok=False; tested here so a future
contributor doesn't expect them to work yet.
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
from arbiter.appliers.manifest_update import set_shared_dir  # noqa: E402
from schema.proposal import ManifestUpdate  # noqa: E402


# ── Fixture helpers ─────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path, monkeypatch):
    """tmp shared_dir wired into the applier; restored after test."""
    set_shared_dir(tmp_path)
    yield tmp_path
    set_shared_dir(Path("/Users/Shared/evolve"))


def _write_manifest(shared_dir: Path, bot_id: str, app_id: str, **fields) -> Path:
    base = {
        "id": app_id,
        "name": app_id,
        "bot_id": bot_id,
        "status": "active",
    }
    base.update(fields)
    app_dir = shared_dir / "applications" / bot_id
    app_dir.mkdir(parents=True, exist_ok=True)
    path = app_dir / f"{app_id}.json"
    path.write_text(json.dumps(base))
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


# ── set_fields apply ───────────────────────────────────────────────────────


class TestSetFieldsApply:
    def test_writes_field_into_manifest(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "daily-bk")
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="daily-bk",
            operation="set_fields",
            fields={"test_exemption_reason": "manual cron"},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        data = _read(path)
        assert data["test_exemption_reason"] == "manual cron"
        # Other fields preserved
        assert data["id"] == "daily-bk"
        assert data["status"] == "active"

    def test_overwrites_existing_field(self, shared_dir):
        path = _write_manifest(
            shared_dir, "bot", "x", test_exemption_reason="stale reason",
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="x",
            operation="set_fields",
            fields={"test_exemption_reason": "new reason"},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        assert _read(path)["test_exemption_reason"] == "new reason"

    def test_set_multiple_fields(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "x")
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="x",
            operation="set_fields",
            fields={"foo": 1, "bar": [1, 2, 3], "baz": {"nested": True}},
        )
        assert applier.apply(action, "bot").ok
        data = _read(path)
        assert data["foo"] == 1
        assert data["bar"] == [1, 2, 3]
        assert data["baz"] == {"nested": True}

    def test_missing_manifest_returns_not_ok(self, shared_dir):
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="never-existed",
            operation="set_fields",
            fields={"x": 1},
        )
        result = applier.apply(action, "bot")
        assert not result.ok
        assert "not found" in result.message

    def test_empty_fields_is_no_op(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "x", original=True)
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(app_id="x", operation="set_fields", fields={})
        result = applier.apply(action, "bot")
        assert result.ok
        # Manifest untouched
        assert _read(path)["original"] is True


# ── Capability-surface ops — explicitly not implemented ────────────────────


class TestNotImplementedOps:
    @pytest.mark.parametrize(
        "op",
        [
            "add_tag",
            "remove_tag",
            "broaden_scope",
            "narrow_scope",
            "add_capability",
            "remove_capability",
            "update_keywords",
        ],
    )
    def test_capability_surface_ops_return_not_ok(self, shared_dir, op):
        _write_manifest(shared_dir, "bot", "x")
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(app_id="x", operation=op, fields={})
        result = applier.apply(action, "bot")
        assert not result.ok
        # Either the message or the details should mark it unimplemented;
        # the details dict carries the precise error string.
        assert "not implemented" in result.details.get("error", "")


# ── Snapshot + revert ──────────────────────────────────────────────────────


class TestRevert:
    def test_revert_restores_prior_value(self, shared_dir):
        path = _write_manifest(
            shared_dir, "bot", "x", test_exemption_reason="original",
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="x",
            operation="set_fields",
            fields={"test_exemption_reason": "applier-set"},
        )
        snapshot = applier.capture_snapshot(action, "bot")
        applier.apply(action, "bot")
        assert _read(path)["test_exemption_reason"] == "applier-set"

        revert = applier.revert(snapshot, "bot")
        assert revert.ok
        assert _read(path)["test_exemption_reason"] == "original"

    def test_revert_removes_field_that_did_not_exist_before(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "x")  # no exemption set
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="x",
            operation="set_fields",
            fields={"test_exemption_reason": "applier-added"},
        )
        snapshot = applier.capture_snapshot(action, "bot")
        applier.apply(action, "bot")
        assert "test_exemption_reason" in _read(path)

        revert = applier.revert(snapshot, "bot")
        assert revert.ok
        assert "test_exemption_reason" not in _read(path)
        # Other fields preserved
        assert _read(path)["id"] == "x"

    def test_revert_handles_mixed_keys(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "x", existing="kept")
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="x",
            operation="set_fields",
            fields={"existing": "overwritten", "new_field": 42},
        )
        snapshot = applier.capture_snapshot(action, "bot")
        applier.apply(action, "bot")

        applier.revert(snapshot, "bot")
        data = _read(path)
        assert data["existing"] == "kept"
        assert "new_field" not in data

    def test_revert_when_manifest_missing_returns_not_ok(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "x", v=1)
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="x", operation="set_fields", fields={"v": 2},
        )
        snapshot = applier.capture_snapshot(action, "bot")
        applier.apply(action, "bot")
        # Manifest deleted out from under us
        path.unlink()
        revert = applier.revert(snapshot, "bot")
        assert not revert.ok


# ── add_files (PR8 — fold_orphan applier) ──────────────────────────────────


class TestAddFilesApply:
    """The fold_orphan applier path: append a file path (or many) to an
    existing app's manifest.files list, deduped against the manifest's
    CURRENT value at apply time. Used by app_posture_reflection's
    fold_orphan proposals."""

    def test_appends_to_empty_files_list(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "habits", files=[])
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ops/habits.py"]},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        assert _read(path)["files"] == ["ops/habits.py"]
        assert result.details["added"] == ["ops/habits.py"]

    def test_appends_to_existing_files_preserving_them(self, shared_dir):
        path = _write_manifest(
            shared_dir, "bot", "habits", files=["ops/habits.py"],
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ops/data.json"]},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        files = _read(path)["files"]
        # Original preserved in original position; new entry appended.
        assert files == ["ops/habits.py", "ops/data.json"]

    def test_dedups_against_string_entries(self, shared_dir):
        """If the file is already in files[] (as a plain string), it
        shouldn't be added twice — and the operation succeeds with a
        no-op message rather than failing."""
        path = _write_manifest(
            shared_dir, "bot", "habits", files=["ops/habits.py"],
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ops/habits.py"]},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        assert "no-op" in result.message
        assert _read(path)["files"] == ["ops/habits.py"]
        assert result.details["already_present"] == ["ops/habits.py"]
        assert result.details["added"] == []

    def test_dedups_against_v5_dict_entries(self, shared_dir):
        """v5+ manifests store files as list[dict] with provenance
        records. The applier must recognize the path inside a dict
        entry and skip duplicates accordingly — not append a string
        version that ends up shadowing the dict."""
        path = _write_manifest(
            shared_dir, "bot", "habits",
            files=[
                {"file_id": "f-1", "path": "ops/habits.py", "owned_by": "p-x"},
            ],
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ops/habits.py"]},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        files = _read(path)["files"]
        # One entry, still in dict form.
        assert files == [
            {"file_id": "f-1", "path": "ops/habits.py", "owned_by": "p-x"}
        ]

    def test_partial_dedup_with_some_new(self, shared_dir):
        path = _write_manifest(
            shared_dir, "bot", "habits", files=["ops/a.py"],
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ops/a.py", "ops/b.py", "ops/c.py"]},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        files = _read(path)["files"]
        assert files == ["ops/a.py", "ops/b.py", "ops/c.py"]
        # Only the genuinely-new ones reported.
        assert result.details["added"] == ["ops/b.py", "ops/c.py"]

    def test_missing_manifest_returns_not_ok(self, shared_dir):
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="never-existed",
            operation="add_files",
            fields={"files": ["x.py"]},
        )
        result = applier.apply(action, "bot")
        assert not result.ok
        assert "not found" in result.message

    def test_empty_files_dict_is_no_op(self, shared_dir):
        path = _write_manifest(
            shared_dir, "bot", "habits", files=["ops/habits.py"],
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits", operation="add_files", fields={},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        # No-op leaves the manifest untouched.
        assert _read(path)["files"] == ["ops/habits.py"]

    def test_rejects_non_list_files_payload(self, shared_dir):
        """add_files requires {'files': [str, ...]}; anything else fails
        loudly so a malformed proposal can't quietly mutate something."""
        _write_manifest(shared_dir, "bot", "habits", files=[])
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": "not-a-list"},
        )
        result = applier.apply(action, "bot")
        assert not result.ok
        assert "list" in result.message

    def test_rejects_non_string_file_entries(self, shared_dir):
        _write_manifest(shared_dir, "bot", "habits", files=[])
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ok.py", 42, "also-ok.py"]},
        )
        result = applier.apply(action, "bot")
        # 42 is not a string — refuse the whole batch rather than
        # quietly skipping it.
        assert not result.ok

    def test_strips_whitespace_and_drops_empty(self, shared_dir):
        path = _write_manifest(shared_dir, "bot", "habits", files=[])
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["  ops/habits.py  ", "", "   "]},
        )
        result = applier.apply(action, "bot")
        assert result.ok
        files = _read(path)["files"]
        assert files == ["ops/habits.py"]


class TestAddFilesRevert:
    def test_revert_restores_prior_files_list(self, shared_dir):
        path = _write_manifest(
            shared_dir, "bot", "habits", files=["ops/a.py"],
        )
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ops/b.py", "ops/c.py"]},
        )
        snapshot = applier.capture_snapshot(action, "bot")
        applier.apply(action, "bot")
        # Sanity: apply succeeded and added the two new files.
        assert _read(path)["files"] == ["ops/a.py", "ops/b.py", "ops/c.py"]

        revert = applier.revert(snapshot, "bot")
        assert revert.ok
        # Back to the pre-apply state.
        assert _read(path)["files"] == ["ops/a.py"]

    def test_revert_removes_files_field_when_absent_before(self, shared_dir):
        """If the manifest had no `files` key before apply (older v3
        manifest, say), revert should remove it rather than leave an
        empty array hanging around."""
        path = _write_manifest(shared_dir, "bot", "habits")  # no files key
        applier = get_applier("ManifestUpdate")
        action = ManifestUpdate(
            app_id="habits",
            operation="add_files",
            fields={"files": ["ops/x.py"]},
        )
        snapshot = applier.capture_snapshot(action, "bot")
        applier.apply(action, "bot")
        assert "files" in _read(path)

        revert = applier.revert(snapshot, "bot")
        assert revert.ok
        assert "files" not in _read(path)
