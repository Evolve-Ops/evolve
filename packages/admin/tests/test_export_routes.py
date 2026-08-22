"""tests/test_export_routes.py — Stage 0e operator-review HTTP surface.

Spec: docs/spec-scanned-export-2026-06-02.md section 3.5.

Covers the four routes registered by
``evolve_admin.web.export_routes.register_export_routes``:

  * GET  /api/export/candidates
  * POST /api/export/draft
  * POST /api/export/publish
  * GET  /export-review (HTML page)

The build_export_draft pipeline (Stages 0a-0d) is mocked here — its
behavior is tested exhaustively in test_export_engine_stage_0*.py.
This test file is scoped to the route layer: input validation,
candidate enumeration, scanner-source detection, slug-safety
defence-in-depth, and atomic publish + collision handling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin.web.export_routes import register_export_routes  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _scanned_manifest(**overrides) -> dict:
    """Minimal scanner-shape manifest — no pkg_id, no build_spec."""
    base = {
        "id": "i-9c16b1c7",
        "name": "Unified Task System",
        "display_name": "Unified Task System",
        "description": "Persistent task management.",
        "files": [
            {"path": "scripts/tasks.py", "role": "build_artifact"},
            {"path": "tasks.json", "role": "data_file"},
        ],
        "identity": {"purpose": "Track todos."},
        "updated_at": "2026-06-02T12:00:00Z",
    }
    base.update(overrides)
    return base


def _forged_manifest(**overrides) -> dict:
    """A forged-shape manifest — pkg_id + build_spec set. Should NOT
    appear in the candidates list."""
    return _scanned_manifest(
        pkg_id="p-abcd1234",
        build_spec="# Build Spec\n\nAlready forged.",
        **overrides,
    )


@pytest.fixture
def pod_layout(tmp_path: Path):
    """Build a fake pod layout: a network.json with two bots, each
    with a workspace + manifests directory containing a mix of
    scanned and forged manifests + some noise files."""
    pod = tmp_path / "pod"
    pod.mkdir()

    # network.json — two bots: team-bot-a and team-bot-c.
    net = pod / "network.json"
    net.write_text(json.dumps({
        "bots": {
            "team-bot-a": {"user": "team-bot-a"},
            "team-bot-c": {"user": "team-bot-c"},
        },
    }))

    # team-bot-a workspace: 2 scanned + 1 forged + 1 noise file
    a_manifests = pod / "team-bot-a" / ".openclaw" / "workspace" / "manifests"
    a_manifests.mkdir(parents=True)
    (a_manifests / "i-aaa11111.json").write_text(json.dumps(_scanned_manifest(
        id="i-aaa11111", display_name="Task System",
    )))
    (a_manifests / "i-bbb22222.json").write_text(json.dumps(_scanned_manifest(
        id="i-bbb22222", display_name="Journal Logging",
    )))
    (a_manifests / "i-ccc33333.json").write_text(json.dumps(_forged_manifest(
        id="i-ccc33333", display_name="Already Forged",
    )))
    (a_manifests / "ignored.json").write_text("{}")
    (a_manifests / ".DS_Store").write_text("")

    # team-bot-c workspace: 1 scanned
    c_manifests = pod / "team-bot-c" / ".openclaw" / "workspace" / "manifests"
    c_manifests.mkdir(parents=True)
    (c_manifests / "i-ddd44444.json").write_text(json.dumps(_scanned_manifest(
        id="i-ddd44444", display_name="Pending Todos",
    )))

    # team-bot-b in network.json with NO manifests dir (corner case).
    # Add team-bot-b without creating its workspace.
    net.write_text(json.dumps({
        "bots": {
            "team-bot-a": {"user": "team-bot-a"},
            "team-bot-c": {"user": "team-bot-c"},
            "team-bot-b": {"user": "team-bot-b"},
        },
    }))

    return {
        "pod": pod,
        "net": net,
        "gallery": pod / "gallery",
    }


@pytest.fixture
def client(pod_layout, monkeypatch):
    """A Flask test client. ``bot_home`` is patched to resolve into the
    fixture tree rather than the real ``/Users/`` directories."""
    pod = pod_layout["pod"]

    def fake_bot_home(bot_id, network=None):
        return pod / bot_id

    monkeypatch.setattr(
        "evolve_admin.config.bot_home", fake_bot_home,
    )

    app = Flask(__name__)
    register_export_routes(app, pod_layout["net"], pod_layout["gallery"])
    return app.test_client()


# ── GET /api/export/candidates ───────────────────────────────────────────────


def test_candidates_lists_only_scanner_sources(client):
    """Forged manifests (have pkg_id + build_spec) MUST NOT appear."""
    res = client.get("/api/export/candidates")
    assert res.status_code == 200
    body = res.get_json()
    assert body["count"] == 3
    ids = {(c["bot_id"], c["manifest_id"]) for c in body["candidates"]}
    assert ids == {
        ("team-bot-a", "i-aaa11111"),
        ("team-bot-a", "i-bbb22222"),
        ("team-bot-c", "i-ddd44444"),
    }


def test_candidates_skips_non_scanner_filename_pattern(client):
    """Files like ``ignored.json`` or ``.DS_Store`` that don't match
    ``i-[0-9a-f]{8}.json`` must be left alone."""
    res = client.get("/api/export/candidates")
    body = res.get_json()
    for c in body["candidates"]:
        assert c["manifest_id"].startswith("i-")


def test_candidates_tolerates_missing_workspace_dir(client):
    """A bot listed in network.json but with no manifests/ dir should
    not blow up the enumeration."""
    res = client.get("/api/export/candidates")
    assert res.status_code == 200
    body = res.get_json()
    # team-bot-b has no manifests dir; total stays at 3.
    assert body["count"] == 3


def test_candidates_summary_fields(client):
    res = client.get("/api/export/candidates")
    body = res.get_json()
    c = next(c for c in body["candidates"] if c["manifest_id"] == "i-aaa11111")
    assert c["display_name"] == "Task System"
    assert "Persistent task" in c["description"]
    assert c["file_count"] == 2
    assert c["bot_id"] == "team-bot-a"
    assert c["updated_at"] == "2026-06-02T12:00:00Z"


def test_candidates_sorted_for_stable_ui_order(client):
    res = client.get("/api/export/candidates")
    body = res.get_json()
    bots = [c["bot_id"] for c in body["candidates"]]
    assert bots == sorted(bots)  # team-bot-a entries before team-bot-c


# ── POST /api/export/draft ───────────────────────────────────────────────────


def test_draft_requires_bot_id_and_manifest_id(client):
    res = client.post("/api/export/draft", json={})
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "missing_fields"
    assert set(body["missing"]) == {"bot_id", "manifest_id"}


def test_draft_returns_404_for_unknown_manifest(client):
    res = client.post("/api/export/draft", json={
        "bot_id": "team-bot-a",
        "manifest_id": "i-deadbeef",
    })
    assert res.status_code == 404
    assert res.get_json()["error"] == "manifest_not_found"


def test_draft_returns_409_for_already_forged_manifest(client):
    """Operator can't accidentally re-export a manifest that already
    has pkg_id + build_spec — they should use the improvement flow."""
    res = client.post("/api/export/draft", json={
        "bot_id": "team-bot-a",
        "manifest_id": "i-ccc33333",
    })
    assert res.status_code == 409
    assert res.get_json()["error"] == "not_a_scanner_source"


def test_draft_runs_pipeline_and_returns_draft_body(client):
    """Happy path: build_export_draft gets called with the right args
    and the returned dict surfaces in the response."""
    fake_draft = {
        "pkg_id": "p-deadbeef",
        "pkg_version": "2026.06.03-1.0",
        "build_spec": "# Drafted",
        "export_stage": "0d",
        "round_trip": {"verdict": "good"},
        "export_meta": {"deriver_model": "x"},
    }
    with patch(
        "evolve_admin.applications.export_engine.build_export_draft",
        return_value=fake_draft,
    ) as mock_build:
        res = client.post("/api/export/draft", json={
            "bot_id": "team-bot-a",
            "manifest_id": "i-aaa11111",
            "strip_source_specific": True,
        })
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["draft"] == fake_draft
    assert "team-bot-a" in body["manifest_path"]

    # The orchestrator received the right scanner manifest.
    args, kwargs = mock_build.call_args
    assert args[0] == "team-bot-a"
    assert args[1]["id"] == "i-aaa11111"
    assert kwargs["strip_source_specific"] is True


def test_draft_502_on_pipeline_value_error(client):
    """``ValueError`` from the pipeline (e.g. missing api_key) bubbles
    to a 502 with the underlying detail."""
    with patch(
        "evolve_admin.applications.export_engine.build_export_draft",
        side_effect=ValueError("ANTHROPIC_API_KEY not set"),
    ):
        res = client.post("/api/export/draft", json={
            "bot_id": "team-bot-a",
            "manifest_id": "i-aaa11111",
        })
    assert res.status_code == 502
    body = res.get_json()
    assert body["error"] == "draft_build_failed"
    assert "ANTHROPIC_API_KEY" in body["detail"]


def test_draft_500_on_unexpected_crash(client):
    """Any other exception lands as a 500 — surfaces enough detail for
    the operator to file a bug."""
    with patch(
        "evolve_admin.applications.export_engine.build_export_draft",
        side_effect=RuntimeError("LLM returned empty"),
    ):
        res = client.post("/api/export/draft", json={
            "bot_id": "team-bot-a",
            "manifest_id": "i-aaa11111",
        })
    assert res.status_code == 500
    assert res.get_json()["error"] == "draft_build_crashed"


def test_draft_skip_round_trip_threaded_through(client):
    """``skip_round_trip=True`` in the body reaches the pipeline."""
    captured: dict = {}

    def fake_build(*args, **kwargs):
        captured.update(kwargs)
        return {
            "pkg_id": "p-aaaaaaaa",
            "build_spec": "# Drafted",
            "export_stage": "0c",
        }

    with patch(
        "evolve_admin.applications.export_engine.build_export_draft",
        side_effect=fake_build,
    ):
        res = client.post("/api/export/draft", json={
            "bot_id": "team-bot-a",
            "manifest_id": "i-aaa11111",
            "skip_round_trip": True,
        })
    assert res.status_code == 200
    assert captured["skip_round_trip"] is True


# ── POST /api/export/publish ─────────────────────────────────────────────────


def _valid_draft() -> dict:
    return {
        "pkg_id": "p-deadbeef",
        "pkg_version": "2026.06.03-1.0",
        "build_spec": "# Build Spec\n\nbody",
        "status": "draft",
        "export_stage": "0d",
    }


def test_publish_requires_draft_and_slug(client):
    res = client.post("/api/export/publish", json={})
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "missing_fields"
    assert set(body["missing"]) == {"draft", "slug"}


def test_publish_rejects_invalid_slug(client):
    """Slugs with separators / uppercase / punctuation must be rejected
    before any filesystem operation."""
    for bad in (
        "../escape", "Foo", "foo/bar", "foo bar", ".hidden",
        "trailing.", "x" * 200,
    ):
        res = client.post("/api/export/publish", json={
            "draft": _valid_draft(), "slug": bad,
        })
        assert res.status_code == 400, f"expected 400 for slug={bad!r}"
        assert res.get_json()["error"] == "invalid_slug"


def test_publish_rejects_missing_or_invalid_pkg_id(client):
    res = client.post("/api/export/publish", json={
        "draft": {"build_spec": "# x", "pkg_id": ""},
        "slug": "task-manager",
    })
    assert res.status_code == 400
    assert res.get_json()["error"] == "missing_or_invalid_pkg_id"

    res = client.post("/api/export/publish", json={
        "draft": {"build_spec": "# x", "pkg_id": "not-a-pkg-id"},
        "slug": "task-manager",
    })
    assert res.status_code == 400
    assert res.get_json()["error"] == "missing_or_invalid_pkg_id"


def test_publish_rejects_empty_build_spec(client):
    res = client.post("/api/export/publish", json={
        "draft": {"pkg_id": "p-deadbeef", "build_spec": ""},
        "slug": "task-manager",
    })
    assert res.status_code == 400
    assert res.get_json()["error"] == "missing_build_spec"


def test_publish_writes_to_resolved_gallery_path(client, pod_layout):
    res = client.post("/api/export/publish", json={
        "draft": _valid_draft(), "slug": "task-manager",
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["path"] == "gallery/task-manager/p-deadbeef.json"
    assert body["pkg_id"] == "p-deadbeef"

    # File actually landed.
    target = pod_layout["gallery"] / "task-manager" / "p-deadbeef.json"
    assert target.is_file()
    written = json.loads(target.read_text())
    assert written["pkg_id"] == "p-deadbeef"
    # Status flipped from draft -> active on publish.
    assert written["status"] == "active"


def test_publish_refuses_to_overwrite_existing_file(client, pod_layout):
    target = pod_layout["gallery"] / "task-manager" / "p-deadbeef.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"pkg_id": "p-deadbeef", "version": "old"}))

    res = client.post("/api/export/publish", json={
        "draft": _valid_draft(), "slug": "task-manager",
    })
    assert res.status_code == 409
    body = res.get_json()
    assert body["error"] == "target_exists"
    # Untouched.
    assert json.loads(target.read_text()) == {"pkg_id": "p-deadbeef", "version": "old"}


def test_publish_force_overwrites_existing_file(client, pod_layout):
    target = pod_layout["gallery"] / "task-manager" / "p-deadbeef.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"pkg_id": "p-deadbeef", "version": "old"}))

    res = client.post("/api/export/publish", json={
        "draft": _valid_draft(), "slug": "task-manager", "force": True,
    })
    assert res.status_code == 200
    written = json.loads(target.read_text())
    assert written["pkg_version"] == "2026.06.03-1.0"  # the draft's value


def test_publish_atomic_write_creates_parent_directory(client, pod_layout):
    """gallery/<slug>/ may not exist yet — first publish creates it."""
    assert not (pod_layout["gallery"] / "brand-new").exists()
    res = client.post("/api/export/publish", json={
        "draft": _valid_draft(), "slug": "brand-new",
    })
    assert res.status_code == 200
    assert (pod_layout["gallery"] / "brand-new" / "p-deadbeef.json").is_file()


# ── GET /export-review (HTML page) ───────────────────────────────────────────


def test_export_review_page_returns_html(client):
    res = client.get("/export-review")
    assert res.status_code == 200
    assert "text/html" in res.headers["Content-Type"]
    body = res.get_data(as_text=True)
    assert "Scanned Export Review" in body
    # Page references the JSON endpoints it consumes.
    assert "/api/export/candidates" in body
    assert "/api/export/draft" in body
    assert "/api/export/publish" in body
