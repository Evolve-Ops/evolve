"""D-CS2 — dollars come from OpenClaw's per-call ``usage.cost``, not Evolve's table.

The fixture is team-bot-a's UTC day 2026-09-12
(``internal/finding-cache-retention-doubled-the-bill-2026-09-12.md``), rebuilt
from the per-model sums of its transcript DBs as measured on the mini on
2026-09-26: every cache write that day went to the 1-hour tier, which OpenClaw
bills at 2x input and Evolve's single write rate priced at 1.25x.

    agent         model              in   out     cacheRead  cacheWrite(=1h)  OC cost
    main          claude-sonnet-4-6  168  15,467  2,179,089  1,199,161        $8.0812
    main          claude-haiku-4-5   131  11,177  2,886,116    469,220        $1.2831
    email-reader  claude-haiku-4-5    18      48          0     37,770        $0.0758
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cost_reconcile  # noqa: E402
import cost_rollup  # noqa: E402
import cost_truth  # noqa: E402
import model_pricing  # noqa: E402
import oc_usage_cost  # noqa: E402
import turn_cost  # noqa: E402

BOT = "team-bot-a"
DAY = date(2026, 9, 12)

#: OpenClaw's own rates ($/MTok: input, output, cacheRead, cacheWrite-1h) —
#: what ``calculateUsageCost`` charged; 1-hour writes at 2x input.
OC_RATES = {"claude-sonnet-4-6": (3.0, 15.0, 0.30, 6.0),
            "claude-haiku-4-5": (1.0, 5.0, 0.10, 2.0)}

# (agent, model, input, output, cacheRead, cacheWrite, cacheWrite1h, ts)
DAY_CALLS = [
    ("main", "claude-sonnet-4-6", 168, 15467, 2179089, 1199161, 1199161, "2026-09-12T19:10:00.000Z"),
    ("main", "claude-haiku-4-5", 131, 11177, 2886116, 469220, 469220, "2026-09-12T08:00:00.000Z"),
    ("email-reader", "claude-haiku-4-5", 18, 48, 0, 37770, 37770, "2026-09-12T06:00:00.000Z"),
]
DAY_OC_TOTAL = 9.4401        # 8.0812 + 1.2831 + 0.0758 (all three agent DBs)


def _oc_parts(model, i, o, cr, cw1h) -> dict:
    ri, ro, rr, rw = OC_RATES[model]
    return {"input": i * ri / 1e6, "output": o * ro / 1e6,
            "cacheRead": cr * rr / 1e6, "cacheWrite": cw1h * rw / 1e6}


def _catalog_doc() -> dict:
    """The pod's live catalog rows for the two models (1-hour rate absent,
    exactly as mirrored on 2026-09-26)."""
    def row(model, inp, out, cr, cw):
        return {
            "provider": "anthropic", "model_id": model,
            "input_cost_per_token": inp, "output_cost_per_token": out,
            "cache_read_cost_per_token": cr, "cache_write_cost_per_token": cw,
        }
    return {"refreshed_at": "2026-09-26T00:00:00Z", "models": [
        row("claude-sonnet-4-6", 3e-6, 15e-6, 0.3e-6, 3.75e-6),
        row("claude-haiku-4-5", 1e-6, 5e-6, 0.1e-6, 1.25e-6),
    ]}


def _write_catalog(shared_dir: Path, doc: dict | None = None) -> None:
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "model-pricing.json").write_text(json.dumps(doc or _catalog_doc()))


def _event(model, i, o, cr, cw, cw1h, cost, ts, *, parts=None) -> str:
    block = None
    if cost is not None:
        block = dict(parts or {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": cost})
        block["total"] = cost
    return json.dumps({
        "type": "message", "timestamp": ts,
        "message": {
            "role": "assistant", "model": model, "provider": "anthropic",
            "usage": {
                "input": i, "output": o, "cacheRead": cr, "cacheWrite": cw,
                "cacheWrite1h": cw1h, "totalTokens": i + o + cr + cw, "cost": block,
            },
        },
    })


def _write_db(home: Path, agent: str, rows: list[tuple[str, str]]) -> None:
    """``rows`` = [(session_id, event_json)]."""
    db = home / ".openclaw" / "agents" / agent / "agent" / "openclaw-agent.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS transcript_events (session_id TEXT NOT NULL, "
        "seq INTEGER NOT NULL, event_json TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "PRIMARY KEY (session_id, seq))"
    )
    start = conn.execute("SELECT COUNT(*) FROM transcript_events").fetchone()[0]
    for n, (sid, ev) in enumerate(rows):
        ts = json.loads(ev)["timestamp"]
        ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        conn.execute("INSERT INTO transcript_events VALUES (?,?,?,?)", (sid, start + n, ev, ms))
    conn.commit()
    conn.close()


def _write_day(home: Path) -> None:
    for n, (agent, model, i, o, cr, cw, cw1h, ts) in enumerate(DAY_CALLS):
        parts = _oc_parts(model, i, o, cr, cw1h)
        _write_db(home, agent, [(f"s{n}", _event(
            model, i, o, cr, cw, cw1h, sum(parts.values()), ts, parts=parts))])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Every bot's home is ``tmp_path/home`` — never a real ``~/.openclaw``."""
    home = tmp_path / "home"
    monkeypatch.delenv(oc_usage_cost.DISABLE_ENV, raising=False)
    import evolve_config
    monkeypatch.setattr(evolve_config, "bot_home", lambda _bot_id, *a, **k: home)
    oc_usage_cost.reset_cache()
    turn_cost.reset_pricing_catalog_cache()
    yield home
    oc_usage_cost.reset_cache()
    turn_cost.reset_pricing_catalog_cache()


