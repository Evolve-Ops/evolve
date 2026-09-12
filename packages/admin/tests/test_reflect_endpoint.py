"""
Tests for GET /api/bots/<bot_id>/reflect — UI-facing wrapper around the
Reflect manifest-hygiene scan (S3b).

The underlying scan logic is covered exhaustively in test_reflect.py. These
tests pin the endpoint glue: response shape, error codes, counts aggregation,
and unknown-bot handling.
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
    """Spin up create_app with a synthetic bot workspace under tmp_path.

    Patches bot_home in the reflect module so the route resolves to our
    test workspace instead of /Users/<bot>.
    """
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_dir = tmp_path / "bot-homes" / "team_bot_a"
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "admin_bot": {"user": "admin_bot"}},
        "members": ["team_bot_a", "admin_bot"],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Redirect bot_home: reflect imports it from ..config at module load.
    # reflect is now a thin reader of the recon ledger — the workspace walk it
    # delegates to resolves bot_home in recon_ledger, so patch that too.
    from evolve_admin.applications import reflect as rf
    from evolve_admin.applications import recon_ledger as rl
    monkeypatch.setattr(rf, "bot_home", lambda _bid: bot_dir)
    monkeypatch.setattr(rl, "bot_home", lambda _bid: bot_dir)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "bot_dir": bot_dir,
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "shared_dir": shared_dir,
    }


def _write_v7_instance(env, instance_id: str, spec_id: str,
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
    (env["manifests"] / f"{instance_id}.json").write_text(json.dumps(inst))


class TestReflectEndpoint:
    def test_clean_workspace_returns_zero_findings(self, app_with_bot):
        client, env = app_with_bot
        _write_v7_instance(env, "i-1", "p-a", realized=[])

        r = client.get("/api/bots/team_bot_a/reflect")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["bot_id"] == "team_bot_a"
        assert data["instances_checked"] == 1
        assert data["findings"] == []
        assert data["counts"] == {}

    def test_unknown_bot_404(self, app_with_bot):
        client, _ = app_with_bot
        r = client.get("/api/bots/ghost/reflect")
        assert r.status_code == 404
        assert "unknown" in r.get_json()["error"].lower()

    def test_no_v7_instances_warns(self, app_with_bot):
        client, env = app_with_bot
        # No Instances written — manifests/ is empty
        r = client.get("/api/bots/team_bot_a/reflect")
        assert r.status_code == 200
        data = r.get_json()
        assert data["instances_checked"] == 0
        assert any("no v7-arc Instances" in w for w in data["warnings"])

    def test_findings_serialize_to_dicts_with_expected_keys(self, app_with_bot):
        """A real finding should round-trip through asdict and include the
        fields the UI renders: kind, file_path, spec_id, description,
        proposed_action."""
        from evolve_admin.applications.provenance import embed_marker

        client, env = app_with_bot
        # Create an Instance that claims one file but the file has no marker
        # → missing_marker finding.
        (env["workspace"] / "scripts").mkdir()
        f = env["workspace"] / "scripts" / "run.py"
        f.write_text("print('hi')\n")
        _write_v7_instance(env, "i-1", "p-a", realized=[
            {"path": str(f), "file_id": "f-abc12345@2026.05.20-1.0"}
        ])

        r = client.get("/api/bots/team_bot_a/reflect")
        assert r.status_code == 200
        data = r.get_json()

        # One missing_marker finding expected
        assert data["counts"].get("missing_marker", 0) == 1
        finding = next(f for f in data["findings"] if f["kind"] == "missing_marker")
        assert "file_path" in finding
        assert finding["instance_id"] == "i-1"
        assert finding["spec_id"] == "p-a"
        assert finding["description"]
        assert finding["proposed_action"]["kind"] == "stamp_marker"

    def test_counts_aggregate_by_kind(self, app_with_bot):
        """Multiple findings of the same kind aggregate; mixed kinds break out."""
        from evolve_admin.applications.provenance import embed_marker

        client, env = app_with_bot
        (env["workspace"] / "scripts").mkdir()

        # Two missing_marker findings (Instance claims, no marker)
        f1 = env["workspace"] / "scripts" / "a.py"
        f1.write_text("x = 1\n")
        f2 = env["workspace"] / "scripts" / "b.py"
        f2.write_text("y = 2\n")
        _write_v7_instance(env, "i-1", "p-a", realized=[
            {"path": str(f1), "file_id": "f-aaa@2026.05.20-1.0"},
            {"path": str(f2), "file_id": "f-bbb@2026.05.20-1.0"},
        ])
        # One orphan_file (attach_candidate) finding: marker resolves to the
        # live p-a Instance, but no Instance claims this path → the genuine
        # "forgot to register" case. (The marker must resolve to a live app;
        # an unresolvable spec would classify as scrub, not attach.)
        orphan = env["workspace"] / "scripts" / "orphan.py"
        orphan.write_text("z = 3\n")
        embed_marker(orphan, pkg_ids=["p-a"],
                     file_id="f-zzz@2026.05.20-1.0",
                     keyword="spec")

        r = client.get("/api/bots/team_bot_a/reflect")
        assert r.status_code == 200
        data = r.get_json()
        assert data["counts"]["missing_marker"] == 2
        assert data["counts"]["orphan_file"] == 1
