"""The Apps dimension of the Usage surface (/api/analytics/usage/by-app)
plus the per-app tile fields on /api/analytics/applications (AL-1.3).

These tests pin the reader contract at the HTTP boundary: grades arrive
additive (``total`` never absorbs ``inferred``), the unattributed bucket
and its coverage share are always present, and a bot with no rollup is
reported as UNMEASURED (with the usage_logger fallback named) rather than
as zero usage.
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
sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin.web import routes_analytics as _ra  # noqa: E402

BOT = "team-bot-a"


def _rollup(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-08-17T03:35:00.000000Z",
        "bot_id": BOT,
        "apps": {
            "digest": {
                "first_seen_ts": "2026-08-11T06:00:00.000Z",
                "last_seen_ts": "2026-08-17T06:00:00.000Z",
                "d7": {
                    "total": {"turns": 7, "input_tokens": 70, "output_tokens": 140,
                              "cost_estimated": 1.4},
                    "scheduled": {"turns": 7, "input_tokens": 70, "output_tokens": 140,
                                  "cost_estimated": 1.4},
                    "explicit": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                 "cost_estimated": 0.0},
                    "inferred": {"turns": 3, "input_tokens": 30, "output_tokens": 60,
                                 "cost_estimated": 0.9},
                },
                "d1": {"total": {"turns": 1, "input_tokens": 10, "output_tokens": 20,
                                 "cost_estimated": 0.2},
                       "scheduled": {"turns": 1, "input_tokens": 10, "output_tokens": 20,
                                     "cost_estimated": 0.2},
                       "explicit": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cost_estimated": 0.0},
                       "inferred": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cost_estimated": 0.0}},
                "d30": {"total": {"turns": 30, "input_tokens": 300, "output_tokens": 600,
                                  "cost_estimated": 6.0},
                        "scheduled": {"turns": 30, "input_tokens": 300,
                                      "output_tokens": 600, "cost_estimated": 6.0},
                        "explicit": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                     "cost_estimated": 0.0},
                        "inferred": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                     "cost_estimated": 0.0}},
            },
            "notes": {
                "first_seen_ts": "2026-08-15T09:00:00.000Z",
                "last_seen_ts": "2026-08-16T09:00:00.000Z",
                "d7": {
                    "total": {"turns": 2, "input_tokens": 20, "output_tokens": 40,
                              "cost_estimated": 5.0},
                    "scheduled": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                  "cost_estimated": 0.0},
                    "explicit": {"turns": 2, "input_tokens": 20, "output_tokens": 40,
                                 "cost_estimated": 5.0},
                    "inferred": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                 "cost_estimated": 0.0},
                },
                "d1": {"total": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                 "cost_estimated": 0.0},
                       "scheduled": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                     "cost_estimated": 0.0},
                       "explicit": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cost_estimated": 0.0},
                       "inferred": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cost_estimated": 0.0}},
                "d30": {"total": {"turns": 2, "input_tokens": 20, "output_tokens": 40,
                                  "cost_estimated": 5.0},
                        "scheduled": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                      "cost_estimated": 0.0},
                        "explicit": {"turns": 2, "input_tokens": 20, "output_tokens": 40,
                                     "cost_estimated": 5.0},
                        "inferred": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                                     "cost_estimated": 0.0}},
            },
        },
        "unattributed": {
            "d1": {"turns": 5, "input_tokens": 50, "output_tokens": 100,
                   "cost_estimated": 1.0, "legacy_schema_turns": 0},
            "d7": {"turns": 91, "input_tokens": 910, "output_tokens": 1820,
                   "cost_estimated": 18.2, "legacy_schema_turns": 40},
            "d30": {"turns": 300, "input_tokens": 3000, "output_tokens": 6000,
                    "cost_estimated": 60.0, "legacy_schema_turns": 200},
        },
        "evolve_overhead": {
            "d1": {"turns": 0, "input_tokens": 0, "output_tokens": 0,
                   "cost_estimated": 0.0},
            "d7": {"turns": 4, "input_tokens": 40, "output_tokens": 80,
                   "cost_estimated": 0.8},
            "d30": {"turns": 9, "input_tokens": 90, "output_tokens": 180,
                    "cost_estimated": 1.8},
        },
        "coverage": {
            "d1": {"attributed_turns": 1, "inferred_turns": 0, "unattributed_turns": 5,
                   "legacy_schema_turns": 0, "evolve_overhead_turns": 0,
                   "app_turns_total": 6, "unattributed_turns_share": 0.8333,
                   "unattributed_cost_share": 0.8333},
            "d7": {"attributed_turns": 9, "inferred_turns": 3, "unattributed_turns": 91,
                   "legacy_schema_turns": 40, "evolve_overhead_turns": 4,
                   "app_turns_total": 103, "unattributed_turns_share": 0.8835,
                   "unattributed_cost_share": 0.7396},
            "d30": {"attributed_turns": 32, "inferred_turns": 0,
                    "unattributed_turns": 300, "legacy_schema_turns": 200,
                    "evolve_overhead_turns": 9, "app_turns_total": 332,
                    "unattributed_turns_share": 0.9036,
                    "unattributed_cost_share": 0.8451},
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / BOT).mkdir(parents=True)
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"members": [BOT], "sharedDir": str(shared)}))

    monkeypatch.setattr(_ra, "_list_manifests_as_bot", lambda bot_id, user=None: [])

    app = Flask(__name__)
    _ra.register_analytics_routes(app, network)
    app.testing = True
    return {"client": app.test_client(), "shared": shared}


def _write_rollup(env, payload: dict, bot: str = BOT) -> None:
    (env["shared"] / bot).mkdir(parents=True, exist_ok=True)
    (env["shared"] / bot / "usage-by-app.json").write_text(json.dumps(payload))


# ── /api/analytics/usage/by-app ──────────────────────────────────────────────

def test_apps_dimension_returns_grades_additively(env):
    _write_rollup(env, _rollup())
    res = env["client"].get(f"/api/analytics/usage/by-app?bot={BOT}")
    assert res.status_code == 200, res.data
    body = res.get_json()

    assert body["window"] == "d7"
    apps = body["bots"][BOT]["apps"]
    # Sorted by deterministic cost desc: notes ($5.00) before digest ($1.40).
    assert [a["app_id"] for a in apps] == ["notes", "digest"]

    digest = next(a for a in apps if a["app_id"] == "digest")
    assert digest["total"]["turns"] == 7
    assert digest["scheduled"]["turns"] == 7
    assert digest["inferred"]["turns"] == 3
    # The route must not fold inferred into the total on the way out.
    assert digest["total"]["turns"] != (
        digest["scheduled"]["turns"] + digest["inferred"]["turns"]
    )
    assert digest["last_seen_ts"] == "2026-08-17T06:00:00.000Z"


def test_unattributed_and_coverage_always_ride_along(env):
    _write_rollup(env, _rollup())
    body = env["client"].get(f"/api/analytics/usage/by-app?bot={BOT}").get_json()
    bot = body["bots"][BOT]

    assert bot["unattributed"]["turns"] == 91
    assert bot["unattributed"]["legacy_schema_turns"] == 40
    assert bot["coverage"]["unattributed_turns_share"] == 0.8835
    assert bot["evolve_overhead"]["turns"] == 4


def test_window_parameter_selects_the_window(env):
    _write_rollup(env, _rollup())
    body = env["client"].get(
        f"/api/analytics/usage/by-app?bot={BOT}&window=d30"
    ).get_json()
    assert body["window"] == "d30"
    digest = next(a for a in body["bots"][BOT]["apps"] if a["app_id"] == "digest")
    assert digest["total"]["turns"] == 30
    assert body["bots"][BOT]["unattributed"]["turns"] == 300


def test_bad_window_is_rejected(env):
    res = env["client"].get(f"/api/analytics/usage/by-app?bot={BOT}&window=d90")
    assert res.status_code == 400


def test_missing_rollup_reads_as_unmeasured_not_zero(env):
    body = env["client"].get(f"/api/analytics/usage/by-app?bot={BOT}").get_json()
    bot = body["bots"][BOT]
    assert bot["measured"] is False
    assert bot["apps"] == []
    # None, not 0.0 — "we have not measured this" vs "nothing was spent".
    assert bot["unattributed"] is None
    assert bot["coverage"] is None
    assert BOT in body["usage_stats_fallback_bots"]


def test_zero_attributed_turns_flags_the_usage_stats_fallback(env):
    payload = _rollup(apps={})
    payload["coverage"]["d7"].update({"attributed_turns": 0, "inferred_turns": 0})
    _write_rollup(env, payload)
    body = env["client"].get(f"/api/analytics/usage/by-app?bot={BOT}").get_json()
    assert body["bots"][BOT]["measured"] is True
    # Measured, but nothing attributed → the tile falls back to the mtime
    # footprint and must label it as such.
    assert BOT in body["usage_stats_fallback_bots"]
    # …and the unattributed bucket is still reported, not hidden.
    assert body["bots"][BOT]["unattributed"]["turns"] == 91


# ── /api/analytics/applications (per-app tile) ───────────────────────────────

def test_app_tile_carries_real_usage_and_coverage(env, monkeypatch, tmp_path):
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    mf = mdir / "digest.json"
    mf.write_text(json.dumps({"id": "digest", "name": "Digest"}))
    monkeypatch.setattr(_ra, "_list_manifests_as_bot", lambda bot_id, user=None: [str(mf)])
    _write_rollup(env, _rollup())

    tiles = env["client"].get(
        f"/api/analytics/applications?bot={BOT}"
    ).get_json()[BOT]
    tile = tiles[0]

    assert tile["usage_turns_7d"] == 7
    assert tile["usage_cost_7d"] == 1.4
    # Inferred stays in its own field so the tile can badge it.
    assert tile["usage_inferred_turns_7d"] == 3
    assert tile["usage_last_seen_ts"] == "2026-08-17T06:00:00.000Z"
    assert tile["usage_attribution_coverage"]["unattributed_turns_share"] == 0.8835
    assert tile["usage_attribution_coverage"]["attributed_primary"] is True
    # The mtime footprint fields survive untouched — the fallback signal
    # is demoted, not deleted.
    assert "usage_days_since_modified" in tile


def test_app_tile_usage_is_none_when_rollup_absent(env, monkeypatch, tmp_path):
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    mf = mdir / "digest.json"
    mf.write_text(json.dumps({"id": "digest", "name": "Digest"}))
    monkeypatch.setattr(_ra, "_list_manifests_as_bot", lambda bot_id, user=None: [str(mf)])

    tile = env["client"].get(
        f"/api/analytics/applications?bot={BOT}"
    ).get_json()[BOT][0]

    assert tile["usage_turns_7d"] is None
    assert tile["usage_cost_7d"] is None
    assert tile["usage_attribution_coverage"] is None
