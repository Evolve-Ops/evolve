"""End-to-end tests for the per-bot Setup checklist Flask routes.

Covers ``routes_setup_checklist``::

  GET  /api/admin/bots/<bot>/setup-checklist
  POST /api/admin/bots/<bot>/setup-checklist/items/<item_id>
  POST /api/admin/bots/<bot>/setup-checklist/suppress
  POST /api/admin/bots/<bot>/setup-checklist/reset

The detector implementations are unit-tested separately in
``test_setup_checklist_detectors.py``; these tests focus on:

  - HTTP error shapes (404 unknown bot, 400 bad state)
  - Round-trip via Flask test client: state writes hit network.json
  - GET payload shape (items list ordered by registry, counter, tile_chip)
  - GET auto-evaluates and persists first-time seeds
  - Dismiss → suppress → reset interaction with the visible-chip rules
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web import routes_setup_checklist as rsc  # noqa: E402
from evolve_admin import setup_checklist as sc  # noqa: E402


def _seed_network(tmp_path: Path, **bot_overrides) -> Path:
    """Write a minimal network.json the routes can read+save."""
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            "team_bot_a": {"role": "member", "port": 19002, **bot_overrides},
        },
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(base, indent=2))
    return p


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    network_path = _seed_network(tmp_path)
    # Redirect bot_home so the openclaw.json loader points at tmp_path.
    monkeypatch.setattr(rsc, "bot_home", lambda bot, net: tmp_path / "Users" / bot)
    # The pairing detector also calls bot_home through config.bot_home —
    # patch at the source.
    monkeypatch.setattr("evolve_admin.config.bot_home",
                        lambda bot, net=None: tmp_path / "Users" / bot)
    # The github detector reads {workspace}/evolve-backup/state.json via
    # get_bot_workspace — redirect that too so the detector can find a
    # test-supplied state file.
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot, user=None: tmp_path / "Users" / bot / "workspace",
    )
    a = Flask(__name__)
    rsc.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a, network_path


# ── GET ───────────────────────────────────────────────────────────────────


def test_get_unknown_bot_returns_404(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.get("/api/admin/bots/nonesuch/setup-checklist")
        assert resp.status_code == 404


def test_get_fresh_bot_returns_all_items_pending(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.get("/api/admin/bots/team_bot_a/setup-checklist")
    data = resp.get_json()
    assert data["bot_id"] == "team_bot_a"
    assert {item.id for item in sc.CHECKLIST_ITEMS} == {row["id"] for row in data["items"]}
    assert all(row["state"] == "pending" for row in data["items"])
    assert data["counter"] == {"done": 0, "total": len(sc.CHECKLIST_ITEMS)}
    assert data["tile_chip"]["visible"] is True
    assert data["tile_chip"]["in_actions_menu"] is False
    assert data["tile_chip"]["suppressed_at"] is None


def test_get_items_are_ordered_per_registry(app):
    a, _ = app
    with a.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/setup-checklist").get_json()
    item_ids = [row["id"] for row in data["items"]]
    expected = [item.id for item in sc.CHECKLIST_ITEMS]
    assert item_ids == expected


def test_get_persists_first_time_seed_to_network_json(app, tmp_path):
    a, network_path = app
    # Before GET: no setup_checklist on the bot
    before = json.loads(network_path.read_text())
    assert "setup_checklist" not in before["bots"]["team_bot_a"]
    with a.test_client() as c:
        c.get("/api/admin/bots/team_bot_a/setup-checklist")
    after = json.loads(network_path.read_text())
    cl = after["bots"]["team_bot_a"]["setup_checklist"]
    assert cl["tile_chip_suppressed_at"] is None
    assert set(cl["items"].keys()) == {item.id for item in sc.CHECKLIST_ITEMS}


def test_get_marks_github_done_when_backup_url_set_and_recent_push(app, tmp_path):
    """Smoke test the detector wiring through the full route. The github
    detector requires both pieces — backupRepoUrl set AND a recent
    last_success_at — so we have to drop both fixtures."""
    from datetime import datetime, timezone, timedelta
    a, network_path = app
    net = json.loads(network_path.read_text())
    net["bots"]["team_bot_a"]["backupRepoUrl"] = "git@github.com:ops/team_bot_a-backup"
    network_path.write_text(json.dumps(net))
    state_dir = tmp_path / "Users" / "team_bot_a" / "workspace" / "evolve-backup"
    state_dir.mkdir(parents=True)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (state_dir / "state.json").write_text(json.dumps({"last_success_at": recent}))
    with a.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/setup-checklist").get_json()
    github = next(r for r in data["items"] if r["id"] == "github")
    assert github["state"] == "done"
    assert data["counter"]["done"] == 1


def test_get_marks_github_pending_when_url_set_but_no_recent_push(app, tmp_path):
    """The strengthened detector: URL alone isn't enough — operator might
    have configured the field and then the daemon never ran. Stay pending
    so the checklist accurately reflects "not yet working.\""""
    a, network_path = app
    net = json.loads(network_path.read_text())
    net["bots"]["team_bot_a"]["backupRepoUrl"] = "git@github.com:ops/team_bot_a-backup"
    network_path.write_text(json.dumps(net))
    # No state.json fixture written
    with a.test_client() as c:
        data = c.get("/api/admin/bots/team_bot_a/setup-checklist").get_json()
    github = next(r for r in data["items"] if r["id"] == "github")
    assert github["state"] == "pending"


# ── POST item state ───────────────────────────────────────────────────────


def test_post_item_rejects_invalid_state(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/team_bot_a/setup-checklist/items/pairing",
                      json={"state": "done"})  # 'done' is auto-only
        assert resp.status_code == 400


