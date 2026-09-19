"""The Users dimension of the Usage surface (/api/analytics/usage/by-user).

These tests pin the reader contract at the HTTP boundary: per-user
metrics arrive with the grade split intact (``total`` never absorbs
``inferred``), the unattributed-user bucket and its coverage share are
always present, a bot with no rollup is UNMEASURED rather than zero, and
a bot whose do-not-track gates WITHHELD attribution is reported as
withheld — a different fact from "nobody uses this".
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
ALICE = "slack:U-ALICE"
BOB = "telegram:998877"


def _metrics(turns: int, cost: float) -> dict:
    return {"turns": turns, "input_tokens": turns * 10,
            "output_tokens": turns * 20, "cost_estimated": cost}


def _rollup(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-08-17T03:37:00.000000Z",
        "bot_id": BOT,
        "as_of_date": "2026-08-17",
        "windows": {"d1": 1, "d7": 7, "d30": 30},
        "user_attribution": {
            "available": True, "reason": None, "requesters_in": 2,
            "requesters_withheld": 0, "sessions_with_requester": 12,
            "sessions_seen": 40,
            "gate_report": {"rows_in": 2, "rows_kept": 2, "rows_excluded": 0,
                            "exclusions": []},
        },
        "users": {
            ALICE: {
                "first_seen_ts": "2026-08-11T06:00:00.000Z",
                "last_seen_ts": "2026-08-17T06:00:00.000Z",
                "d1": _metrics(1, 0.2), "d7": _metrics(7, 1.4),
                "d30": _metrics(30, 6.0),
            },
            BOB: {
                "first_seen_ts": "2026-08-15T06:00:00.000Z",
                "last_seen_ts": "2026-08-16T06:00:00.000Z",
                "d1": _metrics(0, 0.0), "d7": _metrics(2, 5.0),
                "d30": _metrics(2, 5.0),
            },
        },
        "apps": {
            "morning-brief": {
                "d1": {"users": {ALICE: {"total": _metrics(1, 0.2),
                                         "inferred": _metrics(0, 0.0)}},
                       "unattributed_user": _metrics(4, 0.8)},
                "d7": {"users": {
                    ALICE: {"total": _metrics(7, 1.4),
                            "inferred": _metrics(3, 0.9)},
                    BOB: {"total": _metrics(2, 5.0),
                          "inferred": _metrics(0, 0.0)},
                }, "unattributed_user": _metrics(80, 16.0)},
                "d30": {"users": {}, "unattributed_user": _metrics(300, 60.0)},
            },
        },
        "unattributed_user": {
            "d1": {**_metrics(5, 1.0), "no_session_id_turns": 1,
                   "no_summary_turns": 3, "summary_without_requester_turns": 1},
            "d7": {**_metrics(91, 18.2), "no_session_id_turns": 5,
                   "no_summary_turns": 60, "summary_without_requester_turns": 26},
            "d30": {**_metrics(300, 60.0), "no_session_id_turns": 10,
                    "no_summary_turns": 200, "summary_without_requester_turns": 90},
        },
        "evolve_overhead": {
            "d1": _metrics(0, 0.0), "d7": _metrics(4, 0.8),
            "d30": _metrics(9, 1.8),
        },
        "coverage": {
            "d1": {"attributed_user_turns": 1, "unattributed_user_turns": 5,
                   "no_session_id_turns": 1, "no_summary_turns": 3,
                   "summary_without_requester_turns": 1,
                   "evolve_overhead_turns": 0, "distinct_users": 1,
                   "user_turns_total": 6, "unattributed_user_turns_share": 0.8333,
                   "unattributed_user_cost_share": 0.8333},
            "d7": {"attributed_user_turns": 9, "unattributed_user_turns": 91,
                   "no_session_id_turns": 5, "no_summary_turns": 60,
                   "summary_without_requester_turns": 26,
                   "evolve_overhead_turns": 4, "distinct_users": 2,
                   "user_turns_total": 100, "unattributed_user_turns_share": 0.91,
                   "unattributed_user_cost_share": 0.7396},
            "d30": {"attributed_user_turns": 32, "unattributed_user_turns": 300,
                    "no_session_id_turns": 10, "no_summary_turns": 200,
                    "summary_without_requester_turns": 90,
                    "evolve_overhead_turns": 9, "distinct_users": 2,
                    "user_turns_total": 332,
                    "unattributed_user_turns_share": 0.9036,
                    "unattributed_user_cost_share": 0.8451},
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

    app = Flask(__name__)
    _ra.register_analytics_routes(app, network)
    app.testing = True
    return {"client": app.test_client(), "shared": shared}


def _write_rollup(env, payload: dict, bot: str = BOT) -> None:
    (env["shared"] / bot).mkdir(parents=True, exist_ok=True)
    (env["shared"] / bot / "usage-by-user.json").write_text(json.dumps(payload))


def _get(env, query: str = f"bot={BOT}"):
    res = env["client"].get(f"/api/analytics/usage/by-user?{query}")
    assert res.status_code == 200, res.data
    return res.get_json()


# ── The per-user rollup at the HTTP boundary ─────────────────────────────────

def test_users_are_returned_cost_sorted_with_stable_keys(env):
    _write_rollup(env, _rollup())
    body = _get(env)

    assert body["window"] == "d7"
    bot = body["bots"][BOT]
    assert bot["measured"] is True
    assert bot["generated_at"] == "2026-08-17T03:37:00.000000Z"

    users = bot["users"]
    assert [u["user_key"] for u in users] == [BOB, ALICE]  # $5.00 before $1.40
    # The stable key is split for display, never re-derived from the id shape.
    assert (users[0]["platform"], users[0]["user_id"]) == ("telegram", "998877")
    assert (users[1]["platform"], users[1]["user_id"]) == ("slack", "U-ALICE")
    # Unresolved identities still get a categorized label, never a bare id.
    assert users[0]["label"] == "Telegram chat · 998877"
    assert users[1]["label"] == "Slack user · U-ALICE"
    assert users[1]["turns"] == 7
    assert users[1]["last_seen_ts"] == "2026-08-17T06:00:00.000Z"


def test_per_app_users_keep_the_grade_split(env):
    _write_rollup(env, _rollup())
    apps = _get(env)["bots"][BOT]["apps"]
    assert [a["app_id"] for a in apps] == ["morning-brief"]

    rows = apps[0]["users"]
    assert [r["user_key"] for r in rows] == [BOB, ALICE]  # $5.00 before $1.40
    alice = rows[1]
    assert alice["total"]["turns"] == 7
    assert alice["inferred"]["turns"] == 3
    # The inferred turns are NOT inside the total, at the boundary either.
    assert alice["total"]["cost_estimated"] == pytest.approx(1.4)
    assert set(alice) >= {"total", "inferred", "user_key", "label"}
    # And the app's own unattributed-user bucket rides along.
    assert apps[0]["unattributed_user"]["turns"] == 80


def test_unattributed_user_and_coverage_are_always_returned(env):
    _write_rollup(env, _rollup())
    bot = _get(env)["bots"][BOT]

    un = bot["unattributed_user"]
    assert un["turns"] == 91
    # The three-way WHY split survives to the client.
    assert un["no_session_id_turns"] == 5
    assert un["no_summary_turns"] == 60
    assert un["summary_without_requester_turns"] == 26

    coverage = bot["coverage"]
    assert coverage["attributed_user_turns"] == 9
    assert coverage["distinct_users"] == 2
    assert coverage["unattributed_user_turns_share"] == pytest.approx(0.91)
    assert bot["evolve_overhead"]["turns"] == 4


def test_window_selects_the_matching_block(env):
    _write_rollup(env, _rollup())
    body = _get(env, f"bot={BOT}&window=d30")
    bot = body["bots"][BOT]
    assert body["window"] == "d30"
    assert bot["coverage"]["attributed_user_turns"] == 32
    assert bot["unattributed_user"]["turns"] == 300
    # d30 has no per-app user rows in this fixture — an empty list, not a 500.
    assert bot["apps"][0]["users"] == []


def test_unknown_window_is_rejected(env):
    res = env["client"].get(f"/api/analytics/usage/by-user?bot={BOT}&window=d90")
    assert res.status_code == 400


# ── Not measured, and withheld, are different facts ──────────────────────────

def test_bot_with_no_rollup_is_unmeasured_not_zero(env):
    body = _get(env)
    bot = body["bots"][BOT]
    assert bot["measured"] is False
    assert bot["users"] == []
    # None, not 0 / {} — "we have not measured", never "nobody used it".
    assert bot["coverage"] is None
    assert bot["unattributed_user"] is None
    assert bot["evolve_overhead"] is None
    assert bot["user_attribution"] is None
    # An unmeasured bot is not a WITHHELD bot.
    assert body["attribution_withheld_bots"] == []


def test_withheld_attribution_is_named_not_shown_as_no_users(env):
    payload = _rollup()
    payload["users"] = {}
    payload["user_attribution"] = {
        "available": False, "reason": "signal_disabled", "requesters_in": 2,
        "requesters_withheld": 2, "sessions_with_requester": 0,
        "sessions_seen": 40, "gate_report": None,
    }
    _write_rollup(env, payload)

    body = _get(env)
    bot = body["bots"][BOT]
    assert bot["measured"] is True          # the rollup DID run
    assert bot["users"] == []
    assert bot["user_attribution"]["available"] is False
    assert bot["user_attribution"]["reason"] == "signal_disabled"
    assert body["attribution_withheld_bots"] == [BOT]
    # Usage itself is still reported — withheld identity, not lost turns.
    assert bot["unattributed_user"]["turns"] == 91


def test_gate_report_counts_reach_the_client_without_identities(env):
    payload = _rollup()
    payload["user_attribution"]["requesters_withheld"] = 1
    payload["user_attribution"]["gate_report"] = {
        "rows_in": 2, "rows_kept": 1, "rows_excluded": 1,
        "exclusions": [{"bot_id": BOT, "reason": "excluded_requester",
                        "rows_excluded": 1}],
    }
    _write_rollup(env, payload)

    attribution = _get(env)["bots"][BOT]["user_attribution"]
    assert attribution["requesters_withheld"] == 1
    exclusion = attribution["gate_report"]["exclusions"][0]
    # (bot, reason class, count) — never who.
    assert set(exclusion) == {"bot_id", "reason", "rows_excluded"}


def test_default_bot_list_covers_every_member(env):
    _write_rollup(env, _rollup())
    body = _get(env, "window=d7")
    # Members plus the evolve bot, which also runs the plugin.
    assert set(body["bots"]) == {BOT, "evolve"}
    assert body["bots"]["evolve"]["measured"] is False


# ── Key splitting ────────────────────────────────────────────────────────────

def test_key_without_a_platform_half_is_not_guessed(env):
    payload = _rollup()
    payload["users"] = {"U-BARE": {
        "first_seen_ts": None, "last_seen_ts": None,
        "d1": _metrics(1, 0.1), "d7": _metrics(1, 0.1), "d30": _metrics(1, 0.1),
    }}
    _write_rollup(env, payload)

    row = _get(env)["bots"][BOT]["users"][0]
    # "unknown", not a platform inferred from the id's shape.
    assert row["platform"] == "unknown"
    assert row["user_id"] == "U-BARE"
    assert row["label"] == "User · U-BARE"


def test_key_with_colons_in_the_id_keeps_the_whole_id(env):
    payload = _rollup()
    payload["users"] = {"matrix:@person:example.org": {
        "first_seen_ts": None, "last_seen_ts": None,
        "d1": _metrics(1, 0.1), "d7": _metrics(1, 0.1), "d30": _metrics(1, 0.1),
    }}
    _write_rollup(env, payload)

    row = _get(env)["bots"][BOT]["users"][0]
    assert row["platform"] == "matrix"
    assert row["user_id"] == "@person:example.org"
