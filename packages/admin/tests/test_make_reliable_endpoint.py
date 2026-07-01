"""
Tests for POST /api/applications/<bot>/<app_id>/make-reliable.

The endpoint re-forges a script-backed app's installed manifest from
agent_invokes → plugin_intercept (web/routes_applications_reliability.py).
It must: persist a real migration via the sanctioned write path, be
idempotent, refuse cleanly when the app can't be made reliable, refuse when
the script the new contract points at isn't on disk, and 404 on a missing
manifest.

Spec: docs/spec-agent-freelance-bypass-phase2-2026-06-06.md.
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
    """create_app + a bot manifests/scripts tree we control via path redirection."""
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    bot_dir = tmp_path / "bot-homes" / "team_bot_a"
    workspace = bot_dir / ".openclaw" / "workspace"
    manifests = workspace / "manifests"
    scripts = workspace / "scripts"
    manifests.mkdir(parents=True)
    scripts.mkdir(parents=True)

    network = {
        "networkId": "pod-test-1",
        "sharedDir": str(shared_dir),
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "members": ["team_bot_a"],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    workspace_str = str(workspace)
    from evolve_admin.web import server as srv
    monkeypatch.setattr(
        srv, "resolve_bot_paths",
        lambda bot_id, user=None: {"workspace": workspace_str, "user": user or bot_id},
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client(), {
        "manifests": manifests,
        "scripts": scripts,
    }


def _agent_invokes_manifest() -> dict:
    return {
        "id": "atlas-research",
        "name": "Atlas On-Demand Research",
        "invocation_mode": "agent_invokes",
        "blueprint": {
            "files": [
                {"logical_name": "atlas_research",
                 "expected_location": "scripts/atlas_research.py"}
            ]
        },
        "event_triggers": [
            {
                "id": "at_mention",
                "source": "telegram",
                "audience": "members",
                "invokes": "atlas_research",
                "match": {"channel": "telegram_group",
                          "pattern": r"@[A-Za-z][A-Za-z0-9_]{2,}\b"},
            }
        ],
    }


class TestMakeReliableEndpoint:

    def test_migrates_and_persists(self, app_env):
        client, env = app_env
        (env["manifests"] / "atlas-research.json").write_text(
            json.dumps(_agent_invokes_manifest()))
        # The script the synthesized contract points at exists on disk.
        (env["scripts"] / "atlas_research.py").write_text("print('ok')\n")

        r = client.post("/api/applications/team_bot_a/atlas-research/make-reliable")
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["ok"] is True
        assert body["changed"] is True
        assert body["invocation_mode"] == "plugin_intercept"

        # Persisted via the manifest write path.
        on_disk = json.loads((env["manifests"] / "atlas-research.json").read_text())
        assert on_disk["invocation_mode"] == "plugin_intercept"
        assert on_disk["event_triggers"][0]["invocation"]["stdout_protocol"] == "atlas_research"
        assert on_disk.get("updated_at")

    def test_idempotent_second_call_is_noop(self, app_env):
        client, env = app_env
        (env["manifests"] / "atlas-research.json").write_text(
            json.dumps(_agent_invokes_manifest()))
        (env["scripts"] / "atlas_research.py").write_text("print('ok')\n")

        first = client.post("/api/applications/team_bot_a/atlas-research/make-reliable")
        assert first.get_json()["changed"] is True

        second = client.post("/api/applications/team_bot_a/atlas-research/make-reliable")
        body = second.get_json()
        assert body["ok"] is True
        assert body["changed"] is False
        assert "already" in body["reason"].lower()

    def test_refuses_unknown_protocol(self, app_env):
        client, env = app_env
        m = _agent_invokes_manifest()
        m["event_triggers"][0]["invokes"] = "send_invoice"
        m["blueprint"]["files"][0]["logical_name"] = "send_invoice"
        m["blueprint"]["files"][0]["expected_location"] = "scripts/send_invoice.py"
        (env["manifests"] / "atlas-research.json").write_text(json.dumps(m))

        r = client.post("/api/applications/team_bot_a/atlas-research/make-reliable")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is False
        assert body["changed"] is False
        assert body["reason"]
        # Nothing was written — invocation_mode unchanged on disk.
        on_disk = json.loads((env["manifests"] / "atlas-research.json").read_text())
        assert on_disk["invocation_mode"] == "agent_invokes"

    def test_refuses_when_script_absent(self, app_env):
        client, env = app_env
        (env["manifests"] / "atlas-research.json").write_text(
            json.dumps(_agent_invokes_manifest()))
        # The scripts/ dir exists (fixture) and is readable, but the script
        # itself is deliberately NOT created — a *confirmed* absence (readable
        # parent + missing file), which the tri-state guard refuses on.
        assert env["scripts"].is_dir()
        assert not (env["scripts"] / "atlas_research.py").exists()

        r = client.post("/api/applications/team_bot_a/atlas-research/make-reliable")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is False
        assert "disk" in body["reason"].lower() or "isn't" in body["reason"].lower()
        # Not persisted.
        on_disk = json.loads((env["manifests"] / "atlas-research.json").read_text())
        assert on_disk["invocation_mode"] == "agent_invokes"

    def test_404_when_manifest_missing(self, app_env):
        client, _ = app_env
        r = client.post("/api/applications/team_bot_a/nope/make-reliable")
        assert r.status_code == 404
        assert r.get_json()["ok"] is False