# ── 1. Truth = OC usage.cost ──────────────────────────────────────────────────


def test_turn_cost_prefers_oc_usage_cost(_isolate):
    """A turn whose calls OC priced reads OC's figure — not the catalog's."""
    home = _isolate
    sid = "0bdca27c"
    _write_db(home, "main", [
        (sid, _event("claude-sonnet-4-6", 3, 765, 0, 32400, 32400, 0.20, "2026-09-12T19:08:57.876Z")),
        (sid, _event("claude-sonnet-4-6", 1, 410, 32400, 1141, 1141, 0.02, "2026-09-12T19:09:01.637Z")),
        # A later turn's call in the same session — must not join this turn.
        (sid, _event("claude-sonnet-4-6", 1, 8, 33541, 337, 337, 0.01, "2026-09-12T19:30:00.000Z")),
    ])
    turn = {
        "ts": "2026-09-12T19:09:01.700Z", "instance": BOT, "session_id": sid,
        "model": "claude-sonnet-4-6", "provider": "anthropic",
        "input_tokens": 4, "output_tokens": 1175,
        "cache_read_tokens": 32400, "cache_write_tokens": 33541,
        "cost": 0.14, "cost_source": "catalog",   # the table's 1.25x guess
    }
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    stamped = oc_usage_cost.enrich_turns([turn], [BOT], start, datetime(2026, 9, 13, tzinfo=timezone.utc))

    assert stamped == 1
    assert turn["cost_source"] == "oc"
    assert turn["cost_estimate"] == 0.14 and turn["cost_estimate_source"] == "catalog"
    cost, resolution = turn_cost.turn_cost_detail(turn, catalog=_catalog_doc())
    assert cost == pytest.approx(0.22)
    assert resolution == "oc" and turn_cost.cost_source_label(resolution) == "oc"

    # The spend daemon's own entry point reads the same figure.
    import spend_alert
    assert spend_alert._turn_cost(turn, catalog=_catalog_doc()) == pytest.approx(0.22)
    total = turn_cost.sum_turn_costs([turn], catalog=_catalog_doc())
    assert total.source == "oc" and total.oc_turns == 1


# ── 2. Estimate labelled when OC is absent ────────────────────────────────────


