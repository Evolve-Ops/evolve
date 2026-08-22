"""Tests for the gallery-promotion daemon endpoints (F-P.7.a).

Spec: docs/spec-files-pack-hybrid-2026-06-03.md §8.

The endpoints wrap ``snapshot_engine.snapshot_installed_app`` for
the admin UI's Apps-tab "Promote to gallery" button (F-P.7.b,
follows). Tests assert the request/response envelope shape, error
mapping (engine codes -> HTTP statuses), and that the endpoint
honors the same defaults as the engine.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web.gallery_promote_routes import (  # noqa: E402
    register_gallery_promote_routes,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Flask test client wired up against a fake pod tree.

    The endpoints call into the snapshot_engine, which reads from
    workspace dirs. We monkeypatch ``config.bot_home`` + a fake
    ``load_network`` so the engine sees a controllable pod.
    """
    pod = tmp_path / "Users"
    bot_user = "team-bot-a"
    workspace = pod / bot_user / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts/foo.py").write_text("print('ok')\n")
    os.chmod(workspace / "scripts/foo.py", 0o644)
    (workspace / "manifests/task-manager.json").write_text(json.dumps({
        "id": "task-manager", "name": "Task Manager",
        "bot_id": bot_user,
        "pkg_id": "p-9bfa1c84", "pkg_version": "2026.06.03-1.4",
        "files": [{"path": "scripts/foo.py", "sha256": "x"}],
    }))

    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {bot_user: {"user": bot_user}}},
    )

    app = Flask(__name__)
    register_gallery_promote_routes(app)
    app.testing = True
    return {
        "client": app.test_client(),
        "bot_user": bot_user,
        "workspace": workspace,
    }


# ── Request validation ──────────────────────────────────────────────────────


def test_missing_bot_id_returns_400(client):
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={"pkg_id": "p-9bfa1c84"},
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["ok"] is False
    assert body["error"] == "missing_fields"
    assert "bot_id" in body["missing"]


def test_missing_pkg_id_returns_400(client):
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={"bot_id": client["bot_user"]},
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["ok"] is False
    assert "pkg_id" in body["missing"]


def test_unknown_bot_returns_404(client):
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={"bot_id": "no-such-bot", "pkg_id": "p-9bfa1c84"},
    )
    assert res.status_code == 404
    body = res.get_json()
    assert body["ok"] is False
    assert body["error"].startswith("unknown_bot:")


def test_unknown_pkg_returns_404(client, tmp_path):
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={
            "bot_id": client["bot_user"],
            "pkg_id": "p-doesnotexist",
            "out_dir": str(tmp_path / "out"),
        },
    )
    assert res.status_code == 404
    body = res.get_json()
    assert body["error"].startswith("manifest_not_found:")


# ── Happy path ──────────────────────────────────────────────────────────────


def test_happy_path_returns_engine_envelope_fields(client, tmp_path):
    out_dir = tmp_path / "out"
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={
            "bot_id": client["bot_user"],
            "pkg_id": "p-9bfa1c84",
            "out_dir": str(out_dir),
        },
    )
    assert res.status_code == 200, res.data
    body = res.get_json()
    assert body["ok"] is True
    assert body["error"] == ""
    assert body["out_dir"] == str(out_dir)
    assert body["files_count"] == 1
    assert body["format_version"] == "1.0"
    assert body["snapshot_source_pkg_version"] == "2026.06.03-1.4"
    assert len(body["sha256"]) == 64
    assert isinstance(body["per_file"], list)
    # Per-file entries carry the operator-review fields.
    entry = body["per_file"][0]
    assert "placeholders" in entry
    assert "bare_token_count" in entry


def test_out_dir_defaults_to_daemon_temp_dir(client):
    """When ``out_dir`` is omitted, the endpoint allocates a fresh
    staging dir under tempfile.gettempdir() and returns its path."""
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={
            "bot_id": client["bot_user"],
            "pkg_id": "p-9bfa1c84",
        },
    )
    assert res.status_code == 200, res.data
    body = res.get_json()
    assert body["ok"] is True
    out_dir = Path(body["out_dir"])
    assert out_dir.exists()
    # Under the OS tempdir (likely /tmp or /private/tmp on macOS).
    assert str(out_dir).startswith(tempfile.gettempdir())
    # Prefix carries the pkg_id so operators can spot stale dirs.
    assert "p-9bfa1c84" in out_dir.name


def test_non_empty_out_returns_409_without_force(client, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("prior run")
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={
            "bot_id": client["bot_user"],
            "pkg_id": "p-9bfa1c84",
            "out_dir": str(out_dir),
        },
    )
    assert res.status_code == 409
    body = res.get_json()
    assert body["error"].startswith("out_dir_not_empty:")
    assert body["out_dir"] == str(out_dir)
    # Stale file preserved.
    assert (out_dir / "stale.txt").exists()


def test_force_flag_overwrites_existing(client, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("prior run")
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={
            "bot_id": client["bot_user"],
            "pkg_id": "p-9bfa1c84",
            "out_dir": str(out_dir),
            "force": True,
        },
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert not (out_dir / "stale.txt").exists()


def test_auto_detect_default_is_true(client, tmp_path):
    """Without ``auto_detect`` in body, defaults to True (engine pass
    runs); confirm by checking per_file output isn't all-empty when
    we add bot_user content to the source file."""
    # Inject a workspace-pattern token into the source file so the
    # detector has something to find.
    src = client["workspace"] / "scripts/foo.py"
    src.write_text(
        f"WORKSPACE = '/Users/{client['bot_user']}/.openclaw/workspace'\n"
    )
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={
            "bot_id": client["bot_user"],
            "pkg_id": "p-9bfa1c84",
        },
    )
    body = res.get_json()
    assert body["ok"] is True
    entry = body["per_file"][0]
    assert "workspace" in entry["placeholders"]


def test_auto_detect_false_disables_substitution(client, tmp_path):
    src = client["workspace"] / "scripts/foo.py"
    src.write_text(
        f"WORKSPACE = '/Users/{client['bot_user']}/.openclaw/workspace'\n"
    )
    res = client["client"].post(
        "/api/gallery/promote/snapshot",
        json={
            "bot_id": client["bot_user"],
            "pkg_id": "p-9bfa1c84",
            "auto_detect": False,
        },
    )
    body = res.get_json()
    assert body["ok"] is True
    entry = body["per_file"][0]
    assert entry["placeholders"] == []