def test_post_item_rejects_unknown_item(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/team_bot_a/setup-checklist/items/not_an_item",
                      json={"state": "dismissed"})
        assert resp.status_code == 404


def test_post_item_dismiss_persists_and_returns_updated_payload(app, tmp_path):
    a, network_path = app
    with a.test_client() as c:
        c.get("/api/admin/bots/team_bot_a/setup-checklist")  # seed
        resp = c.post("/api/admin/bots/team_bot_a/setup-checklist/items/pairing",
                      json={"state": "dismissed"})
    data = resp.get_json()
    pairing = next(r for r in data["items"] if r["id"] == "pairing")
    assert pairing["state"] == "dismissed"
    assert pairing["auto_detected"] is False
    # Dismissed counts toward done in the counter (same as the chip rule).
    assert data["counter"]["done"] == 1
    # Persisted on disk
    net = json.loads(network_path.read_text())
    stored = net["bots"]["team_bot_a"]["setup_checklist"]["items"]["pairing"]
    assert stored["state"] == "dismissed"


def test_post_item_un_dismiss_flips_back_to_pending(app):
    a, _ = app
    with a.test_client() as c:
        c.post("/api/admin/bots/team_bot_a/setup-checklist/items/pairing",
               json={"state": "dismissed"})
        resp = c.post("/api/admin/bots/team_bot_a/setup-checklist/items/pairing",
                      json={"state": "pending"})
    data = resp.get_json()
    pairing = next(r for r in data["items"] if r["id"] == "pairing")
    # detector returns False (no allowFrom file) → state lands on pending.
    assert pairing["state"] == "pending"


# ── POST suppress / reset ─────────────────────────────────────────────────


def test_post_suppress_hides_chip(app):
    a, _ = app
    with a.test_client() as c:
        c.get("/api/admin/bots/team_bot_a/setup-checklist")  # seed first
        resp = c.post("/api/admin/bots/team_bot_a/setup-checklist/suppress")
    data = resp.get_json()
    assert data["tile_chip"]["visible"] is False
    assert data["tile_chip"]["suppressed_at"] is not None
    # Suppressed-but-pending → surface in Actions menu
    assert data["tile_chip"]["in_actions_menu"] is True


def test_post_reset_brings_chip_back(app):
    a, _ = app
    with a.test_client() as c:
        c.get("/api/admin/bots/team_bot_a/setup-checklist")
        c.post("/api/admin/bots/team_bot_a/setup-checklist/suppress")
        resp = c.post("/api/admin/bots/team_bot_a/setup-checklist/reset")
    data = resp.get_json()
    assert data["tile_chip"]["visible"] is True
    assert data["tile_chip"]["suppressed_at"] is None
    assert data["tile_chip"]["in_actions_menu"] is False


def test_post_suppress_unknown_bot_404s(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/nonesuch/setup-checklist/suppress")
        assert resp.status_code == 404


# ── Built-in (reserved) bot self-heal ─────────────────────────────────────────


def _reserved_app(tmp_path: Path, monkeypatch):
    """Register the routes against a network whose only bot is the built-in
    assistant ``evo`` (no purpose declared yet)."""
    monkeypatch.setattr(rsc, "bot_home", lambda bot, net: tmp_path / "Users" / bot)
    monkeypatch.setattr("evolve_admin.config.bot_home",
                        lambda bot, net=None: tmp_path / "Users" / bot)
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"evo": {"role": "primary", "port": 19000}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(base, indent=2))
    a = Flask(__name__)
    rsc.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a, network_path


def test_get_reserved_bot_self_heals_purpose_and_shows_six_items(tmp_path, monkeypatch):
    """A built-in assistant (evo): the GET seeds its default purpose (so the
    purpose row reads Done) and returns only the 6 applicable items — the
    three member-only items are absent and the counter total is 6."""
    a, network_path = _reserved_app(tmp_path, monkeypatch)
    # Precondition: evo has no purpose yet.
    assert "purpose" not in json.loads(network_path.read_text())["bots"]["evo"]

    with a.test_client() as c:
        data = c.get("/api/admin/bots/evo/setup-checklist").get_json()

    ids = {row["id"] for row in data["items"]}
    assert ids == {
        "purpose", "pairing", "search", "ai_optim_tiers", "cost_profile", "spend_cap",
    }
    for absent in ("gallery_app", "github", "secondary_llm"):
        assert absent not in ids
    assert data["counter"]["total"] == 6
    purpose_row = next(r for r in data["items"] if r["id"] == "purpose")
    assert purpose_row["state"] == "done"

    # The self-heal persisted evo's default purpose into network.json.
    after = json.loads(network_path.read_text())
    seeded = after["bots"]["evo"]["purpose"]
    assert seeded["archetype"] == "custom"
    assert seeded["mission"]


def test_get_reserved_bot_does_not_overwrite_existing_purpose(tmp_path, monkeypatch):
    """An operator-edited purpose on evo is preserved by the read-path seed."""
    a, network_path = _reserved_app(tmp_path, monkeypatch)
    net = json.loads(network_path.read_text())
    net["bots"]["evo"]["purpose"] = {
        "archetype": "custom",
        "mission": "Operator-written mission.",
        "captured": "declared",
        "confidence": 1.0,
    }
    network_path.write_text(json.dumps(net))

    with a.test_client() as c:
        c.get("/api/admin/bots/evo/setup-checklist")

    after = json.loads(network_path.read_text())
    assert after["bots"]["evo"]["purpose"]["mission"] == "Operator-written mission."
