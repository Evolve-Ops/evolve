"""
Tests for GET /api/reflect/pod — pod-wide Reflect rollup.

Walks every bot in network.json, runs reflect, aggregates counts.
The per-bot scan logic itself is covered by test_reflect.py; this file
pins the aggregation glue: pod-level totals, per-bot subsections, and
graceful handling of a scan-time exception on one bot not breaking the
rest.
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
def pod_env(tmp_path, monkeypatch):
    """create_app with two synthetic bot workspaces under tmp_path.

    bot_home in the reflect module is patched to map bot_id → tmp_path
    so each bot's manifests dir is distinct. The fixture also seeds
    network.json with both bots so the pod endpoint iterates both.
    """
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_dirs = {
        "team_bot_a":   tmp_path / "bot-homes" / "team_bot_a",
        "admin_bot": tmp_path / "bot-homes" / "admin_bot",
    }
    for bot_id, d in bot_dirs.items():
        (d / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {bot_id: {"user": bot_id} for bot_id in bot_dirs},
        "members": list(bot_dirs.keys()),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.applications import reflect as rf
    from evolve_admin.applications import recon_ledger as rl
    _map = lambda bid: bot_dirs.get(bid, tmp_path / "ghost" / bid)
    monkeypatch.setattr(rf, "bot_home", _map)
    # reflect delegates the workspace walk to the recon ledger; patch its
    # bot_home too so the per-bot classification reads the tmp workspaces.
    monkeypatch.setattr(rl, "bot_home", _map)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "bot_dirs": bot_dirs,
        "shared_dir": shared_dir,
    }


def _write_v7_instance(env, bot_id: str, instance_id: str,
                       spec_id: str, realized: list[dict]) -> None:
    inst = {
        "instance_id": instance_id,
        "bot_id": bot_id,
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
    manifests = env["bot_dirs"][bot_id] / ".openclaw" / "workspace" / "manifests"
    (manifests / f"{instance_id}.json").write_text(json.dumps(inst))


class TestReflectPodEndpoint:
    def test_clean_pod_returns_zero_total_findings(self, pod_env):
        client, env = pod_env
        _write_v7_instance(env, "team_bot_a", "i-1", "p-a", realized=[])
        _write_v7_instance(env, "admin_bot", "i-2", "p-b", realized=[])

        r = client.get("/api/reflect/pod")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["total_findings"] == 0
        assert data["total_instances_checked"] == 2
        assert data["aggregate_counts"] == {}
        # Each bot has its own slot
        ids = sorted(b["bot_id"] for b in data["bots"])
        assert ids == ["admin_bot", "team_bot_a"]

    def test_findings_aggregate_across_bots(self, pod_env):
        """Two bots with mixed findings — aggregate counts roll up by kind."""
        from evolve_admin.applications.provenance import embed_marker

        client, env = pod_env

        # team_bot_a: 1 missing_marker
        team_bot_a_ws = env["bot_dirs"]["team_bot_a"] / ".openclaw" / "workspace"
        (team_bot_a_ws / "scripts").mkdir()
        kf = team_bot_a_ws / "scripts" / "a.py"
        kf.write_text("x = 1\n")
        _write_v7_instance(env, "team_bot_a", "i-1", "p-a", realized=[
            {"path": str(kf), "file_id": "f-team_bot_a-a@2026.05.20-1.0"},
        ])

        # admin_bot: 1 missing_marker + 1 orphan_file
        admin_bot_ws = env["bot_dirs"]["admin_bot"] / ".openclaw" / "workspace"
        (admin_bot_ws / "scripts").mkdir()
        sf = admin_bot_ws / "scripts" / "b.py"
        sf.write_text("y = 2\n")
        _write_v7_instance(env, "admin_bot", "i-2", "p-b", realized=[
            {"path": str(sf), "file_id": "f-admin_bot-a@2026.05.20-1.0"},
        ])
        # Orphan (attach_candidate): marker resolves to the live p-b Instance
        # but no Instance claims this path. The marker must resolve to a live
        # app — an unresolvable spec would classify as scrub, not attach.
        orphan = admin_bot_ws / "scripts" / "orphan.py"
        orphan.write_text("z = 3\n")
        embed_marker(orphan, pkg_ids=["p-b"],
                     file_id="f-zzz@2026.05.20-1.0",
                     keyword="spec")

        r = client.get("/api/reflect/pod")
        assert r.status_code == 200
        data = r.get_json()
        assert data["total_findings"] == 3
        assert data["aggregate_counts"]["missing_marker"] == 2
        assert data["aggregate_counts"]["orphan_file"] == 1

        # Each bot's per-bot counts only reflect its own findings
        team_bot_a_bot = next(b for b in data["bots"] if b["bot_id"] == "team_bot_a")
        admin_bot_bot = next(b for b in data["bots"] if b["bot_id"] == "admin_bot")
        assert team_bot_a_bot["counts"] == {"missing_marker": 1}
        assert admin_bot_bot["counts"] == {"missing_marker": 1, "orphan_file": 1}

    def test_per_bot_exception_does_not_break_pod(self, pod_env, monkeypatch):
        """If reflect raises on one bot, that bot's slot gets an error field
        but the other bots still return clean results."""
        client, env = pod_env
        _write_v7_instance(env, "team_bot_a", "i-1", "p-a", realized=[])
        _write_v7_instance(env, "admin_bot", "i-2", "p-b", realized=[])

        # Force reflect() to raise for "admin_bot" only
        from evolve_admin.applications import reflect as rf
        real_reflect = rf.reflect

        def fake_reflect(bot_id, shared_dir):
            if bot_id == "admin_bot":
                raise RuntimeError("boom")
            return real_reflect(bot_id, shared_dir)
        monkeypatch.setattr(rf, "reflect", fake_reflect)

        r = client.get("/api/reflect/pod")
        assert r.status_code == 200
        data = r.get_json()

        team_bot_a_slot = next(b for b in data["bots"] if b["bot_id"] == "team_bot_a")
        admin_bot_slot = next(b for b in data["bots"] if b["bot_id"] == "admin_bot")
        assert "error" in admin_bot_slot
        assert "boom" in admin_bot_slot["error"]
        # team_bot_a still scanned cleanly
        assert team_bot_a_slot["findings"] == []
        # Totals reflect only the successful bot
        assert data["total_instances_checked"] == 1

    def test_empty_network_returns_no_bots(self, tmp_path, monkeypatch):
        """A network with no bots returns an empty bots list."""
        from evolve_admin.web.server import create_app

        shared_dir = tmp_path / "evolve"
        shared_dir.mkdir()
        network_path = tmp_path / "network.json"
        network_path.write_text(json.dumps({
            "networkId": "pod-empty",
            "sharedDir": str(shared_dir),
            "bots": {},
            "members": [],
        }))

        app = create_app(network_path)
        app.config["TESTING"] = True
        client = app.test_client()

        r = client.get("/api/reflect/pod")
        assert r.status_code == 200
        data = r.get_json()
        assert data["bots"] == []
        assert data["total_findings"] == 0
        assert data["aggregate_counts"] == {}
