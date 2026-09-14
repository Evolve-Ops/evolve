"""tests/test_backup_data_endpoints.py — Phase 3b data classification endpoints.

Covers GET /api/backup/data/overview, PATCH /api/backup/data/app,
PATCH /api/backup/data/pod.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent  # packages/admin
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


def _write_manifest(workspace_dir: Path, app_id: str, manifest_dict: dict) -> None:
    """Drop a manifest JSON into the bot's workspace/manifests/ dir."""
    mdir = workspace_dir / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    full = {
        "id":   app_id,
        "name": app_id.replace("-", " ").title(),
        "bot_id": "team_bot_a",
        "schema_version": 15,
        **manifest_dict,
    }
    (mdir / f"{app_id}.json").write_text(json.dumps(full))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Flask client + a fake team_bot_a bot whose home is in tmp_path."""
    from evolve_admin.web.server import create_app

    bot_home = tmp_path / "team_bot_a-home"
    (bot_home / ".openclaw" / "workspace").mkdir(parents=True)

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "sharedDir": str(tmp_path / "shared"),
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    # Re-target the bot_home lookup to our tmp dir.
    monkeypatch.setattr(
        "evolve_admin.web.server._bot_home",
        lambda bot_id, net=None: bot_home,
    )
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot_id, net=None: bot_home,
    )
    # The 2026-05-29 endpoint refactor uses _list_manifests_as_bot for
    # parity with the Apps tab. That helper resolves the workspace dir
    # via _bot_manifests_dir, which doesn't go through the patched
    # _bot_home. Patch it directly so tests hit our tmp dir.
    monkeypatch.setattr(
        "evolve_admin.web.server._bot_manifests_dir",
        lambda bot_id, user=None: bot_home / ".openclaw" / "workspace" / "manifests",
    )

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, net_file, bot_home


# ─── GET /api/backup/data/overview ─────────────────────────────────────────

def test_overview_empty_pod_returns_clean_shape(client):
    c, _, _ = client
    resp = c.get("/api/backup/data/overview")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "bots" in data
    assert "team_bot_a" in data["bots"]
    assert data["bots"]["team_bot_a"]["apps"] == []  # no manifests yet
    assert data["pod_wide"]["data_paths"] == []
    assert data["pod_wide"]["default_for_unclassified"] == ""


def test_overview_returns_per_app_tier(client):
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "local",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    })
    _write_manifest(workspace, "index", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "index/", "privacy": "cloud"}],
    })
    _write_manifest(workspace, "legacy", {})  # no classification

    resp = c.get("/api/backup/data/overview")
    assert resp.status_code == 200
    apps = {a["id"]: a for a in resp.get_json()["bots"]["team_bot_a"]["apps"]}
    assert apps["notes"]["current_tier"]  == "all_data_local"
    assert apps["index"]["current_tier"]  == "full_cloud"
    assert apps["legacy"]["current_tier"] == "unclassified"
    assert apps["notes"]["data_paths"]    == [{"path": "notes/", "privacy": "local"}]


def test_overview_includes_v7_arc_instance_manifests(client, monkeypatch):
    """Regression for the 2026-05-29 Data tab vs Apps tab mismatch.

    v7-arc Instance manifests don't carry id/name at the top level —
    those live on the bound Spec. The earlier endpoint used a strict
    ``id``-required filter that silently dropped every v7-arc Instance,
    making the Data tab report empty for bots whose Apps tab showed
    apps. After the fix, the endpoint should:

    1. Include manifests without ``id`` (derived from filename).
    2. Try v7-arc hydration so display names come through.
    3. Surface classification fields stored on the Instance.
    """
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    # A v7-arc Instance manifest with classification fields. No id; no
    # name. The bound Spec doesn't exist in this test fixture, so
    # hydration returns the instance unchanged — but the endpoint still
    # surfaces it as an app keyed by filename.
    (mdir / "diary-app.json").write_text(json.dumps({
        "manifest_shape": "v7-arc",
        "provenance": {"spec_id": "diary", "spec_version": "1.0.0"},
        "data_paths": [{"path": "diary/", "privacy": "local"}],
        "app_files_privacy": "cloud",
    }))

    resp = c.get("/api/backup/data/overview")
    assert resp.status_code == 200
    apps = {a["id"]: a for a in resp.get_json()["bots"]["team_bot_a"]["apps"]}
    assert "diary-app" in apps, (
        "v7-arc Instance manifest dropped — Data tab vs Apps tab "
        "discovery mismatch regressed"
    )
    assert apps["diary-app"]["data_paths"] == [{"path": "diary/", "privacy": "local"}]
    assert apps["diary-app"]["app_files_privacy"] == "cloud"


def test_overview_returns_pod_wide_block(client):
    c, net_file, _ = client
    # Write pod-wide rules into network.json directly.
    net = json.loads(net_file.read_text())
    net["backup"] = {
        "data_paths": [
            {"path": "proposals/", "privacy": "cloud"},
            {"path": "observations/", "privacy": "local"},
        ],
        "default_for_unclassified": "local",
    }
    net_file.write_text(json.dumps(net))

    resp = c.get("/api/backup/data/overview")
    pod = resp.get_json()["pod_wide"]
    assert pod["default_for_unclassified"] == "local"
    assert pod["data_paths"][0]["path"] == "proposals/"


# ─── PATCH /api/backup/data/app ────────────────────────────────────────────

def test_patch_app_requires_bot_and_app_id(client):
    c, _, _ = client
    resp = c.patch("/api/backup/data/app", json={"tier": "whole_app_local"})
    assert resp.status_code == 400


def test_patch_app_404_when_manifest_missing(client):
    c, _, _ = client
    resp = c.patch("/api/backup/data/app", json={
        "bot_id": "team_bot_a", "app_id": "does-not-exist", "tier": "full_cloud",
    })
    assert resp.status_code == 404


def test_patch_app_tier_overwrites_fields(client):
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "notes/", "privacy": "cloud"}],
    })

    # Force manifests to write into our tmp dir (not the real ~/.openclaw).
    # The save path uses get_bot_workspace; we already patched _bot_home, but
    # save_manifest goes via applications_dir which calls get_bot_workspace.
    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "tier": "whole_app_local",
        })
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["current_tier"] == "whole_app_local"
    assert data["app_files_privacy"] == "local"
    assert data["default_for_unclassified"] == "local"
    assert data["data_paths"][0]["privacy"] == "local"


def test_patch_app_direct_fields_layered_on_tier(client):
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "notes/", "privacy": "cloud"}],
    })

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "tier": "all_data_local",
            # Override one of the tier-derived fields:
            "default_for_unclassified": "cloud",
        })
    assert resp.status_code == 200
    data = resp.get_json()
    # default_for_unclassified came from the explicit field, not the tier
    assert data["default_for_unclassified"] == "cloud"
    # app_files_privacy still from the tier template
    assert data["app_files_privacy"] == "cloud"


def test_patch_app_rejects_invalid_privacy(client):
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {})

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "app_files_privacy": "nonsense",
        })
    assert resp.status_code == 400


def test_patch_app_rejects_malformed_data_paths(client):
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {})

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "data_paths": [{"path": "notes/", "privacy": "purple"}],
        })
    assert resp.status_code == 400


def test_patch_app_rejects_unknown_tier(client):
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {})

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "tier": "what-even-is-this",
        })
    assert resp.status_code == 400


def test_patch_app_data_paths_only(client):
    """Setting just data_paths writes them through without touching tier-derived fields."""
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
    })

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "data_paths": [
                {"path": "notes/private/", "privacy": "local"},
                {"path": "notes/public/",  "privacy": "cloud", "note": "shareable"},
            ],
        })
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["data_paths"]) == 2
    assert data["data_paths"][1]["note"] == "shareable"
    # app_files_privacy stayed as before:
    assert data["app_files_privacy"] == "cloud"


def test_patch_app_warns_when_per_app_default_set(client):
    """Regression for the 2026-05-29 review-session bug.

    Per-app ``default_for_unclassified`` is stored on the manifest but
    not enforced at runtime (the resolver doesn't read it — see the
    docstring in data_classification._rules_from_app_manifest). The
    PATCH must surface a ``warning`` so operators don't believe they
    configured something live.
    """
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {})

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "default_for_unclassified": "local",
        })
    assert resp.status_code == 200
    data = resp.get_json()
    assert "warning" in data
    assert "not yet enforced" in data["warning"]


def test_patch_app_no_warning_when_per_app_default_unset(client):
    """Symmetric: no warning when the per-app default isn't set."""
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {})

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/app", json={
            "bot_id": "team_bot_a", "app_id": "notes",
            "app_files_privacy": "local",
        })
    assert resp.status_code == 200
    assert "warning" not in resp.get_json()


