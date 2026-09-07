"""
Tests for /api/applications/<bot_id>/<app_id>/share (Session 4a).

Covers:
  - v13 manifest distill → Spec written + source attribution stamped
  - v7-arc instance distill → uses existing Spec
  - Bot not in network → 404
  - Manifest not found → 404
  - target_bot_id + install: true → install job created
  - target_bot_id == source → install_warning, no job
  - target_bot_id not in network → install_warning
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

from evolve_admin.web.share_routes import distill_to_spec, register_share_routes


# ── Fixture setup ────────────────────────────────────────────────────────────

@pytest.fixture
def share_env(tmp_path, monkeypatch):
    """
    A self-contained share env:
      - tmp_path/network.json
      - tmp_path/shared/                     (shared_dir)
      - tmp_path/bot-homes/<bot_id>/.openclaw/workspace/manifests/

    bot_home is monkey-patched to return tmp_path/bot-homes/<bot_id> so route
    handlers resolve to our test workspace instead of /Users/<bot_id>.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    bot_homes = tmp_path / "bot-homes"
    bot_homes.mkdir()
    # In production, network.json lives at {shared_dir}/network.json — match
    # that layout so _local_pod_id resolves correctly via shared_dir.
    network_path = shared_dir / "network.json"
    network_path.write_text(json.dumps({
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {
            "team_bot_a":   {"user": "team_bot_a"},
            "admin_bot": {"user": "admin_bot"},
            "personal_bot": {"user": "personal_bot"},
        },
    }))

    def fake_bot_home(bot_id, network=None):
        return bot_homes / bot_id

    # Patch both the share_routes import site and the underlying config module
    # so any code path that resolves bot_home routes to our tmp dirs.
    from evolve_admin.web import share_routes as sr
    from evolve_admin import config as cfg
    monkeypatch.setattr(sr, "_load_manifest", lambda bot_id, app_id, network: (
        json.loads(
            (bot_homes / bot_id / ".openclaw/workspace/manifests" / f"{app_id}.json").read_text()
        ) if (bot_homes / bot_id / ".openclaw/workspace/manifests" / f"{app_id}.json").is_file() else None
    ))
    monkeypatch.setattr(cfg, "bot_home", fake_bot_home)

    return {
        "shared_dir": shared_dir,
        "bot_homes": bot_homes,
        "network_path": network_path,
    }


@pytest.fixture
def app_client(share_env):
    app = Flask(__name__)
    register_share_routes(app, share_env["network_path"], share_env["shared_dir"])
    return app.test_client()


def _write_manifest(env, bot_id: str, app_id: str, data: dict):
    """Drop a manifest into the bot's workspace dir."""
    path = env["bot_homes"] / bot_id / ".openclaw/workspace/manifests" / f"{app_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _v13_manifest(**overrides) -> dict:
    base = {
        "id": "journal",
        "name": "Journal",
        "bot_id": "team_bot_a",
        "description": "Daily log",
        "status": "active",
        "schema_version": 13,
        "pkg_id": "p-abcd1234",
        "files": [],
        "crons": [],
    }
    base.update(overrides)
    return base


# ── Distill tests (unit, no Flask) ────────────────────────────────────────────

class TestDistillToSpec:
    def test_v13_manifest_distills_to_spec(self, share_env):
        manifest = _v13_manifest()
        spec, spec_id, spec_version = distill_to_spec(
            manifest, "team_bot_a", share_env["shared_dir"]
        )
        assert spec_id == "p-abcd1234"  # legacy pkg_id preserved
        assert spec["manifest_shape"] == "v7-arc"
        assert spec["source"]["bot_id"] == "team_bot_a"
        assert spec["source"]["pod_id"] == "pod-test-1"
        assert "shared_at" in spec["source"]

    def test_v7_instance_uses_existing_spec(self, share_env):
        # Seed a Spec in gallery/local/
        spec_path = (
            share_env["shared_dir"] / "gallery" / "local"
            / "p-existing1" / "2026.05.20-1.0.json"
        )
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text(json.dumps({
            "spec_id": "p-existing1",
            "spec_version": "2026.05.20-1.0",
            "name": "Pre-existing Spec",
            "schema_version": 14,
            "manifest_shape": "v7-arc",
            "objective": {"primary": "test"},
            "blueprint": {"files": []},
            "dependencies": {
                "apps": [], "python_packages": [], "system_packages": [],
                "oc_plugins": [], "oc_skills": [], "integrations": [], "credentials": [],
            },
            "audience_scoping": {
                "operator": "operator_only", "approved_surfaces": [],
                "role_capabilities": {},
            },
        }))
        instance = {
            "instance_id": "i-foo",
            "bot_id": "team_bot_a",
            "manifest_shape": "v7-arc",
            "provenance": {
                "spec_id": "p-existing1",
                "spec_version": "2026.05.20-1.0",
            },
        }
        spec, sid, sv = distill_to_spec(instance, "team_bot_a", share_env["shared_dir"])
        assert sid == "p-existing1"
        assert sv == "2026.05.20-1.0"
        assert spec["name"] == "Pre-existing Spec"  # loaded from disk
        assert spec["source"]["bot_id"] == "team_bot_a"     # newly stamped

    def test_v7_instance_missing_spec_raises(self, share_env):
        instance = {
            "instance_id": "i-foo",
            "manifest_shape": "v7-arc",
            "provenance": {
                "spec_id": "p-missing0",
                "spec_version": "2026.05.20-1.0",
            },
        }
        with pytest.raises(FileNotFoundError):
            distill_to_spec(instance, "team_bot_a", share_env["shared_dir"])

    def test_v7_instance_missing_provenance_raises(self, share_env):
        instance = {"manifest_shape": "v7-arc", "provenance": {}}
        with pytest.raises(ValueError, match="spec_id"):
            distill_to_spec(instance, "team_bot_a", share_env["shared_dir"])


