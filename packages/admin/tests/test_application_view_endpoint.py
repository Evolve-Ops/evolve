"""
Tests for GET /api/applications/<bot>/<app_id> — the View endpoint.

Regression: atlas-article-capture.json + atlas-on-demand-research.json were
written to disk with display-name-slug filenames (atlas-article-capture.json)
while the manifests' internal ``id`` fields were set to gallery-style values
(``app_atlas_article_capture``). The list endpoint returned the internal
``id`` to the UI, but the View endpoint resolved by filename — so View
clicks returned 404 ("x not found") for those two apps while siblings with
filename==id worked fine.

The View endpoint must be resilient to filename-vs-id divergence — it
should resolve by filename first (current behavior, fast path) and then
fall back to scanning manifests for one whose ``id`` (or ``instance_id``)
field matches.
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
    """create_app + a manifests dir we control via path redirection."""
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


class TestApplicationViewEndpoint:

    def test_filename_equals_id_returns_manifest(self, app_env):
        """Sanity: when filename stem == id, View works (baseline)."""
        client, env = app_env
        manifest = {
            "id": "atlas-knowledge",
            "name": "Atlas Knowledge",
            "schema_version": 20,
            "status": "active",
        }
        (env["manifests"] / "atlas-knowledge.json").write_text(json.dumps(manifest))

        r = client.get("/api/applications/team_bot_a/atlas-knowledge")
        assert r.status_code == 200
        assert r.get_json()["name"] == "Atlas Knowledge"

    def test_filename_differs_from_id_field_resolves(self, app_env):
        """
        Regression for atlas-article-capture / atlas-on-demand-research:

        File on disk is ``atlas-article-capture.json`` but its internal
        ``id`` field is ``app_atlas_article_capture``. The list endpoint
        returns the ``id`` value, so the UI invokes
        ``GET /api/applications/<bot>/app_atlas_article_capture`` —
        which must resolve to the differently-named file.
        """
        client, env = app_env
        manifest = {
            "id": "app_atlas_article_capture",
            "name": "Atlas Article Capture",
            "schema_version": 20,
            "manifest_shape": "v7-arc-pre",
            "status": "active",
        }
        # File named by display-slug, id field uses gallery-style prefix
        (env["manifests"] / "atlas-article-capture.json").write_text(json.dumps(manifest))

        # UI clicks View with cap_id = "app_atlas_article_capture" (from list)
        r = client.get("/api/applications/team_bot_a/app_atlas_article_capture")
        assert r.status_code == 200, (
            f"View must resolve by id field when filename differs; "
            f"got {r.status_code} {r.get_data(as_text=True)}"
        )
        body = r.get_json()
        assert body["name"] == "Atlas Article Capture"
        assert body["id"] == "app_atlas_article_capture"

    def test_v7_arc_instance_id_resolves(self, app_env):
        """
        A v7-arc Instance whose filename is its instance_id and whose ``id``
        was added by hydration must still resolve via either form. Here
        the file is ``i-aaaa1111.json`` and we look it up by instance_id.
        """
        client, env = app_env
        inst = {
            "instance_id": "i-aaaa1111",
            "manifest_shape": "v7-arc",
            "schema_version": 14,
            "provenance": {
                "spec_id": "p-missing",
                "spec_version": "2026.05.20-1.0",
                "source_pod_id": None,
            },
            "status": "active",
        }
        (env["manifests"] / "i-aaaa1111.json").write_text(json.dumps(inst))

        r = client.get("/api/applications/team_bot_a/i-aaaa1111")
        assert r.status_code == 200
        assert r.get_json()["instance_id"] == "i-aaaa1111"

    def test_truly_missing_app_returns_404(self, app_env):
        """No filename match and no id-field match → genuine 404."""
        client, env = app_env
        manifest = {"id": "alpha", "name": "Alpha", "schema_version": 20}
        (env["manifests"] / "alpha.json").write_text(json.dumps(manifest))

        r = client.get("/api/applications/team_bot_a/does-not-exist")
        assert r.status_code == 404
