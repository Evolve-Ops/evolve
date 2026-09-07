"""
Tests for /api/applications/<bot>/<app>/adopt-preview and /adopt.

Exercises the endpoint glue: response shape, error codes, structural-diff
refusal, no-op short-circuit, and end-to-end Instance rewrite when a
presentation-only change is adopted.
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
def app_env(tmp_path, monkeypatch):
    """create_app + a v7-arc Instance and two Spec versions in the gallery."""
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_dir = tmp_path / "bot-homes" / "team_bot_a"
    manifests = bot_dir / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "members": ["team_bot_a"],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Redirect manifest path resolution so reads + writes hit tmp_path
    # instead of /Users/<bot>. _bot_manifests_dir calls resolve_bot_paths
    # (defined in server.py), which uses pwd.getpwnam — fails on the dev
    # laptop where the "team_bot_a" user doesn't exist. Patching it before
    # create_app fires is the cleanest intercept.
    workspace_str = str(bot_dir / ".openclaw" / "workspace")

    from evolve_admin.web import server as srv
    monkeypatch.setattr(
        srv, "resolve_bot_paths",
        lambda bot_id, user=None: {
            "workspace": workspace_str,
            "user": user or bot_id,
        },
    )

    app = create_app(network_path)
    app.config["TESTING"] = True

    return app.test_client(), {
        "shared_dir": shared_dir,
        "manifests": manifests,
        "bot_dir": bot_dir,
    }


def _seed_v7_instance(env, spec_id="p-aaaa1111", version="2026.05.20-1.0"):
    """Drop a v7-arc Instance into the bot's manifests dir."""
    instance_id = "i-12345678"
    inst = {
        "instance_id": instance_id,
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": version,
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": [],
        "status": "active",
    }
    (env["manifests"] / f"{instance_id}.json").write_text(json.dumps(inst))
    return instance_id


def _seed_spec(env, spec_id, version, **overrides):
    """Write a Spec to gallery/local/<spec_id>/<version>.json."""
    spec = {
        "spec_id": spec_id,
        "spec_version": version,
        "name": "Journal",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "objective": {"primary": "Daily log", "sub_objectives": []},
        "blueprint": {"approach": "regex"},
        "dependencies": [],
        "audience_scoping": {"pod_operator": True},
    }
    spec.update(overrides)
    d = env["shared_dir"] / "gallery" / "local" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{version}.json").write_text(json.dumps(spec))
    return spec


# ── adopt-preview ────────────────────────────────────────────────────────────