def test_estimate_labelled_when_oc_absent(_isolate):
    """No DB, or calls that do not reproduce the turn's tokens → estimate, said so."""
    turn = {
        "ts": "2026-09-12T12:00:00Z", "instance": BOT, "session_id": "s-x",
        "model": "claude-haiku-4-5", "provider": "anthropic",
        "input_tokens": 1000, "output_tokens": 100,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    end = datetime(2026, 9, 13, tzinfo=timezone.utc)

    # (a) no transcript DB at all
    assert oc_usage_cost.load_oc_calls(BOT, start, end) is None
    assert oc_usage_cost.enrich_turns([turn], [BOT], start, end) == 0
    cost, resolution = turn_cost.turn_cost_detail(turn, catalog=_catalog_doc())
    assert cost == pytest.approx(0.0015)
    assert turn_cost.cost_source_label(resolution) == "estimate"
    total = turn_cost.sum_turn_costs([turn], catalog=_catalog_doc())
    assert total.source == "estimate" and total.oc_turns == 0

    # (b) a DB whose calls cover only part of the turn (window edge) — the
    # partial sum is refused rather than under-reported as truth.
    _write_db(_isolate, "main", [("s-x", _event(
        "claude-haiku-4-5", 400, 40, 0, 0, 0, 0.0006, "2026-09-12T11:59:59Z"))])
    assert oc_usage_cost.enrich_turns([turn], [BOT], start, end) == 0
    assert "cost_source" not in turn

    # (c) OC's own 0.0 on real tokens is "no price", not "free".
    free = dict(turn, session_id="s-z")
    _write_db(_isolate, "main", [("s-z", _event(
        "claude-haiku-4-5", 1000, 100, 0, 0, 0, 0.0, "2026-09-12T11:59:59Z"))])
    oc_usage_cost.reset_cache()
    assert oc_usage_cost.enrich_turns([free], [BOT], start, end) == 0

    # (d) the label reaches the Usage summary the Cost page renders.
    import usage_analytics
    oc_turn = dict(turn, cost=0.5, cost_source="oc")
    summary = usage_analytics.compute_summary([turn, oc_turn])
    assert summary["cost_source"] == "mixed" and summary["oc_turns"] == 1
    assert summary["by_date"][0]["cost_source"] == "mixed"
    assert usage_analytics.compute_summary([turn])["cost_source"] == "estimate"


# ── 3. The 1-hour dimension + per-dimension reconcile ────────────────────────


def test_price_table_has_1h_rate_and_reconcile_compares_dimensions(_isolate, tmp_path):
    # LiteLLM publishes the tier under its own key; the catalog now keeps it.
    rows = model_pricing.normalize_litellm({"claude-sonnet-4-6": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 3e-6, "output_cost_per_token": 15e-6,
        "cache_creation_input_token_cost": 3.75e-6,
        "cache_creation_input_token_cost_above_1hr": 6e-6,
    }})
    assert rows[0].cache_write_1h_cost_per_token == 6e-6
    assert "cache_write_1h_cost_per_token" in rows[0].to_dict()

    # The live catalog lacks the field; the vendor multiple fills it (2x input).
    sonnet = DAY_CALLS[0]
    call_turn = {"model": "claude-sonnet-4-6", "provider": "anthropic",
                 "input_tokens": sonnet[2], "output_tokens": sonnet[3],
                 "cache_read_tokens": sonnet[4], "cache_write_tokens": sonnet[5],
                 "cache_write_1h_tokens": sonnet[6]}
    est = turn_cost.estimate_turn_cost(call_turn, catalog=_catalog_doc())
    assert est == pytest.approx(8.0812, abs=0.001)           # == OC's figure
    dims = turn_cost.estimate_dimensions(call_turn, catalog=_catalog_doc())
    assert dims is not None and set(dims) == set(oc_usage_cost.DIMENSIONS)
    assert dims["cache_write_1h"] == pytest.approx(1199161 * 6e-6)
    assert dims["cache_write_5m"] == 0.0

    # Reconcile against a console export that splits by token type.
    shared = tmp_path / "shared"
    _write_catalog(shared)
    _write_day(_isolate)
    csv_path = tmp_path / "console.csv"
    csv_path.write_text(
        "date,model,token_type,cost_usd\n"
        "2026-09-12,claude-sonnet-4-6,input,0.0005\n"
        "2026-09-12,claude-sonnet-4-6,output,0.2320\n"
        "2026-09-12,claude-sonnet-4-6,cache_read,0.6537\n"
        "2026-09-12,claude-sonnet-4-6,cache_creation_1h,7.1950\n"
        "2026-09-12,claude-haiku-4-5,output,0.0561\n"
        "2026-09-12,claude-haiku-4-5,cache_read,0.2886\n"
        "2026-09-12,claude-haiku-4-5,cache_creation_1h,1.0140\n"
    )
    result = cost_reconcile.reconcile(
        console_csv=csv_path, shared_dir=shared, provider="anthropic",
        bot_id=BOT, turns=[],
    )
    day = result.days[0]
    assert day.oc_usd is not None
    assert day.oc_usd == pytest.approx(DAY_OC_TOTAL, abs=0.001)
    assert day.oc_ratio == pytest.approx(1.0, abs=0.01)
    dims = day.dimensions_dict()
    assert set(dims) >= {"input", "output", "cache_read", "cache_write_1h"}
    # Each rate is compared on its own: OC, the table, and the console.
    assert dims["cache_write_1h"]["console"] == pytest.approx(8.2090)
    want_1h = (1199161 * 6 + (469220 + 37770) * 2) / 1e6
    assert dims["cache_write_1h"]["estimate"] == pytest.approx(want_1h, abs=1e-6)
    assert dims["cache_write_1h"]["oc"] == pytest.approx(want_1h, abs=1e-6)
    assert dims["cache_write_5m"]["oc"] == 0.0
    assert result.to_dict()["days"][0]["dimensions"]["cache_read"]["console"] == pytest.approx(0.9423)


