"""tests/test_bot_routing_legacy_writer_retired.py — POST /api/bot/routing is gone.

The endpoint persisted untranslated ``*Tier`` keys into network.json
``models.routing`` — the POD-WIDE fallback both plugin load seams read
(``tiersFile.routing ?? network.models?.routing``) — and the plugin runtime
refuses a routing block carrying any ``*Tier`` key on sight
(LegacyTierShapeError, #3662), so a single curl to it could have poisoned
routing for every bot without a per-bot evolve-tiers.json routing block.
It had no caller: the SPA's routing card writes per-bot via
``PUT /api/admin/config/<bot>/routing``, which translates tier→role at the
oc_model write boundary.

Locked here:
  - POST /api/bot/routing is refused. The URL rule still exists for the
    read-only GET, so Flask answers 405 Method Not Allowed rather than 404 —
    either way the write path is unreachable.
  - The refused POST leaves network.json byte-identical: no ``models.routing``
    block is minted.
  - GET /api/bot/routing (the reader) still answers 200.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def app(tmp_path):
    """Flask app over a minimal network.json with no models.routing block."""
    from evolve_admin.web.server import create_app

    network = {"bots": {"evolve": {"user": "evolve"}}, "sharedDir": str(tmp_path / "shared")}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    flask_app = create_app(network_path)
    flask_app.config["TESTING"] = True
    return {"app": flask_app, "network_path": network_path}


def test_post_bot_routing_is_retired_and_writes_nothing(app):
    """The legacy tier-shaped body must not be persistable pod-wide."""
    before = app["network_path"].read_bytes()
    with app["app"].test_client() as c:
        resp = c.post(
            "/api/bot/routing",
            json={"enabled": True, "maintenanceTier": "tier3", "ambiguousTier": "tier2"},
        )
    # 405 (rule kept alive by the GET) — 404 would also prove retirement, but
    # pin the actual behavior so a re-added POST handler fails this test.
    assert resp.status_code == 405, (resp.status_code, resp.get_data(as_text=True))

    assert app["network_path"].read_bytes() == before, "refused POST wrote network.json"
    net = json.loads(app["network_path"].read_text())
    assert "routing" not in net.get("models", {})


def test_get_bot_routing_reader_still_answers(app):
    with app["app"].test_client() as c:
        resp = c.get("/api/bot/routing")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "network.json models.routing"
