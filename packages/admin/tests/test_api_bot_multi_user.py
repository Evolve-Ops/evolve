"""Tests for the PATCH /api/bot/multi-user endpoint.

Behind the chip toggle on the Users page (per-bot panel) — flips
``network.json::bots.<id>.multiUser`` in both directions and verifies
the change persists. The endpoint already existed (used by the
Overview tile's chip); the Users-page toggle reuses it as its
single source of truth.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))


from evolve_admin.web.server import create_app  # noqa: E402


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    path = tmp_path / "network.json"
    path.write_text(json.dumps({
        "primary": "team_bot_a",
        "members": ["team_bot_a", "admin_bot"],
        "bots": {
            "team_bot_a":   {"user": "team_bot_a",   "role": "member", "multiUser": True},
            "admin_bot": {"user": "admin_bot", "role": "member", "multiUser": False},
        },
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir()
    return path


@pytest.fixture
def client(network_path: Path):
    app = create_app(network_path)
    app.testing = True
    return app.test_client()


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


class TestPatchMultiUser:
    def test_flip_single_to_multi(self, client, network_path: Path) -> None:
        r = client.patch(
            "/api/bot/multi-user",
            json={"botId": "admin_bot", "multiUser": True},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body == {"ok": True, "botId": "admin_bot", "multiUser": True}
        assert _load(network_path)["bots"]["admin_bot"]["multiUser"] is True

    def test_flip_multi_to_single(self, client, network_path: Path) -> None:
        r = client.patch(
            "/api/bot/multi-user",
            json={"botId": "team_bot_a", "multiUser": False},
        )
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "botId": "team_bot_a", "multiUser": False}
        assert _load(network_path)["bots"]["team_bot_a"]["multiUser"] is False

    def test_round_trip_preserves_other_fields(
        self, client, network_path: Path,
    ) -> None:
        client.patch(
            "/api/bot/multi-user",
            json={"botId": "admin_bot", "multiUser": True},
        )
        client.patch(
            "/api/bot/multi-user",
            json={"botId": "admin_bot", "multiUser": False},
        )
        admin_bot = _load(network_path)["bots"]["admin_bot"]
        assert admin_bot["multiUser"] is False
        # The role/user fields stay untouched — the patch is field-scoped.
        assert admin_bot["user"] == "admin_bot"
        assert admin_bot["role"] == "member"

    def test_missing_bot_id_400(self, client) -> None:
        r = client.patch("/api/bot/multi-user", json={"multiUser": True})
        assert r.status_code == 400
        assert "botId" in r.get_json()["error"]

    def test_missing_multi_user_field_400(self, client) -> None:
        r = client.patch("/api/bot/multi-user", json={"botId": "admin_bot"})
        assert r.status_code == 400
        assert "multiUser" in r.get_json()["error"]