# ── 4. The drift alarm ───────────────────────────────────────────────────────


def _day_calls() -> list:
    oc_usage_cost.reset_cache()
    calls = oc_usage_cost.load_oc_calls(
        BOT, datetime(2026, 9, 12, tzinfo=timezone.utc),
        datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    assert calls is not None
    return calls


def test_drift_alarm_fires_on_ten_percent(_isolate, tmp_path, monkeypatch):
    _write_day(_isolate)
    calls = _day_calls()
    assert sum(c.cost or 0 for c in calls) == pytest.approx(DAY_OC_TOTAL, abs=0.001)

    # With the 1-hour rate in the table the estimate matches OC: no alarm.
    assert cost_truth.estimate_vs_oc(BOT, calls, day="2026-09-12", catalog=_catalog_doc()) == []

    # The table as it stood on 2026-09-12 — one write rate, no 1-hour tier.
    monkeypatch.setattr(turn_cost, "CACHE_WRITE_1H_INPUT_MULT", {})
    findings = cost_truth.estimate_vs_oc(BOT, calls, day="2026-09-12", catalog=_catalog_doc())
    by_model = {f.model: f for f in findings}
    assert set(by_model) == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    sonnet = by_model["claude-sonnet-4-6"]
    assert sonnet.dimension == "cache_write_1h"
    assert sonnet.truth_usd == pytest.approx(8.0812, abs=0.001)
    assert sonnet.other_usd == pytest.approx(5.3831, abs=0.001)   # what Evolve said
    assert sonnet.ratio == pytest.approx(0.666, abs=0.002)
    assert "claude-sonnet-4-6" in sonnet.title() and "cache_write_1h" in sonnet.title()

    # The boundary is 10 %: a 9 % gap is quiet, an 11 % gap fires.
    monkeypatch.setattr(turn_cost, "CACHE_WRITE_1H_INPUT_MULT", {"anthropic": 2.0})
    def scaled(f):
        doc = _catalog_doc()
        for r in doc["models"]:
            for k in ("input_cost_per_token", "output_cost_per_token",
                      "cache_read_cost_per_token", "cache_write_cost_per_token"):
                r[k] *= f
        return doc
    assert cost_truth.estimate_vs_oc(BOT, calls, day="2026-09-12", catalog=scaled(1.09)) == []
    assert len(cost_truth.estimate_vs_oc(BOT, calls, day="2026-09-12", catalog=scaled(1.11))) == 2

    # OC vs the console, from a reconcile document.
    doc = {"provider": "anthropic", "days": [{
        "date": "2026-09-12", "oc_usd": 9.44, "console_usd": 11.00,
        "dimensions": {"output": {"oc": 0.29, "console": 0.29},
                       "cache_write_1h": {"oc": 8.21, "console": 9.77}},
    }]}
    (console,) = cost_truth.oc_vs_console(doc)
    assert console.dimension == "cache_write_1h" and console.kind == "oc_vs_console"

    # The daily entry point raises a FIRING Signal naming model + dimension,
    # and resolves it once the check comes back in band.
    monkeypatch.setattr(turn_cost, "CACHE_WRITE_1H_INPUT_MULT", {})
    shared = tmp_path / "shared"
    _write_catalog(shared)
    got = cost_truth.maybe_run_daily(
        shared, [BOT], datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc))
    assert got and {f.model for f in got} == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    firing = [json.loads(p.read_text()) for p in (shared / "signals" / "firing").glob("*.json")]
    assert all(s["type"] == cost_truth.SIGNAL_TYPE and s["state"] == "firing" for s in firing)
    named = {(s["details"]["model"], s["details"]["dimension"]) for s in firing}
    assert ("claude-sonnet-4-6", "cache_write_1h") in named
    # Once per day.
    assert cost_truth.maybe_run_daily(
        shared, [BOT], datetime(2026, 9, 13, 7, 0, tzinfo=timezone.utc)) is None

    monkeypatch.setattr(turn_cost, "CACHE_WRITE_1H_INPUT_MULT", {"anthropic": 2.0})
    cost_truth.emit(shared, cost_truth.check_day(shared, [BOT], DAY, reconcile_doc={}))
    assert not list((shared / "signals" / "firing").glob("*.json"))


