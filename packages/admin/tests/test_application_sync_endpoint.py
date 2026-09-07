"""
Tests for the smart-sync orchestrator — POST /api/applications/sync (+ /pod).

Covers the three behaviors the orchestrator promises:
  1. CHEAP path — every inventory code file is covered by a manifest, so the
     full LLM scan is NEVER invoked (scan_workspace_pipeline patched + asserted
     not called); the unified result reports path="cheap".
  2. ESCALATED path — an uncovered code file exists, so the full scan IS
     invoked, then reflect is re-run on the freshly-written manifests.
  3. UNIFIED shape — the stable result keys the frontend chip depends on.

The decision logic lives in applications/sync.py; these tests pin it through
the HTTP surface the way test_reflect_pod_endpoint pins the reflect rollup.
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
def sync_env(tmp_path, monkeypatch):
    """create_app with one synthetic bot workspace under tmp_path.

    bot_home is patched in BOTH the sync and reflect modules so the bot's
    workspace + manifests dir resolve under tmp_path. _collect_crons is
    stubbed so collect_inventory doesn't shell out to ``sudo crontab``.
    """
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_home_dir = tmp_path / "bot-homes" / "team_bot_a"
    workspace = bot_home_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)

    network = {
        "networkId": "pod-sync-1",
        "sharedDir": str(shared_dir),
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "members": ["team_bot_a"],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.applications import reflect as rf
    from evolve_admin.applications import sync as sync_mod
    from evolve_admin.applications import scanner as scanner_mod
    from evolve_admin.applications import recon_ledger as rl

    _map = lambda bid: bot_home_dir if bid == "team_bot_a" else tmp_path / "ghost" / bid
    monkeypatch.setattr(rf, "bot_home", _map)
    monkeypatch.setattr(sync_mod, "bot_home", _map)
    # reflect classifies via the recon ledger now — its bot_home drives the walk.
    monkeypatch.setattr(rl, "bot_home", _map)
    monkeypatch.setattr(scanner_mod, "_collect_crons", lambda bot_id, ws: [])

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "shared_dir": shared_dir,
        "sync_mod": sync_mod,
    }


def _write_v7_instance(manifests: Path, instance_id: str, spec_id: str,
                       realized: list[dict]) -> None:
    inst = {
        "instance_id": instance_id,
        "bot_id": "team_bot_a",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": "2026.05.20-1.0",
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": realized,
        "status": "active",
    }
    (manifests / f"{instance_id}.json").write_text(json.dumps(inst))


class TestApplicationSyncEndpoint:
    def test_cheap_path_does_not_invoke_scan(self, sync_env, monkeypatch):
        """All code files covered by a manifest → no LLM scan, path=cheap."""
        client, env = sync_env

        code = env["workspace"] / "scripts" / "a.py"
        code.parent.mkdir()
        code.write_text("x = 1\n")
        _write_v7_instance(env["manifests"], "i-1", "p-a", realized=[
            {"path": str(code), "file_id": "f-team_bot_a-a@2026.05.20-1.0"},
        ])

        calls = []
        monkeypatch.setattr(env["sync_mod"], "scan_workspace_pipeline",
                            lambda **kw: calls.append(kw) or [])

        r = client.post("/api/applications/sync?bot=team_bot_a")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["path"] == "cheap"
        assert data["discovered_count"] == 0
        assert data["uncovered_files"] == []
        # The whole point: the expensive scan was NOT run.
        assert calls == []

    def test_escalated_path_invokes_scan_then_reflects(self, sync_env, monkeypatch):
        """An uncovered code file → full scan runs, then reflect re-runs on
        the fresh manifests and the remaining-uncovered set is recomputed."""
        client, env = sync_env

        # An uncovered code file (no manifest claims it) + one empty instance
        # so the bot is a migrated v7-arc bot, not a no-manifests warning case.
        code = env["workspace"] / "scripts" / "b.py"
        code.parent.mkdir()
        code.write_text("y = 2\n")
        _write_v7_instance(env["manifests"], "i-existing", "p-x", realized=[])

        manifests = env["manifests"]

        def fake_scan(**kw):
            # Simulate discovery: write a fresh v7 manifest that now covers b.py.
            _write_v7_instance(manifests, "i-new", "p-newapp", realized=[
                {"path": str(code), "file_id": "f-team_bot_a-b@2026.05.20-1.0"},
            ])
            return [{"id": "p-newapp"}]

        calls = []
        monkeypatch.setattr(env["sync_mod"], "scan_workspace_pipeline",
                            lambda **kw: calls.append(kw) or fake_scan(**kw))

        r = client.post("/api/applications/sync?bot=team_bot_a")
        assert r.status_code == 200
        data = r.get_json()
        assert data["path"] == "escalated"
        # Scan invoked exactly once, with the bot's workspace.
        assert len(calls) == 1
        assert calls[0]["bot_id"] == "team_bot_a"
        # One new manifest file appeared this run.
        assert data["discovered_count"] == 1
        # b.py is now covered by the freshly-minted manifest → nothing remains.
        assert data["uncovered_files"] == []
        # Re-reflect ran over the fresh manifests (drift_findings is present).
        assert "drift_findings" in data

    def test_unified_result_shape(self, sync_env, monkeypatch):
        """The stable keys the frontend chip consumes are always present."""
        client, env = sync_env
        _write_v7_instance(env["manifests"], "i-1", "p-a", realized=[])
        monkeypatch.setattr(env["sync_mod"], "scan_workspace_pipeline",
                            lambda **kw: [])

        r = client.post("/api/applications/sync?bot=team_bot_a")
        assert r.status_code == 200
        data = r.get_json()
        for key in ("path", "reason", "discovered_count", "uncovered_files",
                    "drift_findings", "drift_counts"):
            assert key in data, f"missing {key}"
        assert data["path"] in ("cheap", "escalated")
        # drift_counts carries all four kinds for a stable shape.
        assert set(data["drift_counts"]) == {
            "orphan_file", "missing_marker", "stale_pkg_marker", "missing_disk_file",
        }

    def test_unknown_bot_404(self, sync_env):
        client, env = sync_env
        r = client.post("/api/applications/sync?bot=ghost_bot")
        assert r.status_code == 404

    def test_pod_rollup(self, sync_env, monkeypatch):
        """Pod endpoint returns a per-bot rollup with the smart-sync fields."""
        client, env = sync_env
        _write_v7_instance(env["manifests"], "i-1", "p-a", realized=[])
        monkeypatch.setattr(env["sync_mod"], "scan_workspace_pipeline",
                            lambda **kw: [])

        r = client.post("/api/applications/sync/pod")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "total_discovered" in data
        assert "escalated_bots" in data
        slot = next(b for b in data["bots"] if b["bot_id"] == "team_bot_a")
        assert slot["path"] in ("cheap", "escalated")
        assert "uncovered_files" in slot
