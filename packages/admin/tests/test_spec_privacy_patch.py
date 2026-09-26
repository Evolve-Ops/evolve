"""
Tests for PATCH /api/applications/<bot_id>/<app_id>/spec-privacy.

Flips the bound Spec's privacy.shareable_in_lessons flag from the UI
without requiring the operator to edit JSON. Only mutates Specs in
gallery/local (upstream-owned tiers are read-only — operator forks first).
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
    from evolve_admin.web import server as srv
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

    monkeypatch.setattr(
        srv, "resolve_bot_paths",
        lambda bid, user=None: {
            "workspace": str(bot_dir / ".openclaw" / "workspace"),
            "user": user or bid,
        },
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "shared_dir": shared_dir,
        "manifests": manifests,
    }


def _seed_v7_instance(env, spec_id="p-aaaa1111", version="2026.05.20-1.0"):
    iid = "i-12345678"
    inst = {
        "instance_id": iid,
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
    (env["manifests"] / f"{iid}.json").write_text(json.dumps(inst))
    return iid


def _seed_spec(env, tier, spec_id, version, shareable=False):
    d = env["shared_dir"] / "gallery" / tier / spec_id
    d.mkdir(parents=True, exist_ok=True)
    spec = {
        "spec_id": spec_id,
        "spec_version": version,
        "name": "Test App",
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "objective": {"primary": "x", "sub_objectives": []},
        "blueprint": {"approach": "test"},
        "dependencies": [],
        "audience_scoping": {"operator": "operator_only",
                              "approved_surfaces": [],
                              "role_capabilities": {}},
        "privacy": {"shareable_in_lessons": shareable},
    }
    (d / f"{version}.json").write_text(json.dumps(spec))
    return d / f"{version}.json"


class TestSpecPrivacyPatch:
    def test_flip_from_false_to_true(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        spec_path = _seed_spec(env, "local", "p-aaaa1111", "2026.05.20-1.0",
                                shareable=False)

        r = client.patch(f"/api/applications/team_bot_a/{iid}/spec-privacy",
                         json={"shareable_in_lessons": True})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["shareable_in_lessons"] is True
        assert body["spec_tier"] == "local"

        # Spec on disk reflects new value
        spec = json.loads(spec_path.read_text())
        assert spec["privacy"]["shareable_in_lessons"] is True

    def test_flip_from_true_to_false(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        spec_path = _seed_spec(env, "local", "p-aaaa1111", "2026.05.20-1.0",
                                shareable=True)

        r = client.patch(f"/api/applications/team_bot_a/{iid}/spec-privacy",
                         json={"shareable_in_lessons": False})
        assert r.status_code == 200
        spec = json.loads(spec_path.read_text())
        assert spec["privacy"]["shareable_in_lessons"] is False

    def test_creates_privacy_block_if_missing(self, app_env):
        """A Spec with no privacy block at all should get one created."""
        client, env = app_env
        iid = _seed_v7_instance(env)
        # Custom spec without a privacy block
        d = env["shared_dir"] / "gallery" / "local" / "p-aaaa1111"
        d.mkdir(parents=True)
        spec_path = d / "2026.05.20-1.0.json"
        spec_path.write_text(json.dumps({
            "spec_id": "p-aaaa1111",
            "spec_version": "2026.05.20-1.0",
            "name": "Bare",
            "schema_version": 14,
            "manifest_shape": "v7-arc",
        }))

        r = client.patch(f"/api/applications/team_bot_a/{iid}/spec-privacy",
                         json={"shareable_in_lessons": True})
        assert r.status_code == 200
        spec = json.loads(spec_path.read_text())
        assert spec["privacy"]["shareable_in_lessons"] is True


class TestSpecPrivacyPatchValidation:
    def test_non_boolean_value_400(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "local", "p-aaaa1111", "2026.05.20-1.0", shareable=True)

        r = client.patch(f"/api/applications/team_bot_a/{iid}/spec-privacy",
                         json={"shareable_in_lessons": "yes"})
        assert r.status_code == 400
        assert "boolean" in r.get_json()["error"]

    def test_missing_body_400(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "local", "p-aaaa1111", "2026.05.20-1.0", shareable=True)

        r = client.patch(f"/api/applications/team_bot_a/{iid}/spec-privacy", json={})
        assert r.status_code == 400

    def test_v13_instance_400(self, app_env):
        client, env = app_env
        # Legacy v13
        (env["manifests"] / "old-app.json").write_text(json.dumps({
            "id": "old-app", "schema_version": 13,
        }))
        r = client.patch("/api/applications/team_bot_a/old-app/spec-privacy",
                         json={"shareable_in_lessons": True})
        assert r.status_code == 400
        assert "v7-arc" in r.get_json()["error"]

    def test_instance_not_found_404(self, app_env):
        client, _ = app_env
        r = client.patch("/api/applications/team_bot_a/i-missing/spec-privacy",
                         json={"shareable_in_lessons": True})
        assert r.status_code == 404

    def test_builtin_tier_refused_400_with_fork_hint(self, app_env):
        """Builtin Specs are upstream-owned — refuse + tell operator to fork
        by re-sharing the app locally first."""
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "builtin", "p-aaaa1111", "2026.05.20-1.0", shareable=False)

        r = client.patch(f"/api/applications/team_bot_a/{iid}/spec-privacy",
                         json={"shareable_in_lessons": True})
        assert r.status_code == 400
        body = r.get_json()
        assert body["spec_tier"] == "builtin"
        assert "fork" in body["error"].lower() or "re-share" in body["error"].lower()

    def test_spec_not_in_gallery_at_all_404(self, app_env):
        """Instance pins a Spec that's been deleted — 404."""
        client, env = app_env
        iid = _seed_v7_instance(env)
        # No Spec file seeded

        r = client.patch(f"/api/applications/team_bot_a/{iid}/spec-privacy",
                         json={"shareable_in_lessons": True})
        assert r.status_code == 404