# ── Endpoint tests ────────────────────────────────────────────────────────────

class TestShareEndpoint:
    def test_share_v13_writes_spec(self, app_client, share_env):
        _write_manifest(share_env, "team_bot_a", "journal", _v13_manifest())
        resp = app_client.post("/api/applications/team_bot_a/journal/share", json={})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["spec_id"] == "p-abcd1234"
        assert body["spec_version"] == "2026.05.20-1.0"
        assert body["source_bot_id"] == "team_bot_a"
        spec_path = Path(body["spec_path"])
        assert spec_path.exists()
        spec = json.loads(spec_path.read_text())
        assert spec["source"]["bot_id"] == "team_bot_a"
        assert spec["source"]["pod_id"] == "pod-test-1"

    def test_unknown_bot_404(self, app_client):
        resp = app_client.post("/api/applications/ghost/journal/share", json={})
        assert resp.status_code == 404
        assert "unknown bot_id" in resp.get_json()["error"]

    def test_missing_manifest_404(self, app_client, share_env):
        # bot exists but app doesn't
        resp = app_client.post("/api/applications/team_bot_a/nonexistent/share", json={})
        assert resp.status_code == 404
        assert "no manifest" in resp.get_json()["error"]

    def test_body_must_be_dict_or_absent(self, app_client, share_env):
        _write_manifest(share_env, "team_bot_a", "journal", _v13_manifest())
        # Pass a list — should be rejected
        resp = app_client.post(
            "/api/applications/team_bot_a/journal/share",
            data=json.dumps(["not", "an", "object"]),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_no_body_is_ok(self, app_client, share_env):
        _write_manifest(share_env, "team_bot_a", "journal", _v13_manifest())
        resp = app_client.post("/api/applications/team_bot_a/journal/share")
        assert resp.status_code == 200

    def test_target_same_bot_warning(self, app_client, share_env):
        _write_manifest(share_env, "team_bot_a", "journal", _v13_manifest())
        resp = app_client.post(
            "/api/applications/team_bot_a/journal/share",
            json={"target_bot_id": "team_bot_a", "install": True},
        )
        body = resp.get_json()
        assert resp.status_code == 200
        assert "install_warning" in body
        assert "matches source bot" in body["install_warning"]
        assert "install_job" not in body

    def test_target_unknown_bot_warning(self, app_client, share_env):
        _write_manifest(share_env, "team_bot_a", "journal", _v13_manifest())
        resp = app_client.post(
            "/api/applications/team_bot_a/journal/share",
            json={"target_bot_id": "ghost", "install": True},
        )
        body = resp.get_json()
        assert resp.status_code == 200
        assert "install_warning" in body
        assert "not in network.json" in body["install_warning"]

    def test_target_without_install_flag_no_op(self, app_client, share_env):
        # target_bot_id provided but install=False (default) → no install
        _write_manifest(share_env, "team_bot_a", "journal", _v13_manifest())
        resp = app_client.post(
            "/api/applications/team_bot_a/journal/share",
            json={"target_bot_id": "admin_bot"},
        )
        body = resp.get_json()
        assert resp.status_code == 200
        assert "install_job" not in body
        assert "install_warning" not in body

    def test_target_with_install_creates_job(self, app_client, share_env, monkeypatch):
        # Stub create_install_job so we don't depend on the full forge_jobs
        # machinery; we just verify the share endpoint calls it correctly.
        captured = {}
        from evolve_admin.applications import forge_jobs as fj

        class FakeJob:
            job_id = "job-test-123"
            status = "queued"

        def fake_create_install_job(*, pkg_id, app_id, bot_id, gallery_version, shared_dir):
            captured["pkg_id"] = pkg_id
            captured["app_id"] = app_id
            captured["bot_id"] = bot_id
            captured["gallery_version"] = gallery_version
            return FakeJob()

        monkeypatch.setattr(fj, "create_install_job", fake_create_install_job)

        _write_manifest(share_env, "team_bot_a", "journal", _v13_manifest())
        resp = app_client.post(
            "/api/applications/team_bot_a/journal/share",
            json={"target_bot_id": "admin_bot", "install": True},
        )
        body = resp.get_json()
        assert resp.status_code == 200
        assert "install_warning" not in body, body.get("install_warning")
        assert body["install_job"]["job_id"] == "job-test-123"
        assert body["install_job"]["bot_id"] == "admin_bot"
        # Verify the forge call got the right params
        assert captured["pkg_id"] == "p-abcd1234"
        assert captured["bot_id"] == "admin_bot"
        assert captured["gallery_version"] == "2026.05.20-1.0"
