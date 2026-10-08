"""The PA-week cost rollup, the standard-default estimate, and the operator's
reading (brief ``pa-week-cost-is-measured``). A fixture pod: a week of turn rows
in the shape the plugin writes, no network, no real shared dir. Bot names are
role placeholders."""
from __future__ import annotations

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

import live_spend  # noqa: E402
import pa_week_cost as pw  # noqa: E402
from evolve_admin.web import routes_pa_week_cost as rpw  # noqa: E402

NOW = datetime(2026, 10, 16, 18, 30, tzinfo=timezone.utc)
POWER = "anthropic/claude-opus-4-1"
STANDARD = "anthropic/claude-sonnet-4-5"
FAST = "anthropic/claude-haiku-4-5"
ROLE_MODELS = {"fast": [FAST], "standard": [STANDARD], "power": [POWER]}


@pytest.fixture(autouse=True)
def _utc_pod(monkeypatch):
    monkeypatch.setattr(live_spend, "pod_tz_or_local", lambda: timezone.utc)


def _turn(day_ago, minute, source, cost, *, model=POWER, **extra):
    t = {
        "ts": (NOW - timedelta(days=day_ago, minutes=minute)).isoformat(),
        "source": source, "channel": "telegram", "model": model,
        "cost": cost, "cost_source": "oc", "user_id": "u1",
        "input_tokens": 1000, "output_tokens": 500,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    t.update(extra)
    return t


def _fixture_week():
    turns = []
    for d in range(7):
        turns += [_turn(d, 10, "human", 1.00), _turn(d, 20, "human", 0.50),
                  _turn(d, 30, "human", 0.05, model=STANDARD),
                  _turn(d, 40, "classifier", 0.02, evolve_tag="preflight", model=FAST),
                  _turn(d, 50, "memory_flush", 0.30)]
    turns.append(_turn(3, 5, "human", 4.00))   # the week's largest turn
    return turns


def _rows(turns=None, **kw):
    kw.setdefault("tier_of_model", pw.model_to_tier(ROLE_MODELS))
    kw.setdefault("standard_model", STANDARD)
    return pw.build_daily_rows(turns or _fixture_week(), days=7, now=NOW, **kw)


def test_daily_row_sums_oc_cost_and_splits_the_pieces():
    rows = _rows()
    assert len(rows) == 7
    r = rows[-1]                       # NOW's own day
    assert r["total_usd"] == pytest.approx(1.00 + 0.50 + 0.05 + 0.02 + 0.30)
    assert r["cost_source"] == "oc"
    assert r["tiers"]["power"] == {"turns": 2, "usd": pytest.approx(1.50)}
    assert r["tiers"]["standard"]["turns"] == 1
    # Evolve's own call and the flush are not "what answered the person".
    assert r["tiers"]["fast"]["turns"] == 0
    assert r["housekeeping"] == {"turns": 1, "usd": pytest.approx(0.30)}
    assert r["overhead"]["evolve_calls"] == 1
    assert r["overhead"]["evolve_usd"] == pytest.approx(0.02)
    assert r["overhead"]["share"] == pytest.approx(0.02 / 1.87, abs=1e-5)


def test_largest_turn_is_the_days_single_biggest():
    rows = _rows()
    big = [r for r in rows if r["largest_turn"]["usd"] == 4.00]
    assert len(big) == 1 and big[0]["largest_turn"]["tier"] == "power"
    assert rows[-1]["largest_turn"]["usd"] == pytest.approx(1.00)


def test_unknown_tier_is_never_guessed():
    t = _turn(0, 10, "human", 1.0, model="other/mystery")
    r = _rows([t])[-1]
    assert r["tiers"]["unknown"]["turns"] == 1
    assert r["tiers"]["power"]["turns"] == 0


def test_row_model_role_wins_over_model_lookup():
    r = _rows([_turn(0, 10, "human", 1.0, model=POWER, model_role="fast")])[-1]
    assert r["tiers"]["fast"]["turns"] == 1
    assert pw.tier_of({"model_role": "max"}) == "other"


def test_idle_and_unreadable_are_different(monkeypatch):
    rows = _rows([_turn(0, 10, "human", 1.0)])
    assert rows[0]["turns"] == 0 and rows[0]["total_usd"] == 0.0   # idle day: a zero row
    import evolve_overhead as eo
    monkeypatch.setattr(eo, "load_turns", lambda *a, **k: None)
    report = pw.build_report(Path("/nonexistent"), {}, bot_ids=["bot-a"], now=NOW,
                             role_models={"bot-a": ROLE_MODELS})
    assert report["unreadable_bots"] == ["bot-a"] and report["bots"] == {}


# ── the estimate ────────────────────────────────────────────────────────────


def test_estimate_arithmetic_reprices_only_power_turns(monkeypatch):
    seen = []

    def fake(turn, model, catalog):
        seen.append(model)
        return 0.10                     # standard rung: ten cents a turn

    monkeypatch.setattr(pw, "_reprice", fake)
    r = _rows()[-1]
    # 2 power turns repriced to 0.10; standard (0.05), housekeeping (0.30) and
    # Evolve's call (0.02) stay at their actual cost.
    assert r["standard_default_estimate_usd"] == pytest.approx(0.20 + 0.05 + 0.30 + 0.02)
    assert set(seen) == {STANDARD}
    assert r["total_usd"] == pytest.approx(1.87)          # the actual is untouched


def test_estimate_keeps_actual_when_it_cannot_reprice(monkeypatch):
    monkeypatch.setattr(pw, "_reprice", lambda *a: None)
    r = _rows()[-1]
    assert r["standard_default_estimate_usd"] == pytest.approx(r["total_usd"])
    assert r["estimate_unpriced_turns"] == 2


def test_no_standard_model_means_no_estimate_change():
    r = _rows(standard_model=None)[-1]
    assert r["standard_default_estimate_usd"] == pytest.approx(r["total_usd"])


def test_estimate_prices_from_the_turns_own_tokens():
    # Real pricing path, no stub: one Power turn of 1M input tokens must reprice
    # lower on the cheaper standard model (or at worst equal) and never to None.
    t = _turn(0, 10, "human", 15.0, input_tokens=1_000_000, output_tokens=0)
    r = _rows([t])[-1]
    assert r["estimate_unpriced_turns"] in (0, 1)
    assert r["standard_default_estimate_usd"] <= 15.0


# ── the operator's reading ──────────────────────────────────────────────────


def test_reading_is_optional_and_never_inferred(tmp_path):
    assert pw.load_readings(tmp_path) == {}
    pw.set_reading(tmp_path, "2026-10-14", used_usd="12.5", ceiling_usd="100", now=NOW)
    pw.set_reading(tmp_path, "2026-10-15", used_usd=20)           # no ceiling
    rep = pw.build_report(tmp_path, {}, bot_ids=["a"], now=NOW,
                          turns_by_bot={"a": _fixture_week()},
                          role_models={"a": ROLE_MODELS})
    rd = rep["operator_readings"]
    assert set(rd) == {"2026-10-14", "2026-10-15"}                # the other five: absent
    assert rd["2026-10-15"]["ceiling_usd"] is None                # not copied from the 14th
    assert "2026-10-16" not in rd


def test_reading_validation(tmp_path):
    for bad in ({"day": "10/14", "used_usd": 1}, {"day": "2026-10-14", "used_usd": "abc"},
                {"day": "2026-10-14", "used_usd": -1},
                {"day": "2026-10-14", "used_usd": 1, "ceiling_usd": 0}):
        with pytest.raises(ValueError):
            pw.set_reading(tmp_path, **bad)
    assert pw.load_readings(tmp_path) == {}


def test_reading_accepts_pasted_dollar_strings(tmp_path):
    rec = pw.set_reading(tmp_path, "2026-10-14", used_usd="$1,234.50", ceiling_usd="$2,000")
    assert rec["used_usd"] == 1234.5 and rec["ceiling_usd"] == 2000.0


def test_fit_sentence_both_ways_and_missing_ceiling(tmp_path):
    rep = pw.build_report(tmp_path, {}, bot_ids=["a"], now=NOW,
                          turns_by_bot={"a": _fixture_week()}, role_models={"a": ROLE_MODELS})
    total = rep["pod"]["week"]["total_usd"]
    assert "cannot be stated" in pw.fit_sentence(rep)
    days = rep["pod"]["week"]["days"]
    # The ceiling is monthly; the week is compared with its prorated share.
    rep["operator_readings"] = {"2026-10-16": {"used_usd": 1, "ceiling_usd": (total + 50) * 30 / days}}
    assert "did fit" in pw.fit_sentence(rep) and "$50.00 under" in pw.fit_sentence(rep)
    rep["operator_readings"] = {"2026-10-16": {"used_usd": 1, "ceiling_usd": (total - 10) * 30 / days}}
    assert "did not fit" in pw.fit_sentence(rep) and "$10.00 over" in pw.fit_sentence(rep)
    # Under the MONTHLY ceiling but over the week's share of it: the week is
    # compared with ceiling × days / 30, so this reads "did not fit".
    monthly = total * 30 / days - 30  # ceiling > total, share = total - 30 * days/30 < total
    assert monthly > total
    rep["operator_readings"] = {"2026-10-16": {"used_usd": 1, "ceiling_usd": monthly}}
    s = pw.fit_sentence(rep)
    assert "did not fit" in s and "monthly pool" in s and f"{days}-day share" in s


# ── the route ───────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = Flask(__name__)
    net = tmp_path / "network.json"
    net.write_text('{"bots": {"bot-a": {}}}')
    monkeypatch.setattr(rpw, "load_network", lambda p: {"bots": {"bot-a": {}}})
    import evolve_config
    monkeypatch.setattr(evolve_config, "get_shared_dir", lambda n: tmp_path, raising=False)
    rpw._memo.update(at=0.0, report=None)
    rpw.register_pa_week_cost_routes(app, net)
    return app.test_client()


def test_reading_route_roundtrip_and_rejects_garbage(client, tmp_path):
    ok = client.post("/api/analytics/pa-week-cost/reading",
                     json={"day": "2026-10-14", "used_usd": 5, "ceiling_usd": 100})
    assert ok.status_code == 200 and ok.get_json()["reading"]["used_usd"] == 5.0
    bad = client.post("/api/analytics/pa-week-cost/reading", json={"day": "x", "used_usd": 5})
    assert bad.status_code == 400
    assert list(pw.load_readings(tmp_path)) == ["2026-10-14"]


def test_render_table_has_a_row_per_bot_and_the_pod(tmp_path):
    rep = pw.build_report(tmp_path, {}, bot_ids=["a", "b"], now=NOW,
                          turns_by_bot={"a": _fixture_week(), "b": _fixture_week()},
                          role_models={"a": ROLE_MODELS, "b": ROLE_MODELS})
    out = pw.render_table(rep)
    assert "| a |" in out and "| b |" in out and "**pod**" in out
    assert rep["pod"]["week"]["total_usd"] == pytest.approx(
        2 * rep["bots"]["a"]["week"]["total_usd"])
