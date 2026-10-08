"""Pause/deprecate must unsplice app guidance from AGENTS.md (and back).

Base-spec §8.4 step 2 closed in manifest-v7 Slice 2: the lifecycle routes
(pause/unpause/archive/restore) and status-flipping PATCHes regenerate
INSTALLED_APPS.md + the AGENTS.md EVOLVE-INSTALLED-APPS marker block after
the status write. regenerate_installed_apps_md filters to
_VISIBLE_STATUSES, so the symmetry is by construction:

    pause / archive / deprecate → app's guidance entries disappear
    unpause / restore           → re-spliced

Before this, a paused app's "USE THESE FOR THE THINGS THEY DO" guidance
stayed live in AGENTS.md until the next forge/scanner run — the bot kept
being told to use an app whose triggers and crons were dark (#2641).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


BOT = "team_bot_a"
APP = "journal"


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """create_app with manifests + workspace redirected into tmp_path."""
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    workspace = tmp_path / "bot-homes" / BOT / ".openclaw" / "workspace"
    manifests = workspace / "manifests"
    manifests.mkdir(parents=True)

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {BOT: {"user": BOT}},
        "members": [BOT],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Server-side reads/writes (_read/_write_manifest_as_bot).
    from evolve_admin.web import server as srv
    monkeypatch.setattr(
        srv, "resolve_bot_paths",
        lambda bot_id, user=None: {
            "workspace": str(workspace),
            "user": user or bot_id,
        },
    )
    # manifest.applications_dir (late import from config) + app_registry's
    # module-level binding — both must point at the tmp workspace.
    import evolve_admin.config as cfg
    import evolve_admin.applications.app_registry as app_registry
    monkeypatch.setattr(cfg, "get_bot_workspace", lambda bot_id, user=None: workspace)
    monkeypatch.setattr(app_registry, "get_bot_workspace", lambda bot_id, user=None: workspace)

    # AGENTS.md must pre-exist (regenerate never creates it from scratch).
    (workspace / "AGENTS.md").write_text(
        "# Identity\n\nOperator-authored content stays.\n"
    )

    (manifests / f"{APP}.json").write_text(json.dumps({
        "id": APP,
        "name": "Journal",
        "display_name": "Journal",
        "description": "Daily log keeper",
        "bot_id": BOT,
        "status": "active",
        # Defined (operator-vouched) so it appears in the Tier-1 always-on menu —
        # the menu lists defined apps only (spec OQ-3). This suite tests
        # lifecycle splice/unsplice, orthogonal to the defined/discovered axis.
        "definition_status": "defined",
    }))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {"workspace": workspace, "manifests": manifests}


def _agents_md(env) -> str:
    return (env["workspace"] / "AGENTS.md").read_text()


class TestLifecycleUnsplice:
    def test_pause_unsplices_and_unpause_resplices(self, app_env):
        client, env = app_env

        r = client.post(f"/api/applications/{BOT}/{APP}/pause")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["guidance_resynced"] is True
        text = _agents_md(env)
        assert "Journal" not in text
        # Marker block exists but is in its empty state; operator content intact.
        assert "EVOLVE-INSTALLED-APPS" in text
        assert "Operator-authored content stays." in text

        r = client.post(f"/api/applications/{BOT}/{APP}/unpause")
        assert r.status_code == 200
        assert r.get_json()["guidance_resynced"] is True
        text = _agents_md(env)
        assert "**Journal**" in text
        installed = (env["workspace"] / "INSTALLED_APPS.md").read_text()
        assert "Journal" in installed

    def test_archive_unsplices(self, app_env):
        client, env = app_env
        r = client.post(f"/api/applications/{BOT}/{APP}/archive")
        assert r.status_code == 200
        assert "Journal" not in _agents_md(env)

    def test_deprecate_via_patch_unsplices(self, app_env):
        client, env = app_env
        # First splice it in (regenerate runs on any lifecycle action).
        client.post(f"/api/applications/{BOT}/{APP}/unpause")
        assert "**Journal**" in _agents_md(env)

        # No dedicated deprecate route — status flips through PATCH.
        r = client.patch(
            f"/api/applications/{BOT}/{APP}",
            json={"status": "deprecated"},
        )
        assert r.status_code == 200
        assert "Journal" not in _agents_md(env)

    def test_non_status_patch_leaves_agents_md_alone(self, app_env):
        client, env = app_env
        client.post(f"/api/applications/{BOT}/{APP}/unpause")
        before = _agents_md(env)
        mtime_before = (env["workspace"] / "AGENTS.md").stat().st_mtime_ns

        r = client.patch(
            f"/api/applications/{BOT}/{APP}",
            json={"description": "Updated description"},
        )
        assert r.status_code == 200
        assert _agents_md(env) == before
        assert (env["workspace"] / "AGENTS.md").stat().st_mtime_ns == mtime_before
