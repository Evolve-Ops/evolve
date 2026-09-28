"""Tiny fixture server for screenshotting the Turn Audit drawer.

Spins up a Flask app that mounts the real index.html and stubs the two
endpoints the Cost Monitor → Turn Audit drawer talks to. Used to
generate PR screenshots; not part of the production server.

    python3 scripts/turn_audit_drawer_demo.py
    open http://127.0.0.1:8765/

Synthetic data only — no bot records are read.
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, jsonify, send_file


_INDEX = Path(__file__).resolve().parent.parent / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_FIXTURE_TURNS = [
    {
        "ts": "2026-05-05T18:23:11Z",
        "bot_id": "admin_bot",
        "model": "anthropic/claude-sonnet-4-6",
        "source": "human",
        "session_class": "productive",
        "model_tier": "tier1",
        "input_tokens": 14200,
        "output_tokens": 1820,
        "cache_read_tokens": 41100,
        "cache_write_tokens": 7300,
        "cost_estimated": 0.0921,
        "session_id": "sess-20260505-18-admin_bot",
        "turn_id": "turn-2026-05-05-18-admin_bot-001",
    },
    {
        "ts": "2026-05-05T17:42:09Z",
        "bot_id": "team_bot_a",
        "model": "anthropic/claude-haiku-4-5",
        "source": "cron",
        "session_class": "maintenance",
        "model_tier": "tier3",
        "input_tokens": 32100,
        "output_tokens": 220,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_estimated": 0.0142,
        "session_id": "sess-20260505-17-team_bot_a",
        "turn_id": "turn-2026-05-05-17-team_bot_a-007",
    },
    {
        "ts": "2026-05-05T16:08:54Z",
        "bot_id": "team_bot_b",
        "model": "anthropic/claude-sonnet-4-6",
        "source": "human",
        "session_class": "productive",
        "model_tier": "tier1",
        "input_tokens": 8100,
        "output_tokens": 940,
        "cache_read_tokens": 22000,
        "cache_write_tokens": 0,
        "cost_estimated": 0.0381,
        "session_id": "sess-20260505-16-team_bot_b",
        "turn_id": "turn-2026-05-05-16-team_bot_b-003",
    },
]

_FIXTURE_DETAIL = {
    "annotation": {
        "type": "turn_annotation",
        "turn_id": "turn-2026-05-05-18-admin_bot-001",
        "session_id": "sess-20260505-18-admin_bot",
        "ts": "2026-05-05T18:23:11Z",
        "bot_id": "admin_bot",
        "session_class": "productive",
        "class_confidence": 0.94,
        "model_tier": "tier1",
        "model_selected": "claude-sonnet-4-6",
        "provider": "anthropic",
        "auth_mode": "token",
        "resolution_turn": 4,
        "correction_detected": False,
        "input_tokens": 14200,
        "output_tokens": 1820,
        "cache_read_tokens": 41100,
        "cache_write_tokens": 7300,
        "cost_estimated": 0.0921,
    },
    "turn_record": {
        "session_id": "sess-20260505-18-admin_bot",
        "ts": "2026-05-05T18:23:11Z",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "source": "human",
        "channel": "telegram",
        "auth_mode": "token",
    },
    "cost_events": [
        {
            "ts": "2026-05-05T18:23:11Z",
            "session_id": "sess-20260505-18-admin_bot",
            "trigger_kind": "user_turn",
            "cache_state": "warm",
            "model": "claude-sonnet-4-6",
            "input_tokens": 14200,
            "output_tokens": 1820,
            "cache_read_tokens": 41100,
            "cache_write_tokens": 7300,
            "cost_usd": 0.0921,
        },
    ],
    "transcript": {
        "system": {
            "chars": 4820,
            "text": (
                "You are Admin_bot, the team's onboarding bot.\n"
                "Stay concise. Refuse anything that requires admin access.\n"
                "API key (redacted): [redacted-key]\n"
                "Pod: evolve-ops. Members: team_bot_a, team_bot_b, admin_bot, evolve."
            ),
            "truncated": False,
        },
        "user": {
            "chars": 142,
            "text": "Hey Admin_bot, can you summarize the docs for the new app onboarding flow and tell me what changed between yesterday's revision and today's?",
            "truncated": False,
        },
        "assistant": {
            "chars": 1340,
            "text": (
                "Sure — here's the diff in plain English:\n\n"
                "1. Added a new \"Permissions Preview\" step before submitting.\n"
                "2. The provider list is now grouped by category.\n"
                "3. Telegram bot_token onboarding now uses the rotation API.\n\n"
                "The full doc is at docs/onboarding.md if you want to read it."
            ),
            "truncated": False,
        },
        "tool_calls": [
            {
                "tool": "Read",
                "input_chars": 78,
                "result_chars": 4120,
                "input": '{"file_path": "docs/onboarding.md"}',
                "input_truncated": False,
                "result": "(file contents — 4120 chars)",
                "result_truncated": False,
            },
            {
                "tool": "Bash",
                "input_chars": 56,
                "result_chars": 612,
                "input": '{"command": "git log --oneline -- docs/onboarding.md | head -5"}',
                "input_truncated": False,
                "result": "abc1234 docs: clarify telegram rotation\n…",
                "result_truncated": False,
            },
        ],
        "tools_invoked": 2,
        "tool_summary": {"Read": 1, "Bash": 1},
    },
    "transcript_status": "ok",
    "transcript_source": "/Users/admin_bot/.openclaw/agents/main/agent/sessions/sess-20260505-18-admin_bot/transcript.jsonl",
}


app = Flask(__name__)


@app.get("/")
def index():
    return send_file(_INDEX)


# Endpoints the front-end calls when the Cost Monitor page is opened.

@app.get("/api/network")
def api_network():
    return jsonify({
        "sharedDir": "/Users/Shared/evolve",
        "members": ["admin_bot", "team_bot_a", "team_bot_b"],
        "bots": {"admin_bot": {}, "team_bot_a": {}, "team_bot_b": {}},
        "timezone": "America/Los_Angeles",
    })


@app.get("/api/analytics/turns/audit")
def api_audit():
    return jsonify({"turns": _FIXTURE_TURNS, "total": len(_FIXTURE_TURNS), "days": 7, "sort": "ts"})


@app.get("/api/analytics/turns/<turn_id>")
def api_turn_detail(turn_id):
    return jsonify(_FIXTURE_DETAIL)


# Stub the rest of the panel just enough that the page renders without
# the loading spinner blocking the audit table.

@app.get("/api/analytics/cost")
def api_cost():
    return jsonify({"total_cost": 0, "daily": [], "alert": {}})


@app.get("/api/analytics/sessions/runaway")
def api_runaway():
    return jsonify({})


@app.get("/api/cost-measures/forensics/spikes")
def api_spikes():
    return jsonify({"spikes": [], "total_events_scanned": 0, "bots": [], "days": 7, "limit": 20})


@app.get("/api/cost-measures/forensics/trigger-rollup")
def api_trigger():
    return jsonify({"combined": {}, "per_bot": {}, "bots": [], "days": 7})


@app.get("/api/cost-measures/forensics/app-rollup")
def api_app():
    return jsonify({"combined": {}, "per_bot": {}, "bots": [], "days": 7})


@app.get("/api/<path:rest>")
def fallback(rest):
    return jsonify({}), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
