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


# ── promotion shield (AL-1.7 brief §8.3 step 6) ─────────────────────────────


def test_the_operator_can_clear_a_false_never(pod):
    """The route exists because a "never" could be set by mistake and unset by
    nothing — see ``app_promotion.set_promotion_shield``.

    MUTATION CHECKED, run in this session: making the route ignore ``shielded``
    (always shielding) makes this go red.
    """
    _write(pod["manifests"], "morning-brief", {
        "id": "morning-brief", "name": "Morning Brief", "bot_id": pod["bot"],
        "definition_status": "discovered",
        "do_not_offer": True, "do_not_offer_by": "user:promotion_offer",
    })

    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/morning-brief/definition/promotion-shield",
        json={"shielded": False},
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["ok"] is True and body["shielded"] is False and body["changed"] is True
    after = _read(pod["manifests"], "morning-brief")
    assert "do_not_offer" not in after
    assert "do_not_offer_by" not in after


def test_an_operator_can_shield_a_draft_too(pod):
    """Reversible in both directions — the same route with the other value."""
    _write(pod["manifests"], "morning-brief", {
        "id": "morning-brief", "name": "Morning Brief", "bot_id": pod["bot"],
        "definition_status": "discovered",
    })

    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/morning-brief/definition/promotion-shield",
        json={"shielded": True},
    )

    assert res.status_code == 200
    after = _read(pod["manifests"], "morning-brief")
    assert after["do_not_offer"] is True
    assert after["do_not_offer_by"] == "ui:operator"


def test_the_shield_route_is_idempotent(pod):
    """Twice is once, and ``changed`` says which it was — an operator retrying a
    request must not be told they altered something they did not."""
    _write(pod["manifests"], "morning-brief", {
        "id": "morning-brief", "name": "Morning Brief", "bot_id": pod["bot"],
        "definition_status": "discovered", "do_not_offer": True,
    })
    url = f"/api/applications/{pod['bot']}/morning-brief/definition/promotion-shield"

    first = pod["client"].post(url, json={"shielded": False}).get_json()
    second = pod["client"].post(url, json={"shielded": False}).get_json()

    assert first["changed"] is True
    assert second["changed"] is False
    assert "do_not_offer" not in _read(pod["manifests"], "morning-brief")


def test_a_body_without_a_boolean_is_refused(pod):
    """No default. ``shielded`` IS the request, and guessing it means a
    malformed body silently picks one of two opposite outcomes — one of which
    is the one a user cannot undo.

    MUTATION CHECKED: defaulting to ``True`` when the key is absent makes this
    go red.
    """
    _write(pod["manifests"], "morning-brief", {
        "id": "morning-brief", "name": "Morning Brief", "bot_id": pod["bot"],
        "definition_status": "discovered",
    })
    url = f"/api/applications/{pod['bot']}/morning-brief/definition/promotion-shield"

    for body in ({}, {"shielded": "false"}, {"shielded": 0}):
        res = pod["client"].post(url, json=body)
        assert res.status_code == 400, body
    assert "do_not_offer" not in _read(pod["manifests"], "morning-brief")


def test_the_shield_route_404s_on_a_manifest_that_is_not_there(pod):
    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/no-such-app/definition/promotion-shield",
        json={"shielded": False},
    )
    assert res.status_code == 404


def test_the_shield_route_resolves_the_stem_like_its_siblings(pod):
    """§9.7 finding #5 — read and write must land on the same file even when
    the manifest's internal id diverges from its filename stem.

    MUTATION CHECKED: dropping the ``_resolve_manifest_stem`` call makes this
    go red (the route 404s on the internal id).
    """
    _write(pod["manifests"], "i-7f3a", {
        "id": "app_morning_brief", "name": "Morning Brief", "bot_id": pod["bot"],
        "definition_status": "discovered", "do_not_offer": True,
    })

    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/app_morning_brief/definition/promotion-shield",
        json={"shielded": False},
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    assert "do_not_offer" not in _read(pod["manifests"], "i-7f3a")


def test_a_failed_shield_write_is_a_500_not_a_silent_success(pod, monkeypatch):
    """N4 (independent review of #3750) — the route's write-failure branch had
    no test entering it.

    It is the one branch where a wrong answer is actively harmful: an operator
    clearing a false "never" and being told `ok: true` while the shield is still
    on disk would move on, and the draft stays out of the conversational path
    forever — the exact outcome the route exists to end.

    MUTATION CHECKED, run in this session: ignoring `_write_manifest_as_bot`'s
    return value (always answering 200) makes this go red.
    """
    from evolve_admin.web import server as _srv

    _write(pod["manifests"], "morning-brief", {
        "id": "morning-brief", "name": "Morning Brief", "bot_id": pod["bot"],
        "definition_status": "discovered", "do_not_offer": True,
    })
    monkeypatch.setattr(_srv, "_write_manifest_as_bot", lambda *a, **k: False)

    res = pod["client"].post(
        f"/api/applications/{pod['bot']}/morning-brief/definition/promotion-shield",
        json={"shielded": False},
    )

    assert res.status_code == 500
    assert res.get_json()["ok"] is False
    # And the shield is still on disk — the answer and the state agree.
    assert _read(pod["manifests"], "morning-brief")["do_not_offer"] is True