def _firing_sigs(shared: Path) -> set[str]:
    return {json.loads(p.read_text())["signature"]
            for p in (shared / "signals" / "firing").glob("*.json")}


def test_drift_alarm_never_resolves_on_missing_data(_isolate, tmp_path, monkeypatch):
    """Hold 1 (pr-4492 review): no data is not "in band".

    An unreadable transcript DB must leave that bot's drift Signals firing,
    an empty reconcile must leave the console Signal firing, and a day on
    which no member's DB read must not write the day's flag.
    """
    shared = tmp_path / "shared"
    _write_catalog(shared)
    _write_day(_isolate)
    real_load, real_latest = oc_usage_cost.load_oc_calls, cost_reconcile.latest_reconcile
    other = "team-bot-b"
    console_drift = {"provider": "anthropic", "days": [{
        "date": "2026-09-12", "oc_usd": 9.44, "console_usd": 11.00,
        "dimensions": {"cache_write_1h": {"oc": 8.21, "console": 9.77}},
    }]}
    other_finding = cost_truth.DriftFinding(
        kind="estimate_vs_oc", day=DAY.isoformat(), model="claude-sonnet-4-6",
        truth_usd=2.0, other_usd=1.0, dimension="cache_write_1h", bot_id=other,
    )

    # Seed: BOT drifts on the 2026-09-12 table, the console drifts, and a
    # second bot has its own firing Signal.
    monkeypatch.setattr(turn_cost, "CACHE_WRITE_1H_INPUT_MULT", {})
    seeded = cost_truth.check_day(shared, [BOT], DAY, reconcile_doc=console_drift)
    assert seeded.read_bots == {BOT} and seeded.console_read
    cost_truth.emit(shared, seeded)
    cost_truth.emit(shared, cost_truth.DayCheck(findings=[other_finding], read_bots=frozenset({other})))
    firing = _firing_sigs(shared)
    kinds = {sig.split(":")[2] for sig in firing}
    assert kinds == {"estimate_vs_oc", "oc_vs_console"}
    assert other_finding.signature in firing and len(firing) == 4

    # Nothing readable: the loader returns None, the reconcile is empty.
    blind = cost_truth.check_day(shared, [BOT, other], DAY,
                                 load_calls=lambda *_a: None, reconcile_doc={})
    assert blind.findings == [] and blind.read_bots == frozenset() and not blind.console_read
    cost_truth.emit(shared, blind)
    assert _firing_sigs(shared) == firing

    # A raising loader is unreadable too.
    def boom(*_a):
        raise PermissionError("acl lost")
    cost_truth.emit(shared, cost_truth.check_day(shared, [BOT], DAY, load_calls=boom,
                                                 reconcile_doc={}))
    assert _firing_sigs(shared) == firing

    # The daily entry point: no DB readable → no flag, and the next tick retries.
    monkeypatch.setattr(oc_usage_cost, "load_oc_calls", lambda *_a, **_k: None)
    monkeypatch.setattr(cost_reconcile, "latest_reconcile", lambda _s: None)
    tick = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)
    flag = shared / "alerts" / "cost-truth-2026-09-12.flag"
    logged: list[str] = []
    assert cost_truth.maybe_run_daily(shared, [BOT, other], tick, log=logged.append) == []
    assert not flag.exists()
    assert cost_truth.maybe_run_daily(shared, [BOT, other], tick, log=logged.append) == []
    assert sum("will retry" in m for m in logged) == 1   # logged once, not per tick
    assert _firing_sigs(shared) == firing
    monkeypatch.setattr(oc_usage_cost, "load_oc_calls", real_load)
    monkeypatch.setattr(cost_reconcile, "latest_reconcile", real_latest)

    # Positive: BOT's DB reads and is back in band, the other bot's does not.
    # Only BOT's Signals resolve; the other bot's and the console's stand.
    monkeypatch.setattr(turn_cost, "CACHE_WRITE_1H_INPUT_MULT", {"anthropic": 2.0})
    partial = cost_truth.check_day(
        shared, [BOT, other], DAY,
        load_calls=lambda bot, *a: real_load(bot, *a) if bot == BOT else None,
        reconcile_doc={},
    )
    assert partial.findings == [] and partial.read_bots == {BOT}
    cost_truth.emit(shared, partial)
    left = _firing_sigs(shared)
    assert other_finding.signature in left
    assert {sig.split(":")[2] for sig in left} == {"estimate_vs_oc", "oc_vs_console"}
    assert not any(f":{BOT}:" in sig for sig in left)

    # Positive: a reconcile back in band resolves the console Signal — and
    # only it: the unread bot's Signal still stands.
    in_band = {"provider": "anthropic", "days": [{"date": "2026-09-13",
                                                  "oc_usd": 10.0, "console_usd": 10.2}]}
    cost_truth.emit(shared, cost_truth.check_day(
        shared, [other], DAY, load_calls=lambda *_a: None, reconcile_doc=in_band))
    assert _firing_sigs(shared) == {other_finding.signature}