class TestLessonsGetReturnsSpecPrivacy:
    """The Lessons GET response carries the bound Spec's privacy block so
    the UI can render the toggle in one round-trip."""

    def test_includes_spec_privacy_in_summary(self, app_env):
        client, env = app_env
        iid = _seed_v7_instance(env)
        _seed_spec(env, "local", "p-aaaa1111", "2026.05.20-1.0", shareable=True)
        # Lessons file
        (env["shared_dir"] / "lessons" / "team_bot_a").mkdir(parents=True)
        (env["shared_dir"] / "lessons" / "team_bot_a" / "p-aaaa1111.json").write_text(
            json.dumps({
                "lessons_id": "l-12345678",
                "spec_id": "p-aaaa1111",
                "spec_version_observed": "2026.05.20-1.0",
                "source_pod_id": "pod-test-1",
                "source_bot_id": "team_bot_a",
                "observation_window": {
                    "start": "2026-05-20T00:00:00Z",
                    "end": "2026-05-20T00:00:00Z",
                    "instance_runs": 0,
                },
                "lessons": [],
                "redaction_applied": False,
            })
        )

        r = client.get("/api/lessons/team_bot_a/p-aaaa1111")
        assert r.status_code == 200
        s = r.get_json()["summary"]
        assert s["spec_privacy"]["shareable_in_lessons"] is True
        assert s["spec_tier"] == "local"
        assert s["spec_version_for_privacy"] == "2026.05.20-1.0"

    def test_includes_spec_tier_builtin_when_only_in_builtin(self, app_env):
        client, env = app_env
        _seed_v7_instance(env)
        # Spec only in builtin, not local
        _seed_spec(env, "builtin", "p-aaaa1111", "2026.05.20-1.0", shareable=False)
        (env["shared_dir"] / "lessons" / "team_bot_a").mkdir(parents=True)
        (env["shared_dir"] / "lessons" / "team_bot_a" / "p-aaaa1111.json").write_text(
            json.dumps({
                "lessons_id": "l-12345678",
                "spec_id": "p-aaaa1111",
                "spec_version_observed": "2026.05.20-1.0",
                "source_pod_id": "pod-test-1",
                "source_bot_id": "team_bot_a",
                "observation_window": {
                    "start": "2026-05-20T00:00:00Z",
                    "end": "2026-05-20T00:00:00Z",
                    "instance_runs": 0,
                },
                "lessons": [],
                "redaction_applied": False,
            })
        )

        r = client.get("/api/lessons/team_bot_a/p-aaaa1111")
        assert r.status_code == 200
        s = r.get_json()["summary"]
        assert s["spec_tier"] == "builtin"
        assert s["spec_privacy"] is not None
        # Shareable still reflects the Spec's value
        assert s["spec_privacy"].get("shareable_in_lessons") is False
