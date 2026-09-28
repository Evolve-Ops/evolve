"""tests/test_user_tier_prefs_endpoint.py — per-user tier defaults read surface.

G5 of the spec-user-tier-control 2026-05-26 spec's 2026-08-03 addendum: the
Users page needs a READ surface over ``{sharedDir}/{botId}/user-tier-prefs.json``
(written by the ``evo tier-default`` handler) so a pod admin can see each
user's standing tier default. Registered by
``evolve_admin.web.routes_bot_users.register_routes`` as
``GET /api/admin/bots/<bot_id>/users/tier-prefs``.

Pinned properties:

  * Happy path — entries render as sorted rows with ``user_key``,
    lowercased ``default_role``, ``updated_at``, and (for ``ext:`` keys)
    the parsed ``channel`` / ``external_id`` join fields.
  * Legacy ``defaultTier`` entries (pre model-role migration) are honoured.
  * ``pod:`` keys pass through without channel fields.
  * Missing file → empty ``users`` list, HTTP 200 (the common case — most
    pods have no per-user prefs yet).
  * Malformed file (bad JSON / wrong shape / junk entries) → empty or
    filtered ``users``, HTTP 200, never a 500.
  * Unknown bot → 404.

Placeholder bot/user names only; no real identities.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin.web.routes_bot_users import register_routes  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network = {
        "sharedDir": str(shared),
        "bots": {
            "a_bot": {"role": "member", "user": "a_bot"},
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_routes(app, network_path)
    return {"client": app.test_client(), "shared": shared}


def _write_prefs(shared: Path, bot_id: str, payload) -> Path:
    p = shared / bot_id / "user-tier-prefs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


_URL = "/api/admin/bots/a_bot/users/tier-prefs"


# ── Happy path ──────────────────────────────────────────────────────────────


def test_entries_render_as_sorted_joinable_rows(env) -> None:
    _write_prefs(env["shared"], "a_bot", {"users": {
        "ext:telegram:222": {"defaultRole": "power",
                             "updated_at": "2026-08-01T10:00:00+00:00"},
        "ext:slack:U01AAAA": {"defaultRole": "MAX",
                              "updated_at": "2026-08-02T11:00:00+00:00"},
    }})
    r = env["client"].get(_URL)
    assert r.status_code == 200
    body = r.get_json()
    assert body["bot_id"] == "a_bot"
    # Sorted by user_key → slack row first.
    assert body["users"] == [
        {"user_key": "ext:slack:U01AAAA", "default_role": "max",
         "updated_at": "2026-08-02T11:00:00+00:00",
         "channel": "slack", "external_id": "U01AAAA"},
        {"user_key": "ext:telegram:222", "default_role": "power",
         "updated_at": "2026-08-01T10:00:00+00:00",
         "channel": "telegram", "external_id": "222"},
    ]


def test_legacy_default_tier_field_is_honoured(env) -> None:
    """Pre-migration files carry ``defaultTier``; the reader must fall back
    to it, mirroring the plugin's ``defaultRole ?? defaultTier``."""
    _write_prefs(env["shared"], "a_bot", {"users": {
        "ext:telegram:333": {"defaultTier": "fast",
                             "updated_at": "2026-05-01T00:00:00+00:00"},
    }})
    body = env["client"].get(_URL).get_json()
    assert body["users"][0]["default_role"] == "fast"


def test_pod_key_passes_through_without_channel_fields(env) -> None:
    _write_prefs(env["shared"], "a_bot", {"users": {
        "pod:some_pod_user": {"defaultRole": "standard",
                              "updated_at": "2026-08-01T10:00:00+00:00"},
    }})
    body = env["client"].get(_URL).get_json()
    (row,) = body["users"]
    assert row["user_key"] == "pod:some_pod_user"
    assert row["default_role"] == "standard"
    assert "channel" not in row and "external_id" not in row


# ── Empty / malformed — never a 500 ─────────────────────────────────────────


def test_missing_file_returns_empty_list(env) -> None:
    r = env["client"].get(_URL)
    assert r.status_code == 200
    assert r.get_json() == {"bot_id": "a_bot", "users": []}


def test_malformed_json_returns_empty_list(env) -> None:
    _write_prefs(env["shared"], "a_bot", "{not json at all")
    r = env["client"].get(_URL)
    assert r.status_code == 200
    assert r.get_json()["users"] == []


def test_wrong_shape_returns_empty_list(env) -> None:
    _write_prefs(env["shared"], "a_bot", {"users": ["not", "a", "dict"]})
    r = env["client"].get(_URL)
    assert r.status_code == 200
    assert r.get_json()["users"] == []


def test_junk_entries_are_filtered_not_fatal(env) -> None:
    _write_prefs(env["shared"], "a_bot", {"users": {
        "ext:telegram:444": {"defaultRole": "power",
                             "updated_at": "2026-08-01T10:00:00+00:00"},
        "ext:telegram:555": "not-a-dict",
        "ext:telegram:666": {"no_role_field": True},
    }})
    body = env["client"].get(_URL).get_json()
    assert [u["user_key"] for u in body["users"]] == ["ext:telegram:444"]


# ── Unknown bot ─────────────────────────────────────────────────────────────


def test_unknown_bot_404s(env) -> None:
    r = env["client"].get("/api/admin/bots/no_such_bot/users/tier-prefs")
    assert r.status_code == 404
