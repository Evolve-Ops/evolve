"""
Tests for POST /api/bots/<bot_id>/reflect/reconcile — UI-facing wrapper
around the manifest_hygiene auto-reconcile.

The reconcile logic itself is covered in test_manifest_hygiene.py. These
tests pin the endpoint glue: response shape, dry-run default, apply
toggle, unknown-bot handling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture
def app_with_bot(tmp_path, monkeypatch):
    """create_app wired up with a tmp_path bot workspace and a stamped
    orphan ready to reconcile."""
    from evolve_admin.web.server import create_app
    from evolve_admin.applications.provenance import embed_marker

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_dir = tmp_path / "bot-homes" / "team_bot_a"
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "evolve").mkdir(parents=True)

    # One Instance + one stamped orphan whose primary spec_id matches.
    inst = {
        "instance_id": "i-research",
        "bot_id": "team_bot_a",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": "p-0501449a",
            "spec_version": "2026.06.01-1.0",
            "installed_at": "2026-06-01T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": [],
        "status": "active",
    }
    (workspace / "manifests" / "i-research.json").write_text(json.dumps(inst))

    # Stamp the orphan under an OWNABLE dir. A marker under evolve/ (platform
    # telemetry) is now a scrub_candidate, not an attachable orphan, so it would
    # never reach the reconcile path.
    orphan = workspace / "scripts" / "helper.py"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("def x(): pass\n")
    embed_marker(
        orphan,
        pkg_ids=["p-0501449a"],
        file_id="f-12345678",
        pkg_versions={"p-0501449a": "2026.06.01-1.0"},
        file_version="2026.06.01-1.0",
        keyword="spec",
        merge=False,
    )

    network = {
        "networkId": "pod-test",
        "sharedDir": str(shared_dir),
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "members": ["team_bot_a"],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # manifest_hygiene, reflect AND the recon ledger (which reflect now reads)
    # each resolve bot_home — patch each.
    from evolve_admin.applications import manifest_hygiene as mh
    from evolve_admin.applications import reflect as rf
    from evolve_admin.applications import recon_ledger as rl
    monkeypatch.setattr(mh, "bot_home", lambda _bid: bot_dir)
    monkeypatch.setattr(rf, "bot_home", lambda _bid: bot_dir)
    monkeypatch.setattr(rl, "bot_home", lambda _bid: bot_dir)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "workspace": workspace,
        "manifests": workspace / "manifests",
    }


class TestReconcileEndpoint:
    def test_dry_run_is_default(self, app_with_bot):
        """POST with no body → dry-run (applied=False, no disk mutation)."""
        client, env = app_with_bot
        r = client.post("/api/bots/team_bot_a/reflect/reconcile")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["applied"] is False
        assert len(data["resolved"]) == 1
        assert data["resolved"][0]["spec_id"] == "p-0501449a"
        assert data["resolved"][0]["instance_id"] == "i-research"

        # Disk unchanged
        inst = json.loads((env["manifests"] / "i-research.json").read_text())
        assert inst["realized_files"] == []

    def test_apply_true_persists(self, app_with_bot):
        client, env = app_with_bot
        r = client.post(
            "/api/bots/team_bot_a/reflect/reconcile",
            json={"apply": True},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["applied"] is True
        assert len(data["resolved"]) == 1

        inst = json.loads((env["manifests"] / "i-research.json").read_text())
        assert len(inst["realized_files"]) == 1
        assert inst["realized_files"][0]["path"].endswith("helper.py")

    def test_unknown_bot_404(self, app_with_bot):
        client, _ = app_with_bot
        r = client.post("/api/bots/ghost/reflect/reconcile")
        assert r.status_code == 404
        assert "unknown" in r.get_json()["error"].lower()

    def test_counts_by_spec_shape(self, app_with_bot):
        client, _ = app_with_bot
        r = client.post("/api/bots/team_bot_a/reflect/reconcile")
        data = r.get_json()
        assert "counts_by_spec" in data
        assert data["counts_by_spec"]["p-0501449a"]["resolved"] == 1

    def test_targeted_paths_attaches_only_requested(self, app_with_bot):
        """A targeted ``paths`` request attaches just that file (per-row Attach)."""
        client, env = app_with_bot
        target = str(env["workspace"] / "scripts" / "helper.py")
        r = client.post(
            "/api/bots/team_bot_a/reflect/reconcile",
            json={"apply": True, "paths": [target]},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert len(data["resolved"]) == 1
        assert data["resolved"][0]["instance_id"] == "i-research"
        inst = json.loads((env["manifests"] / "i-research.json").read_text())
        assert len(inst["realized_files"]) == 1

    def test_paths_non_candidate_is_unmatched_not_no_instance(self, app_with_bot):
        """A requested path that is not an attach candidate → not_attach_candidate,
        never the legacy no_instance_for_spec_id."""
        client, env = app_with_bot
        bogus = str(env["workspace"] / "scripts" / "ghost.py")
        r = client.post(
            "/api/bots/team_bot_a/reflect/reconcile",
            json={"apply": True, "paths": [bogus]},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["resolved"] == []
        assert len(data["unmatched"]) == 1
        assert data["unmatched"][0]["reason"] == "not_attach_candidate"

    def test_malformed_paths_rejected(self, app_with_bot):
        """paths must be a list of strings — a malformed value is a 400, never a
        silent degrade to attach-all."""
        client, _ = app_with_bot
        r = client.post(
            "/api/bots/team_bot_a/reflect/reconcile",
            json={"apply": True, "paths": "scripts/helper.py"},
        )
        assert r.status_code == 400
        assert "paths" in r.get_json()["error"].lower()
