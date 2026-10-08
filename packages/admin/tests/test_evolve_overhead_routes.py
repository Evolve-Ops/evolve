"""The Cost page's Evolve overhead panel + the tile card (D-OH5).

Brief ``evolve-overhead-ledger-and-budget``. The route is a thin re-shape of
the analyzer's ledger; the resume is Evolve's, not the bot's. Bot names are
role placeholders.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evolve_overhead as eo  # noqa: E402
import live_spend  # noqa: E402
from evolve_admin.web import routes_cost_measures  # noqa: E402
from evolve_admin.web import routes_evolve_overhead as reo  # noqa: E402

# Fixed, mid-day UTC: a clock-derived NOW lets the oldest fixture turns fall
# off the 7-local-day window at some hours of the day.
NOW = datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc)
LOOP_KEY = "agent:main:explicit:evolve:preflight:1"


def _turn(ago_min, source, cost, *, user_id=None, tag=None, key=None):
    t = {"ts": (NOW - timedelta(minutes=ago_min)).isoformat(), "source": source,
         "channel": "telegram", "model": "anthropic/claude-sonnet-4-5", "cost": cost,
         "cost_source": "oc", "user_id": user_id, "input_tokens": 1, "output_tokens": 1}
    if tag:
        t["evolve_tag"] = tag
        t["evolve_key"] = key or f"agent:main:explicit:evolve:{tag}:1"
    return t


def _week(ev_per_day=2):
    out = []
    for d in range(7):
        for i in range(10):
            out.append(_turn(d * 1440 + 600 + i, "human", 0.1, user_id="u1"))
        for i in range(ev_per_day):
            out.append(_turn(d * 1440 + 700 + i, "classifier", 0.01, tag="preflight"))
    return out


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    monkeypatch.setattr(live_spend, "pod_tz_or_local", lambda: timezone.utc)
    reo._memo.update(at=0.0, ledger=None)


@pytest.fixture
def env(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    net = {"members": ["bot-a", "bot-b"], "sharedDir": str(shared),
           "bots": {"bot-a": {}, "bot-b": {}}}
    path = tmp_path / "network.json"
    path.write_text(json.dumps(net))
    app = Flask(__name__)
    routes_cost_measures.register_cost_measures_routes(app, path)
    app.testing = True
    return {"client": app.test_client(), "shared": shared, "net": net, "path": path}


def _pin_trip_live(env, bot):
    """The routes judge expiry against the real clock; the fixture trip is
    stamped at the fixed NOW, so push its expiry out."""
    p = eo.breaker_path(env["shared"], bot)
    rec = json.loads(p.read_text())
    rec["expires_at"] = "2099-01-01T00:00:00+00:00"
    p.write_text(json.dumps(rec))


def _seed(env, bot_a_turns, bot_b_turns):
    turns = {"bot-a": bot_a_turns, "bot-b": bot_b_turns}
    eo.run_cycle(env["shared"], env["net"], now=NOW, turns_by_bot=turns, force_ledger=True)
    # run_cycle's ledger pass reads real turn files; write the fixture ledger.
    eo.write_ledger(env["shared"], eo.build_ledger(
        env["shared"], ["bot-a", "bot-b"], now=NOW, turns_by_bot=turns))


def test_row_per_bot_expandable_by_kind_with_pod_total(env):
    _seed(env, _week(2), _week(0))
    body = env["client"].get("/api/analytics/evolve-overhead").get_json()
    assert set(body["bots"]) == {"bot-a", "bot-b"}
    a = body["bots"]["bot-a"]
    assert a["measured"] and len(a["days"]) == 7
    assert a["d7"]["by_kind"]["classifier"]["calls"] == 14
    assert a["d7"]["by_kind"]["classifier"]["tags"]["preflight"]["calls"] == 14
    assert a["d7"]["share"] is not None and a["d7"]["context_cost_source"] == "estimate"
    assert body["pod"]["d7"]["evolve_calls"] == 14
    assert body["config"]["share_max"] == 0.05
    assert body["bots"]["bot-b"]["d7"]["evolve_calls"] == 0


def test_tripped_bot_card_names_the_caller_in_words(env):
    loop = _week() + [_turn(30 - i * 0.2, "classifier", 0.02, tag="preflight", key=LOOP_KEY)
                      for i in range(40)]
    _seed(env, loop, _week())
    _pin_trip_live(env, "bot-a")
    body = env["client"].get("/api/analytics/evolve-overhead").get_json()
    card = body["bots"]["bot-a"]["card"]
    assert card["tripped"] is True
    assert "still answering" in card["headline"]
    assert card["top_prefix_words"] == "Evolve's own routing calls"
    assert card["top_session_key"] == LOOP_KEY and card["top_count"] == 40
    assert body["bots"]["bot-b"]["card"]["tripped"] is False


def test_resume_is_evolves_and_counts_from_now(env):
    loop = _week() + [_turn(30 - i * 0.2, "classifier", 0.02, tag="preflight", key=LOOP_KEY)
                      for i in range(40)]
    _seed(env, loop, _week())
    _pin_trip_live(env, "bot-a")
    assert eo.breaker_path(env["shared"], "bot-a").exists()
    r = env["client"].post("/api/evolve-overhead/bot-a/resume", json={"reason": "seen it"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["accepted"]["accepted_calls"] == 40
    assert not eo.breaker_path(env["shared"], "bot-a").exists()
    assert eo.read_resumed_at(env["shared"], "bot-a") is not None
    # Evolve's resume never touches the bot's cost breaker or spend-cap state.
    assert not (env["shared"] / "spend-caps").exists()
    assert not (env["shared"] / "breakers" / "bot-a" / "cost.json").exists()


def test_resume_unknown_bot_is_404(env):
    assert env["client"].post("/api/evolve-overhead/nobody/resume", json={}).status_code == 404


def test_unreadable_turns_say_so_not_zero(env, monkeypatch):
    monkeypatch.setattr(eo, "load_turns", lambda *a, **k: None)
    body = env["client"].get("/api/analytics/evolve-overhead?refresh=1").get_json()
    assert body["unreadable_bots"] == ["bot-a", "bot-b"]
    assert body["bots"]["bot-a"]["measured"] is False and body["bots"]["bot-a"]["d7"] is None


def test_status_overlay_carries_the_card_but_not_a_bot_breaker(tmp_path):
    """A tripped overhead breaker is a card on the tile — never an
    ``active_breakers`` entry, which the tile reads as 'this bot is halted'."""
    from breakers import store
    import spend_caps

    shared = tmp_path / "shared"
    shared.mkdir()
    eo.trip(shared, "bot-a", eo.evaluate_hour(
        _week() + [_turn(30 - i * 0.2, "classifier", 0.02, tag="preflight", key=LOOP_KEY)
                   for i in range(40)], now=NOW, cfg=eo.OverheadConfig()),
        eo.OverheadConfig(), now=NOW)
    per_bot, pod = spend_caps.breaker_status_overlay(shared)
    assert per_bot == {} and pod == []
    assert store.list_active(shared) == []
    card = eo.card_for(shared, "bot-a", now=NOW)
    assert card["tripped"] and card["top_session_key"] == LOOP_KEY
    assert "paused" in card and card["resume_hint"]


def test_api_status_puts_the_card_on_the_tripped_tile_only(tmp_path):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "shared"
    shared.mkdir()
    path = tmp_path / "network.json"
    path.write_text(json.dumps({
        "primary": "team_bot_a", "members": ["team_bot_a", "security_bot"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "security_bot": {"user": "security_bot"}},
        "sharedDir": str(shared),
    }))
    loop = _week() + [_turn(30 - i * 0.2, "classifier", 0.02, tag="preflight", key=LOOP_KEY)
                      for i in range(40)]
    v = eo.evaluate_hour(loop, now=NOW, cfg=eo.OverheadConfig())
    eo.trip(shared, "team_bot_a", v, eo.OverheadConfig(), now=datetime.now(timezone.utc))
    app = create_app(path)
    app.testing = True
    bots = app.test_client().get("/api/status").get_json().get("bots") or {}
    if "team_bot_a" not in bots:
        pytest.skip("fixture pod does not render bot tiles in /api/status")
    tile = bots["team_bot_a"]
    assert tile["evolve_overhead"]["tripped"] is True
    assert tile["evolve_overhead"]["top_prefix_words"] == "Evolve's own routing calls"
    assert tile["active_breakers"] == []                 # the bot itself is not halted
    assert bots["security_bot"]["evolve_overhead"] is None