# ── 5. Backfill ──────────────────────────────────────────────────────────────


def test_backfill_replaces_estimates(_isolate, tmp_path):
    shared = tmp_path / "shared"
    _write_catalog(shared)
    _write_day(_isolate)
    # The rollup on disk as Evolve wrote it on 2026-09-12: $6.50, estimated.
    path = shared / "metrics" / BOT / f"cost-{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 2, "bot_id": BOT,
                                "date": DAY.isoformat(), "total_usd": 6.503975}))
    # The cost_events behind it: the sonnet turn (its session IS in the
    # transcript, priced by the table at the 5-minute write rate), and a turn
    # OC billed but never persisted an assistant message for — the transcript
    # is not a complete ledger (measured 2026-09-25: 14 of one bot's 67 turns).
    sonnet = DAY_CALLS[0]
    events = [
        {"type": "cost_event", "ts": "2026-09-12T19:10:01Z", "session_id": "s0",
         "model": "claude-sonnet-4-6", "provider": "anthropic",
         "input_tokens": sonnet[2], "output_tokens": sonnet[3],
         "cache_read_tokens": sonnet[4], "cache_write_tokens": sonnet[5],
         "cost_usd": 5.3831},
        {"type": "cost_event", "ts": "2026-09-12T21:00:00Z", "session_id": "s-unpersisted",
         "model": "claude-haiku-4-5", "provider": "anthropic",
         "input_tokens": 100_000, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.1},
    ]
    ev_path = shared / "annotations" / BOT / f"cost_events-{DAY.isoformat()}.jsonl"
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    ev_path.write_text("".join(json.dumps(e) + "\n" for e in events))

    results = cost_rollup.backfill_from_oc(shared, [BOT], today=date(2026, 9, 26))
    assert len(results) == cost_rollup.OC_BACKFILL_DAYS
    doc = json.loads(path.read_text())
    assert doc["schema_version"] == cost_rollup.COST_ROLLUP_SCHEMA_VERSION
    # OC's figure for every call it holds, the table for the one it lost —
    # never a raw sum of calls that would drop that turn.
    assert doc["source"] == "mixed"
    assert doc["total_usd"] == pytest.approx(DAY_OC_TOTAL + 0.1, abs=0.001)
    assert doc["event_count"] == 4          # 2 events + 2 OC-only haiku calls
    assert doc["oc_calls"] == 3
    # What the table said, beside the truth: $5.38 for the sonnet turn.
    assert doc["estimate_usd"] == pytest.approx(5.3831 + 1.2831 + 0.0758 + 0.1, abs=0.01)
    assert doc["by_model"]["claude-sonnet-4-6"]["cost_usd"] == pytest.approx(8.0812, abs=0.001)

    # The routine 14-day pass extends itself to 30 exactly once.
    other = tmp_path / "shared2"
    _write_catalog(other)
    quiet = lambda _m: None  # noqa: E731
    first = cost_rollup.refresh_all(other, [BOT], days=14, today=date(2026, 9, 26), log_fn=quiet)
    again = cost_rollup.refresh_all(other, [BOT], days=14, today=date(2026, 9, 26), log_fn=quiet)
    assert len(first) == 30 and len(again) == 14