# ─── PATCH /api/backup/data/bot (per-bot default tier) ────────────────────


def test_patch_bot_requires_bot_id(client):
    c, _, _ = client
    resp = c.patch("/api/backup/data/bot", json={"tier": "full_cloud"})
    assert resp.status_code == 400


def test_patch_bot_rejects_unknown_tier(client):
    c, _, _ = client
    resp = c.patch("/api/backup/data/bot", json={
        "bot_id": "team_bot_a", "tier": "made-up-tier",
    })
    assert resp.status_code == 400


def test_patch_bot_stores_default_in_network_json(client):
    c, net_file, _ = client
    resp = c.patch("/api/backup/data/bot", json={
        "bot_id": "team_bot_a", "tier": "all_data_local",
    })
    assert resp.status_code == 200
    assert resp.get_json()["backup_default_tier"] == "all_data_local"
    assert resp.get_json()["apps_updated"] == 0  # apply_to_existing not set

    net = json.loads(net_file.read_text())
    assert net["bots"]["team_bot_a"]["backup_default_tier"] == "all_data_local"


def test_patch_bot_clears_default_when_tier_empty(client):
    c, net_file, _ = client
    # Seed an existing default
    net = json.loads(net_file.read_text())
    net["bots"]["team_bot_a"]["backup_default_tier"] = "full_cloud"
    net_file.write_text(json.dumps(net))

    resp = c.patch("/api/backup/data/bot", json={"bot_id": "team_bot_a", "tier": ""})
    assert resp.status_code == 200
    assert resp.get_json()["backup_default_tier"] == ""

    net = json.loads(net_file.read_text())
    assert "backup_default_tier" not in net["bots"]["team_bot_a"]


