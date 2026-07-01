"""Manifest-last uninstall ordering.

Regression for the personal-bot / task-manager bug (2026-06-02): the
``delete_application`` endpoint deleted the manifest *first*, then ran
the file-unlink loop. The UI's two-pass DELETE pattern — first call
with ``delete_files: []`` to fetch the preview, second call with the
confirmed list to execute — meant the preview itself silently deleted
the manifest. The second call 404'd, the unlink loop never ran, and
the operator was left with 5 orphan files and "Error: manifest not
found" on screen.

These tests pin the post-fix invariant: the manifest is the natural
checklist for an uninstall, so it must survive until every other step
succeeds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


# ── Unit tests on the manifest module ─────────────────────────────────────────


def _seed_manifest(
    shared_dir: Path,
    workspace: Path,
    *,
    bot_id: str = "team_bot_a",
    app_id: str = "task-manager",
    pkg_id: str = "p-aaaa1111",
    file_layers: dict[str, str] | None = None,
) -> Path:
    """Drop a v5 manifest at <workspace>/manifests/<app_id>.json with
    the requested file list. Returns the manifest path.

    file_layers is path → layer; data/state files end up preserved, the
    rest become deletion candidates (no shared_with by default).
    """
    file_layers = file_layers or {
        "scripts/tasks.py":    "script",
        "scripts/task-check.sh": "script",
        "TASKS.md":            "reference",
        "tasks.json":          "data",
    }
    manifests_dir = workspace / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id":      app_id,
        "name":    "Task Manager",
        "bot_id":  bot_id,
        "pkg_id":  pkg_id,
        "status":  "active",
        "files": [
            {
                "file_id":  f"f-{i:04x}",
                "path":     path,
                "layer":    layer,
                "owned_by": pkg_id,
                "shared_with": [],
            }
            for i, (path, layer) in enumerate(file_layers.items())
        ],
    }
    p = manifests_dir / f"{app_id}.json"
    p.write_text(json.dumps(manifest))
    # Materialize the files on disk so the unlink loop has something to do.
    for rel in file_layers:
        f = workspace / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# stub for {rel}\n")
    return p


def test_plan_manifest_deletion_does_not_mutate(tmp_path, monkeypatch):
    from evolve_admin.applications import manifest as m_mod

    shared = tmp_path / "shared"; shared.mkdir()
    workspace = tmp_path / "ws"
    manifest_path = _seed_manifest(shared, workspace)

    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id, user=None: workspace,
    )

    plan = m_mod.plan_manifest_deletion(
        application_id="task-manager",
        bot_id="team_bot_a",
        shared_dir=shared,
        workspace_path=workspace,
    )
    assert plan["ok"] is True
    assert plan["pkg_id"] == "p-aaaa1111"
    # tasks.json is data-layer → preserved
    assert plan["preserved_files"] == ["tasks.json"]
    # the rest are sole-owner, non-data → deletion candidates
    assert set(plan["deletion_candidates"]) == {
        "scripts/tasks.py", "scripts/task-check.sh", "TASKS.md",
    }
    # Manifest still on disk after the plan call — this is the whole point.
    assert manifest_path.exists(), "plan() must not delete the manifest"
    # And every file is still on disk too.
    for rel in ["scripts/tasks.py", "scripts/task-check.sh", "TASKS.md", "tasks.json"]:
        assert (workspace / rel).exists()


def test_finalize_manifest_deletion_deletes_only_manifest(tmp_path, monkeypatch):
    from evolve_admin.applications import manifest as m_mod

    shared = tmp_path / "shared"; shared.mkdir()
    workspace = tmp_path / "ws"
    manifest_path = _seed_manifest(shared, workspace)

    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id, user=None: workspace,
    )

    res = m_mod.finalize_manifest_deletion(
        application_id="task-manager",
        bot_id="team_bot_a",
        shared_dir=shared,
    )
    assert res["ok"] is True
    assert not manifest_path.exists(), "finalize must remove the manifest JSON"
    # Source files are not finalize's responsibility — they should still
    # be on disk (the endpoint unlinks them between plan and finalize).
    for rel in ["scripts/tasks.py", "TASKS.md", "tasks.json"]:
        assert (workspace / rel).exists()
    # Archive copy was written.
    archive_dir = manifest_path.parent / "_history"
    assert archive_dir.exists()
    archived = list(archive_dir.glob("task-manager_deleted_*.json"))
    assert len(archived) == 1


def test_finalize_returns_error_when_manifest_already_gone(tmp_path, monkeypatch):
    from evolve_admin.applications import manifest as m_mod

    shared = tmp_path / "shared"; shared.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "manifests").mkdir()
    # No manifest written.

    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id, user=None: workspace,
    )

    res = m_mod.finalize_manifest_deletion(
        application_id="task-manager",
        bot_id="team_bot_a",
        shared_dir=shared,
    )
    assert res["ok"] is False
    assert "not found" in res["error"]


# ── Endpoint integration ──────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    bot_id = "team_bot_a"
    workspace = tmp_path / "ws"
    shared = tmp_path / "shared"; shared.mkdir()

    network = {
        "bots": {bot_id: {"user": bot_id}},
        "sharedDir": str(shared),
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))

    # get_bot_workspace is imported inside the endpoint via
    # `from ..config import get_bot_workspace`, so patching the
    # canonical module attribute is enough.
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id, user=None: workspace,
    )

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, workspace, shared, bot_id


def test_preview_call_does_not_mutate(client):
    c, workspace, shared, bot_id = client
    manifest_path = _seed_manifest(shared, workspace)

    resp = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={"delete_files": []},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data["deletion_candidates"]) == {
        "scripts/tasks.py", "scripts/task-check.sh", "TASKS.md",
    }
    # The whole point: preview is non-mutating.
    assert manifest_path.exists()
    for rel in ["scripts/tasks.py", "scripts/task-check.sh", "TASKS.md", "tasks.json"]:
        assert (workspace / rel).exists()


def test_execute_deletes_files_then_manifest(client):
    c, workspace, shared, bot_id = client
    manifest_path = _seed_manifest(shared, workspace)

    resp = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={
            "delete_files": [
                "scripts/tasks.py", "scripts/task-check.sh", "TASKS.md",
            ],
            "commit": True,
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert set(data["actually_deleted"]) == {
        "scripts/tasks.py", "scripts/task-check.sh", "TASKS.md",
    }
    assert data["failed_deletes"] == []
    # Confirmed files are gone…
    for rel in ["scripts/tasks.py", "scripts/task-check.sh", "TASKS.md"]:
        assert not (workspace / rel).exists()
    # …data-layer file is preserved on disk (and not listed for delete)…
    assert (workspace / "tasks.json").exists()
    # …manifest is gone (final commit step succeeded).
    assert not manifest_path.exists()


def test_unlink_failure_leaves_manifest_on_disk(client, monkeypatch):
    """A mid-flight failure must not orphan files behind a deleted manifest.

    Inject an unlink that fails for one file. The endpoint must skip
    the finalize step so the operator can resume cleanup later — the
    manifest is the checklist.
    """
    c, workspace, shared, bot_id = client
    manifest_path = _seed_manifest(shared, workspace)

    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **kw):
        if self.name == "task-check.sh":
            raise PermissionError("simulated unlink failure")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    resp = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={
            "delete_files": [
                "scripts/tasks.py", "scripts/task-check.sh", "TASKS.md",
            ],
            "commit": True,
        },
    )
    assert resp.status_code == 500
    data = resp.get_json()
    assert "scripts/task-check.sh" in data["failed_deletes"]
    # Manifest must still be on disk — that's the resumable checklist.
    assert manifest_path.exists(), (
        "manifest must survive a partial-failure run so the operator "
        "can re-trigger uninstall and finish cleaning up"
    )


def test_unlink_failure_leaves_manifest_unwired(client, monkeypatch):
    """§8.4 step 3: triggers are unregistered BEFORE files leave disk.

    The failed-delete resume window leaves the manifest in the bot's
    manifests/ dir indefinitely — the same dir the plugin compiles
    event_triggers[] from. Without the unwire-first step, the plugin
    keeps intercepting matched messages and invoking the scripts this
    very run just deleted.
    """
    c, workspace, shared, bot_id = client
    manifest_path = _seed_manifest(shared, workspace)
    data = json.loads(manifest_path.read_text())
    data["invocation_mode"] = "plugin_intercept"
    data["event_triggers"] = [{
        "id": "t1",
        "match": {"channel": "any", "pattern": "^!task\\b"},
        "invocation": {
            "script": "scripts/tasks.py",
            "stdout_protocol": "atlas_research",
            "on_failure": "silent",
        },
    }]
    manifest_path.write_text(json.dumps(data))

    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **kw):
        if self.name == "task-check.sh":
            raise PermissionError("simulated unlink failure")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    resp = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={
            "delete_files": [
                "scripts/tasks.py", "scripts/task-check.sh", "TASKS.md",
            ],
            "commit": True,
        },
    )
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["triggers_unwired"]["unwired"] is True
    # The surviving checklist manifest must carry no live registrations.
    left = json.loads(manifest_path.read_text())
    assert left["event_triggers"] == []
    assert left["invocation_mode"] == "agent_invokes"


def test_execute_response_reports_triggers_unwired(client):
    c, workspace, shared, bot_id = client
    _seed_manifest(shared, workspace)

    resp = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={"delete_files": ["scripts/tasks.py"], "commit": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    # No triggers on the seed manifest → clean no-op, still reported.
    assert body["triggers_unwired"] == {
        "ok": True, "unwired": False, "trigger_count": 0,
    }


def test_empty_files_with_commit_still_finalizes(client):
    """An app whose only artifact is the manifest itself: zero files to
    delete, but the operator still wants the slot freed. commit=true
    must let the finalize step run.
    """
    c, workspace, shared, bot_id = client
    manifest_path = _seed_manifest(shared, workspace, file_layers={})

    resp = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={"delete_files": [], "commit": True},
    )
    assert resp.status_code == 200
    assert not manifest_path.exists()


def test_double_preview_call_is_idempotent(client):
    """Two preview calls in a row both succeed without mutating —
    direct regression for the bug: the first call used to delete the
    manifest, so a second call 404'd.
    """
    c, workspace, shared, bot_id = client
    manifest_path = _seed_manifest(shared, workspace)

    r1 = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={"delete_files": []},
    )
    r2 = c.delete(
        f"/api/applications/{bot_id}/task-manager",
        json={"delete_files": []},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.get_json()["deletion_candidates"] == r2.get_json()["deletion_candidates"]
    assert manifest_path.exists()
