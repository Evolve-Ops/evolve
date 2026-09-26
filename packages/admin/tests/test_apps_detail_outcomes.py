"""tests/test_apps_detail_outcomes.py — the Outcomes region on app detail
(design-application-platform-2026-09-22.md §3.2/§5, D-AP6).

Same fixture shape as test_apps_pod_routes.py (one bot, a manifest, a
rollup file written the way the real writer shapes it) — extended with an
``outcome-by-app.json`` so ``GET /api/apps/<app_id>`` exercises the actual
route wiring (``routes_apps._load_outcomes`` -> ``pod_apps.build_app_detail``
-> ``app_outcomes.get_app_outcomes``), not just the reader in isolation
(that's ``test_app_outcomes.py``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin.web import server as _server  # noqa: E402
from evolve_admin.web.routes_apps import register_apps_routes  # noqa: E402

BOT = "team-bot-a"


def _manifest(app_id: str, name: str) -> dict:
    return {
        "id": app_id, "app_id": app_id, "name": name,
        "definition_status": "defined",
        "identity": {"purpose": f"{name}. Second sentence."},
        "schema_version": 30,
    }


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    ws = tmp_path / "Users" / BOT / ".openclaw" / "workspace"
    (ws / "manifests").mkdir(parents=True)
    (ws / "manifests" / "assistant.json").write_text(
        json.dumps(_manifest("assistant", "Assistant")))

    (shared / "network.json").write_text(json.dumps({
        "sharedDir": str(shared), "members": [BOT],
    }))

    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(ws)},
    )
    monkeypatch.setattr(_server, "_resolve_bot_user", lambda bid, *a, **kw: bid)

    app = Flask(__name__)
    register_apps_routes(app, shared / "network.json")
    app.testing = True
    return {"client": app.test_client(), "shared": shared}


def _detail(pod, app_id: str) -> dict:
    res = pod["client"].get(f"/api/apps/{app_id}")
    assert res.status_code == 200, res.data
    return res.get_json()


def test_an_app_named_for_a_tracker_application_shows_its_outcomes(pod):
    (pod["shared"] / BOT).mkdir(parents=True, exist_ok=True)
    (pod["shared"] / BOT / "outcome-by-app.json").write_text(json.dumps({
        "schema_version": 1, "bot_id": BOT,
        "apps": {"assistant": {
            "windows": {
                "d7": {"moved_by_bot": {"count": 2, "card_ids": ["p1", "p2"],
                                         "truncated": False}},
            },
            "users": {}, "daily": {},
            "backlog": {"open_cards": 3, "backlog_age_p50_days": 4.0,
                        "backlog_age_max_days": 9.0, "blocked_over_3_days": 0},
        }},
    }))

    detail = _detail(pod, "assistant")
    assert detail["ok"] is True
    outcomes = detail["outcomes"]
    assert outcomes["measured"] is True
    assert outcomes["total"]["moved_by_bot"]["count"] == 2
    assert outcomes["backlog"]["open_cards"] == 3


def test_an_app_with_no_tracker_rollup_reads_not_measured_not_zero(pod):
    detail = _detail(pod, "assistant")
    assert detail["ok"] is True
    assert detail["outcomes"]["measured"] is False
    assert detail["outcomes"]["app_id"] == "assistant"
