"""tests/test_primary_state_routes.py — Flask routes for /api/primary/state/*.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.

Tests assert the HTTP shape (status codes, JSON shape, query-string
parsing). The library functions themselves are exercised separately
in test_pod_state.py — these tests verify the glue.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def state_app(tmp_path):
    from flask import Flask
    from evolve_admin.web.primary_state_routes import (
        register_primary_state_routes,
    )

    network = {"sharedDir": str(tmp_path), "bots": {}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_primary_state_routes(app, network_path)
    app.config["_SHARED_DIR"] = tmp_path
    return app


def _write_network(path: Path, bots: dict) -> None:
    path.write_text(json.dumps({
        "sharedDir": str(path.parent),
        "bots": bots,
    }))


def _write_signal(shared_dir: Path, *, state="firing", **overrides):
    from schema.signal import Signal, new_signal_id
    from signals import store as _store

    defaults = dict(
        id=new_signal_id(),
        signature=f"test:t:{overrides.get('bot_id','team_bot_a')}",
        producer="test",
        type="t",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id="team_bot_a",
        title="t",
        body="",
    )
    defaults.update(overrides)
    defaults["state"] = state  # type: ignore[assignment]
    sig = Signal(**defaults)
    _store.write_signal(sig, shared_dir, subdir=_store.subdir_for_state(state))
    return sig.id


# ── /api/primary/state/pod_status ───────────────────────────────────────────


class TestPodStatusRoute:
    def test_empty_pod_returns_empty_bots(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/pod_status")
        assert r.status_code == 200
        body = r.get_json()
        assert body["bots"] == []
        assert body["primary_id"] is None

    def test_reflects_network_json(self, state_app):
        network_path = Path(state_app.config["_NETWORK_PATH"]) if "_NETWORK_PATH" in state_app.config \
            else Path(state_app.config["_SHARED_DIR"]) / "network.json"
        _write_network(network_path, {
            "evo": {"role": "primary", "tier": "full"},
            "team_bot_a": {"role": "member", "tier": "full"},
        })
        client = state_app.test_client()
        r = client.get("/api/primary/state/pod_status")
        assert r.status_code == 200
        body = r.get_json()
        assert body["primary_id"] == "evo"
        assert {b["bot_id"] for b in body["bots"]} == {"evo", "team_bot_a"}


# ── /api/primary/state/signals ──────────────────────────────────────────────


class TestSignalsRoute:
    def test_empty_store_returns_empty(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/signals")
        assert r.status_code == 200
        body = r.get_json()
        assert body["signals"] == []
        assert body["count"] == 0
        assert body["filters"]["state"] == "firing"

    def test_filters_by_state(self, state_app):
        shared = Path(state_app.config["_SHARED_DIR"])
        _write_signal(shared, state="firing", bot_id="team_bot_a")
        _write_signal(shared, state="resolved", bot_id="team_bot_a",
                      signature="test:t:team_bot_a-resolved")

        client = state_app.test_client()
        r = client.get("/api/primary/state/signals?state=resolved")
        body = r.get_json()
        assert body["count"] == 1
        assert body["signals"][0]["state"] == "resolved"

    def test_filters_by_bot_query_param(self, state_app):
        shared = Path(state_app.config["_SHARED_DIR"])
        _write_signal(shared, state="firing", bot_id="team_bot_a",
                      signature="test:t:team_bot_a")
        _write_signal(shared, state="firing", bot_id="admin_bot",
                      signature="test:t:admin_bot")

        client = state_app.test_client()
        r = client.get("/api/primary/state/signals?bot=team_bot_a")
        body = r.get_json()
        assert body["count"] == 1
        assert body["signals"][0]["bot_id"] == "team_bot_a"

    def test_invalid_limit_falls_back_to_default(self, state_app):
        """A non-integer limit shouldn't 500 — the bot's LLM might pass
        a malformed value."""
        client = state_app.test_client()
        r = client.get("/api/primary/state/signals?limit=notanumber")
        assert r.status_code == 200
        # Default limit (20) gets through to pod_state, which caps and
        # echoes back.
        assert r.get_json()["filters"]["limit"] == 20


# ── /api/primary/state/proposals ────────────────────────────────────────────


class TestProposalsRoute:
    def test_empty_store_returns_empty(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/proposals")
        assert r.status_code == 200
        body = r.get_json()
        assert body["proposals"] == []
        assert body["count"] == 0
        assert body["filters"]["state"] == "pending"

    def test_state_query_param_passes_through(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/proposals?state=archived")
        body = r.get_json()
        assert body["filters"]["state"] == "archived"


# ── /api/primary/state/watchdog ─────────────────────────────────────────────


class TestWatchdogRoute:
    def test_empty_returns_empty(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/watchdog")
        assert r.status_code == 200
        body = r.get_json()
        assert body["events"] == []
        assert body["count"] == 0
        # Default 24-hour window.
        assert body["filters"]["hours"] == 24

    def test_hours_query_param_caps_at_one_week(self, state_app):
        """The bot can ask for more than a week and we silently cap —
        not a 400 — to keep the tool call simple."""
        client = state_app.test_client()
        r = client.get("/api/primary/state/watchdog?hours=1000")
        body = r.get_json()
        # MAX_HOURS = 168.
        assert body["filters"]["hours"] == 168


# ── /api/primary/state/spend ────────────────────────────────────────────────


class TestSpendRoute:
    def test_no_data_returns_zeros(self, state_app):
        shared = Path(state_app.config["_SHARED_DIR"])
        _write_network(shared / "network.json", {"team_bot_a": {"role": "member"}})
        client = state_app.test_client()
        r = client.get("/api/primary/state/spend?bot=team_bot_a")
        assert r.status_code == 200
        body = r.get_json()
        assert body["total_usd"] == 0.0
        assert body["window"] == "7d"
        assert body["bot_id"] == "team_bot_a"

    def test_window_query_param(self, state_app):
        shared = Path(state_app.config["_SHARED_DIR"])
        _write_network(shared / "network.json", {"team_bot_a": {"role": "member"}})
        client = state_app.test_client()
        r = client.get("/api/primary/state/spend?bot=team_bot_a&window=30d")
        body = r.get_json()
        assert body["window"] == "30d"
        assert body["days"] == 30

    def test_pod_wide_when_bot_omitted(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/spend?window=7d")
        body = r.get_json()
        assert body["bot_id"] is None
        # by_bot present (possibly empty).
        assert "by_bot" in body

    def test_unknown_bot_returns_400(self, state_app):
        """Whitelist applies to /spend too: bot must be in network.bots."""
        client = state_app.test_client()
        r = client.get("/api/primary/state/spend?bot=nonexistent")
        assert r.status_code == 400


# ── /api/primary/state/turns ────────────────────────────────────────────────


class TestTurnsRoute:
    def test_missing_bot_returns_400(self, state_app):
        """Unlike the other read tools, recent_turns requires a bot —
        a pod-wide transcript dump would leak across security
        boundaries."""
        client = state_app.test_client()
        r = client.get("/api/primary/state/turns")
        assert r.status_code == 400
        assert "bot" in r.get_json()["error"]

    def test_unknown_bot_returns_400(self, state_app):
        """Bots must be in network.json. Loopback or not, accepting any
        string is the path-traversal vector caught in Bundle 3-part-2
        second-pass review: bot='../../../etc/passwd' walked out of
        shared_dir at the library layer. Route-level whitelist closes
        that."""
        client = state_app.test_client()
        r = client.get("/api/primary/state/turns?bot=nonexistent")
        assert r.status_code == 400
        assert "unknown bot" in r.get_json()["error"]

    def test_path_traversal_rejected(self, state_app):
        """Regression for the path-traversal vector: a relative-path
        bot id must not reach the filesystem layer."""
        client = state_app.test_client()
        r = client.get(
            "/api/primary/state/turns?bot=../../../etc/passwd"
        )
        assert r.status_code == 400

    def test_returns_opted_out_when_no_buffer(self, state_app):
        shared = Path(state_app.config["_SHARED_DIR"])
        _write_network(shared / "network.json", {"team_bot_a": {"role": "member"}})
        client = state_app.test_client()
        r = client.get("/api/primary/state/turns?bot=team_bot_a")
        assert r.status_code == 200
        body = r.get_json()
        assert body["opted_out"] is True
        assert body["turns"] == []


# ── /api/primary/state/describe ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_audit_state():
    """Audit cache is module-level; reset between tests."""
    from evolve_admin import audit_state
    audit_state.reset()
    yield
    audit_state.reset()


class TestDescribeRoute:
    def test_missing_bot_returns_400(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/describe")
        assert r.status_code == 400

    def test_unknown_bot_returns_400(self, state_app):
        """Originally this returned 200/found=False, but Bundle 3-part-2
        review pulled the validation up to the route layer so the
        whitelist check is applied uniformly across /turns, /describe,
        and /spend. Trade-off: the LLM now sees a 400 error string
        rather than a clean found=false envelope. That's acceptable —
        the call() helper in PodStateTools.ts already routes 400 to
        isError=true and the LLM handles it.
        """
        client = state_app.test_client()
        r = client.get("/api/primary/state/describe?bot=nonexistent")
        assert r.status_code == 400

    def test_returns_known_bot(self, state_app):
        shared = Path(state_app.config["_SHARED_DIR"])
        _write_network(shared / "network.json", {
            "team_bot_a": {"role": "member", "tier": "full"},
        })
        client = state_app.test_client()
        r = client.get("/api/primary/state/describe?bot=team_bot_a")
        assert r.status_code == 200
        body = r.get_json()
        assert body["found"] is True
        assert body["role"] == "member"


# ── /api/primary/state/audits ───────────────────────────────────────────────


class TestAuditsRoute:
    def test_empty_cache_reports_stale(self, state_app):
        client = state_app.test_client()
        r = client.get("/api/primary/state/audits")
        assert r.status_code == 200
        body = r.get_json()
        assert body["audits"] == []
        assert body["stale"] is True

    def test_unknown_bot_returns_400(self, state_app):
        """Bot whitelist applies here too — protects against the
        same vector as /turns and /describe."""
        client = state_app.test_client()
        r = client.get("/api/primary/state/audits?bot=nonexistent")
        assert r.status_code == 400

    def test_returns_cached_data(self, state_app):
        """Populate the cache directly, then read through the route."""
        import time as _time
        from evolve_admin import audit_state

        shared = Path(state_app.config["_SHARED_DIR"])
        _write_network(shared / "network.json", {"team_bot_a": {"role": "member"}})
        audit_state._state["data"]["team_bot_a"] = {
            "summary": {"critical": 2, "warn": 1, "info": 0}}
        audit_state._state["cached_at"] = _time.time()

        client = state_app.test_client()
        r = client.get("/api/primary/state/audits?bot=team_bot_a")
        body = r.get_json()
        assert body["count"] == 1
        assert body["stale"] is False
        assert body["audits"][0]["summary"]["critical"] == 2
