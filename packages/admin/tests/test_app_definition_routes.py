"""Tests for the Defined/Discovered definition routes (spec §9, Bite 4).

These are the thin privileged web wrappers the Bite-4 Apps UI calls:

    POST /api/applications/<bot>/<app>/definition/promote
    POST /api/applications/<bot>/<app>/definition/demote
    POST /api/applications/<bot>/<app>/definition/drift/review

The mutations themselves live in ``coherence_actions`` (promote/demote) and
``drift_classifier`` (drift review); these tests pin the route envelope plus
the two correctness gaps the spec flagged for Bite 4:

  * **filename-stem, not internal id** (§9.7 finding #5) — read AND write must
    land on the SAME file even when the manifest's internal ``id`` diverges
    from its on-disk filename stem (gallery v7-arc-pre apps).
  * **drift review** flips ``reviewed`` (additive + reversible), never deletes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import server as _server  # noqa: E402
from evolve_admin.web.routes_app_definition import (  # noqa: E402
    register_app_definition_routes,
    _resolve_manifest_stem,
)


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    """A fake per-bot manifests tree the real read/write/resolve helpers use."""
    bot = "team-bot-a"
    workspace = tmp_path / "Users" / bot / ".openclaw" / "workspace"
    manifests = workspace / "manifests"
    manifests.mkdir(parents=True)

    # resolve_bot_paths is what _bot_manifests_dir / _list_manifests_as_bot use.
    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(workspace)},
    )
    monkeypatch.setattr(_server, "_resolve_bot_user", lambda bid, *a, **kw: bid)

    app = Flask(__name__)
    register_app_definition_routes(app)
    app.testing = True
    return {
        "bot": bot,
        "manifests": manifests,
        "client": app.test_client(),
    }


def _write(manifests: Path, stem: str, data: dict) -> None:
    (manifests / f"{stem}.json").write_text(json.dumps(data))


def _read(manifests: Path, stem: str) -> dict:
    return json.loads((manifests / f"{stem}.json").read_text())


# ── promote / demote round-trip ─────────────────────────────────────────────


def test_promote_then_demote_round_trip(pod):
    _write(pod["manifests"], "task-manager", {
        "id": "task-manager", "name": "Task Manager", "bot_id": pod["bot"],
        "definition_status": "discovered",
    })

    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/task-manager/definition/promote")
    assert res.status_code == 200, res.data
    assert res.get_json()["ok"] is True
    assert res.get_json()["definition_status"] == "defined"
    assert _read(pod["manifests"], "task-manager")["definition_status"] == "defined"

    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/task-manager/definition/demote")
    assert res.status_code == 200, res.data
    assert res.get_json()["definition_status"] == "discovered"
    assert _read(pod["manifests"], "task-manager")["definition_status"] == "discovered"


def test_promote_is_idempotent(pod):
    _write(pod["manifests"], "x", {"id": "x", "name": "X", "definition_status": "defined"})
    res = pod["client"].post(f"/api/applications/{pod['bot']}/x/definition/promote")
    assert res.status_code == 200
    assert res.get_json()["was_already_defined"] is True
    assert _read(pod["manifests"], "x")["definition_status"] == "defined"


def test_promote_unknown_app_404(pod):
    res = pod["client"].post(f"/api/applications/{pod['bot']}/nope/definition/promote")
    assert res.status_code == 404
    assert res.get_json()["ok"] is False


# ── filename-stem-not-id (§9.7 finding #5) ──────────────────────────────────


def test_promote_by_internal_id_writes_back_to_filename_stem(pod):
    """The file lives at the display slug; the internal id differs. Promoting by
    the INTERNAL ID must mutate the EXISTING file, not mint a new one keyed by
    the id (the bug finding #5 warns about)."""
    _write(pod["manifests"], "atlas-article-capture", {
        "id": "app_atlas_article_capture", "name": "Atlas",
        "definition_status": "discovered",
    })

    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/app_atlas_article_capture/definition/promote")
    assert res.status_code == 200, res.data

    # The real file flipped...
    assert _read(pod["manifests"], "atlas-article-capture")["definition_status"] == "defined"
    # ...and NO stray <id>.json was created.
    assert not (pod["manifests"] / "app_atlas_article_capture.json").exists()
    files = sorted(p.name for p in pod["manifests"].glob("*.json"))
    assert files == ["atlas-article-capture.json"]


def test_resolve_manifest_stem_handles_id_and_stem(pod):
    _write(pod["manifests"], "atlas-article-capture", {
        "id": "app_atlas_article_capture", "name": "Atlas",
    })
    # Internal id resolves to the filename stem.
    assert _resolve_manifest_stem(
        pod["bot"], "app_atlas_article_capture") == "atlas-article-capture"
    # The filename stem resolves to itself (fast path).
    assert _resolve_manifest_stem(
        pod["bot"], "atlas-article-capture") == "atlas-article-capture"
    # An unknown id is returned unchanged (caller's read then 404s).
    assert _resolve_manifest_stem(pod["bot"], "ghost") == "ghost"


# ── drift review ────────────────────────────────────────────────────────────


def _defined_with_drift():
    return {
        "id": "rep", "name": "Reporter", "definition_status": "defined",
        "drift_log": [
            {"ts": "2026-06-20T00:00:00Z", "kind": "add", "target_type": "cron",
             "target": "scripts/x.py", "significance": "major", "summary": "added cron",
             "reviewed": False, "source": "scanner_drift", "classifier": "deterministic"},
            {"ts": "2026-06-21T00:00:00Z", "kind": "remove", "target_type": "file",
             "target": "scripts/y.py", "significance": "major", "summary": "removed script",
             "reviewed": False, "source": "scanner_drift", "classifier": "deterministic"},
        ],
    }


def test_drift_review_marks_all_reviewed(pod):
    _write(pod["manifests"], "rep", _defined_with_drift())
    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/rep/definition/drift/review",
        json={"reviewed": True})
    assert res.status_code == 200, res.data
    body = res.get_json()
    assert body["ok"] is True
    assert body["updated"] == 2
    assert body["unreviewed_count"] == 0
    on_disk = _read(pod["manifests"], "rep")["drift_log"]
    assert all(e["reviewed"] is True for e in on_disk)


def test_drift_review_targets_subset_by_ts(pod):
    _write(pod["manifests"], "rep", _defined_with_drift())
    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/rep/definition/drift/review",
        json={"targets": ["2026-06-20T00:00:00Z"]})
    assert res.status_code == 200, res.data
    assert res.get_json()["updated"] == 1
    on_disk = {e["ts"]: e["reviewed"] for e in _read(pod["manifests"], "rep")["drift_log"]}
    assert on_disk["2026-06-20T00:00:00Z"] is True
    assert on_disk["2026-06-21T00:00:00Z"] is False


def test_drift_review_is_reversible(pod):
    m = _defined_with_drift()
    for e in m["drift_log"]:
        e["reviewed"] = True
    _write(pod["manifests"], "rep", m)
    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/rep/definition/drift/review",
        json={"reviewed": False})
    assert res.status_code == 200
    assert res.get_json()["updated"] == 2
    assert res.get_json()["unreviewed_count"] == 2


def test_drift_review_rejects_bad_targets(pod):
    _write(pod["manifests"], "rep", _defined_with_drift())
    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/rep/definition/drift/review",
        json={"targets": "not-a-list"})
    assert res.status_code == 400