class TestAdoptPreview:
    def test_presentation_only_diff_returns_safe(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "p-aaaa1111", "2026.05.20-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0",
                   description="new presentation description")

        r = client.get(f"/api/applications/team_bot_a/{iid}/adopt-preview")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["from_version"] == "2026.05.20-1.0"
        assert data["to_version"] == "2026.05.22-1.0"
        assert data["safe_to_adopt"] is True
        assert data["spec_diff"]["kind"] == "presentation_only"

    def test_structural_diff_returns_unsafe(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "p-aaaa1111", "2026.05.20-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0",
                   blueprint={"approach": "totally different"})

        r = client.get(f"/api/applications/team_bot_a/{iid}/adopt-preview")
        assert r.status_code == 200
        data = r.get_json()
        assert data["safe_to_adopt"] is False
        assert data["spec_diff"]["kind"] == "structural"
        assert "blueprint" in data["spec_diff"]["structural_fields_touched"]

    def test_explicit_target_version(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "p-aaaa1111", "2026.05.20-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0", description="v1.1")
        _seed_spec(env, "p-aaaa1111", "2026.05.25-1.0", description="v1.2")

        # Without target_version, picks latest (v1.2)
        r = client.get(f"/api/applications/team_bot_a/{iid}/adopt-preview")
        assert r.get_json()["to_version"] == "2026.05.25-1.0"

        # With explicit target_version, picks that
        r = client.get(
            f"/api/applications/team_bot_a/{iid}/adopt-preview?target_version=2026.05.22-1.0"
        )
        assert r.get_json()["to_version"] == "2026.05.22-1.0"

    def test_instance_not_found_404(self, app_env):
        client, _ = app_env
        r = client.get("/api/applications/team_bot_a/i-missing/adopt-preview")
        assert r.status_code == 404

    def test_no_spec_in_gallery_400(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        # No Specs written to gallery

        r = client.get(f"/api/applications/team_bot_a/{iid}/adopt-preview")
        assert r.status_code == 400
        assert "no Spec" in r.get_json()["error"]

    def test_v13_instance_rejected_400(self, app_env):
        client, env = app_env
        v13 = {"id": "old-app", "schema_version": 13}
        (env["manifests"] / "old-app.json").write_text(json.dumps(v13))

        r = client.get("/api/applications/team_bot_a/old-app/adopt-preview")
        assert r.status_code == 400
        assert "v7-arc" in r.get_json()["error"]


# ── POST adopt ──────────────────────────────────────────────────────────────


class TestAdoptPost:
    def test_presentation_only_rebinds_provenance(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "p-aaaa1111", "2026.05.20-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0", description="bumped")

        r = client.post(f"/api/applications/team_bot_a/{iid}/adopt", json={})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["from_version"] == "2026.05.20-1.0"
        assert data["to_version"] == "2026.05.22-1.0"

        # Instance on disk is rewritten
        on_disk = json.loads((env["manifests"] / f"{iid}.json").read_text())
        assert on_disk["provenance"]["spec_version"] == "2026.05.22-1.0"
        # History entry appended
        assert on_disk["spec_version_history"][-1]["version"] == "2026.05.22-1.0"
        assert on_disk["spec_version_history"][-1]["reason"] == "manual_adopt"

    def test_custom_reason_recorded(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "p-aaaa1111", "2026.05.20-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0", description="bumped")

        r = client.post(
            f"/api/applications/team_bot_a/{iid}/adopt",
            json={"reason": "lesson_adoption"},
        )
        assert r.status_code == 200
        on_disk = json.loads((env["manifests"] / f"{iid}.json").read_text())
        assert on_disk["spec_version_history"][-1]["reason"] == "lesson_adoption"

    def test_structural_diff_refused_400(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "p-aaaa1111", "2026.05.20-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0",
                   blueprint={"approach": "different"})

        r = client.post(f"/api/applications/team_bot_a/{iid}/adopt", json={})
        assert r.status_code == 400
        data = r.get_json()
        assert "structural" in data["error"].lower() or "Forge rebuild" in data["error"]
        # Diff is included so the UI can show what blocked the adopt
        assert data["spec_diff"]["kind"] == "structural"

        # Instance is NOT rewritten
        on_disk = json.loads((env["manifests"] / f"{iid}.json").read_text())
        assert on_disk["provenance"]["spec_version"] == "2026.05.20-1.0"
        assert "spec_version_history" not in on_disk or not on_disk.get("spec_version_history")

    def test_noop_when_already_at_target(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env, version="2026.05.22-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0")

        r = client.post(
            f"/api/applications/team_bot_a/{iid}/adopt",
            json={"target_version": "2026.05.22-1.0"},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data.get("noop") is True

        # Instance untouched — no history entry added
        on_disk = json.loads((env["manifests"] / f"{iid}.json").read_text())
        assert not on_disk.get("spec_version_history")

    def test_v13_instance_400(self, app_env):
        client, env = app_env
        v13 = {"id": "old-app", "schema_version": 13}
        (env["manifests"] / "old-app.json").write_text(json.dumps(v13))

        r = client.post("/api/applications/team_bot_a/old-app/adopt", json={})
        assert r.status_code == 400

    def test_explicit_target_version_in_body(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "p-aaaa1111", "2026.05.20-1.0")
        _seed_spec(env, "p-aaaa1111", "2026.05.22-1.0", description="v1.1")
        _seed_spec(env, "p-aaaa1111", "2026.05.25-1.0", description="v1.2")

        # Adopt to the middle version, not the latest
        r = client.post(
            f"/api/applications/team_bot_a/{iid}/adopt",
            json={"target_version": "2026.05.22-1.0"},
        )
        assert r.status_code == 200
        assert r.get_json()["to_version"] == "2026.05.22-1.0"