def test_patch_bot_apply_to_existing_rewrites_manifests(client, monkeypatch):
    """apply_to_existing=true iterates every manifest and applies the tier."""
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "notes/", "privacy": "cloud"}],
    })
    _write_manifest(workspace, "diary", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "diary/", "privacy": "cloud"}],
    })

    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/bot", json={
            "bot_id": "team_bot_a",
            "tier": "whole_app_local",
            "apply_to_existing": True,
        })
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["apps_updated"] == 2

    # Re-fetch the overview; both apps should now be whole_app_local.
    overview = c.get("/api/backup/data/overview").get_json()
    apps = {a["id"]: a for a in overview["bots"]["team_bot_a"]["apps"]}
    assert apps["notes"]["current_tier"] == "whole_app_local"
    assert apps["diary"]["current_tier"] == "whole_app_local"


def test_patch_bot_some_data_local_does_not_iterate(client, monkeypatch):
    """``some_data_local`` is operator-authored per-path; bulk-apply is a no-op."""
    c, _, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    _write_manifest(workspace, "notes", {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "notes/", "privacy": "cloud"}],
    })
    with patch("evolve_admin.applications.manifest.applications_dir",
               return_value=workspace / "manifests"):
        resp = c.patch("/api/backup/data/bot", json={
            "bot_id": "team_bot_a",
            "tier": "some_data_local",
            "apply_to_existing": True,
        })
    assert resp.status_code == 200
    assert resp.get_json()["apps_updated"] == 0


def test_overview_surfaces_per_bot_default_tier(client, net_file=None):
    c, net_file, _ = client
    net = json.loads(net_file.read_text())
    net["bots"]["team_bot_a"]["backup_default_tier"] = "all_data_local"
    net_file.write_text(json.dumps(net))

    resp = c.get("/api/backup/data/overview")
    assert resp.status_code == 200
    assert resp.get_json()["bots"]["team_bot_a"]["backup_default_tier"] == "all_data_local"


# ─── PATCH /api/backup/data/pod ────────────────────────────────────────────

def test_patch_pod_writes_default_and_data_paths(client):
    c, net_file, _ = client
    resp = c.patch("/api/backup/data/pod", json={
        "default_for_unclassified": "local",
        "data_paths": [
            {"path": "proposals/", "privacy": "cloud"},
            {"path": "observations/", "privacy": "local"},
        ],
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["default_for_unclassified"] == "local"
    assert len(data["data_paths"]) == 2

    # Persisted to network.json.
    net = json.loads(net_file.read_text())
    assert net["backup"]["default_for_unclassified"] == "local"
    assert net["backup"]["data_paths"][0]["path"] == "proposals/"


def test_patch_pod_empty_default_clears_field(client):
    c, net_file, _ = client
    # Seed an existing default
    net = json.loads(net_file.read_text())
    net["backup"] = {"default_for_unclassified": "local"}
    net_file.write_text(json.dumps(net))
    # Operator clears it
    resp = c.patch("/api/backup/data/pod", json={"default_for_unclassified": ""})
    assert resp.status_code == 200
    assert resp.get_json()["default_for_unclassified"] == ""
    # network.json no longer has the default in the backup block; depending on
    # whether anything else is in backup, the whole block may be removed.
    net = json.loads(net_file.read_text())
    assert "default_for_unclassified" not in net.get("backup", {})


def test_patch_pod_rejects_invalid_privacy(client):
    c, _, _ = client
    resp = c.patch("/api/backup/data/pod", json={
        "data_paths": [{"path": "foo/", "privacy": "🌶"}],
    })
    assert resp.status_code == 400


def test_patch_pod_rejects_invalid_default(client):
    c, _, _ = client
    resp = c.patch("/api/backup/data/pod", json={"default_for_unclassified": "nope"})
    assert resp.status_code == 400


def test_patch_pod_round_trips_through_overview(client):
    """After a pod PATCH, the next GET /overview reflects the change."""
    c, _, _ = client
    c.patch("/api/backup/data/pod", json={
        "default_for_unclassified": "local",
        "data_paths": [{"path": "signals/", "privacy": "cloud"}],
    })
    resp = c.get("/api/backup/data/overview")
    pod = resp.get_json()["pod_wide"]
    assert pod["default_for_unclassified"] == "local"
    assert pod["data_paths"][0]["path"] == "signals/"
