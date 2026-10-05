"""The Evolve overhead ledger, breaker and hook-rate signal (D-OH5).

Brief ``evolve-overhead-ledger-and-budget``. Every test here is a fixture pod:
a week of turn rows in the shape the plugin writes (``source`` / ``cost`` /
``cost_source`` / ``evolve_key`` / ``evolve_tag``), no network, no real shared
dir. Bot names are role placeholders.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import evolve_overhead as eo  # noqa: E402
import live_spend  # noqa: E402
import spend_attribution as sa  # noqa: E402

NOW = datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc)
ROUTER_KEY = "agent:main:explicit:evolve:preflight:1788000000000"


@pytest.fixture(autouse=True)
def _utc_pod(monkeypatch):
    """Pod-local day == UTC day, so the fixture's days are unambiguous."""
    monkeypatch.setattr(live_spend, "pod_tz_or_local", lambda: timezone.utc)


def _turn(ago_min: float, source: str, cost: float, *, user_id=None, channel="telegram",
          tag=None, key=None, model="anthropic/claude-sonnet-4-5", now=NOW,
          role="primary_user") -> dict:
    t = {
        "ts": (now - timedelta(minutes=ago_min)).isoformat(),
        "source": source, "channel": channel, "model": model,
        "cost": cost, "cost_source": "oc",
        "input_tokens": 100, "output_tokens": 100,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "user_id": user_id,
    }
    if role is not None and source == "human":
        t["speaker_role"] = role          # the plugin stamps this on every turn row
    if tag:
        t["evolve_tag"] = tag
        t["evolve_key"] = key or f"agent:main:explicit:evolve:{tag}:1"
    return t


def _week(bot_spend=1.0, per_day=10, ev_per_day=2, ev_cost=0.01, user="u1") -> list[dict]:
    """Seven quiet days: ``per_day`` user turns and ``ev_per_day`` Evolve calls."""
    out: list[dict] = []
    for d in range(7):
        for i in range(per_day):
            out.append(_turn(d * 1440 + 600 + i, "human", bot_spend / per_day, user_id=user))
        for i in range(ev_per_day):
            out.append(_turn(d * 1440 + 700 + i, "classifier", ev_cost, tag="preflight"))
    return out


# ── 1. The number ───────────────────────────────────────────────────────────


def test_rollup_by_kind_and_tag(tmp_path):
    turns = _week()
    turns += [_turn(30, "summarizer", 0.02, tag="session-summary") for _ in range(3)]
    turns += [_turn(20, "classifier", 0.01, tag="tier-classifier") for _ in range(2)]
    led = eo.compute_bot(tmp_path, "bot-a", now=NOW, turns=turns)
    d7 = led["d7"]
    assert d7["evolve_calls"] == 7 * 2 + 3 + 2
    kinds = d7["by_kind"]
    assert kinds["classifier"]["calls"] == 7 * 2 + 2
    assert kinds["summarizer"]["calls"] == 3
    assert kinds["classifier"]["tags"]["preflight"]["calls"] == 14
    assert kinds["classifier"]["tags"]["tier-classifier"]["calls"] == 2
    assert kinds["summarizer"]["usd"] == pytest.approx(0.06)
    assert len(led["days"]) == 7
    assert led["d1"]["evolve_calls"] == 2 + 3 + 2          # today only


def test_share_is_overhead_over_total_spend(tmp_path):
    turns = _week(bot_spend=1.0, per_day=10, ev_per_day=2, ev_cost=0.01)
    d7 = eo.compute_bot(tmp_path, "bot-a", now=NOW, turns=turns)["d7"]
    # 7 x ($1.00 user + $0.02 evolve) = $7.14 total; evolve = $0.14.
    assert d7["total_usd"] == pytest.approx(7.14)
    assert d7["evolve_usd"] == pytest.approx(0.14)
    assert d7["share"] == pytest.approx(0.14 / 7.14, rel=1e-4)
    assert d7["over_target"] is False                      # 1.96% < 5%
    assert d7["evolve_cost_source"] == "oc"


def test_share_over_target_is_flagged_and_labelled_estimate_for_context(tmp_path):
    turns = _week(ev_per_day=40, ev_cost=0.01)
    d7 = eo.compute_bot(tmp_path, "bot-a", now=NOW, turns=turns)["d7"]
    assert d7["over_target"] is True
    assert d7["context_cost_source"] == "estimate"


def test_injected_context_is_tokens_times_turns_times_cached_input_price(tmp_path):
    from turn_cost import estimate_dimensions

    fp = tmp_path / "bot-a" / "turns"
    fp.mkdir(parents=True)
    (fp / "context-footprint.json").write_text(json.dumps({
        "total_chars": 22_000,
        "profiles": {"no_live_speaker": {"total_chars": 8_000}},
    }))
    turns = [_turn(10, "human", 0.5, user_id="u1"), _turn(20, "human", 0.5, user_id="u1"),
             _turn(30, "heartbeat", 0.1)]
    led = eo.compute_bot(tmp_path, "bot-a", now=NOW, turns=turns)
    assert led["footprint_tokens"] == {"full": 5500, "background": 2000}
    rate = estimate_dimensions({"model": "anthropic/claude-sonnet-4-5",
                                "cache_read_tokens": 1_000_000})["cache_read"]
    want = (2 * 5500 + 2000) * rate / 1_000_000
    assert led["d7"]["context_tokens"] == 2 * 5500 + 2000
    assert led["d7"]["context_usd"] == pytest.approx(want, rel=1e-4)


def test_no_footprint_means_no_context_figure_not_a_zero_claim(tmp_path):
    led = eo.compute_bot(tmp_path, "bot-a", now=NOW, turns=_week())
    assert led["footprint_tokens"] is None
    assert led["d7"]["context_turns"] == 0


def test_unreadable_turns_is_none_not_an_idle_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(eo, "load_turns", lambda *a, **k: None)
    assert eo.compute_bot(tmp_path, "bot-a", now=NOW) is None


def test_pod_sums_bots(tmp_path):
    a = eo.compute_bot(tmp_path, "bot-a", now=NOW, turns=_week())
    b = eo.compute_bot(tmp_path, "bot-b", now=NOW, turns=_week(ev_per_day=0))
    pod = eo.compute_pod({"bot-a": a, "bot-b": b}, share_max=0.05)
    assert pod["d7"]["evolve_calls"] == a["d7"]["evolve_calls"]
    assert pod["d7"]["total_usd"] == pytest.approx(a["d7"]["total_usd"] + b["d7"]["total_usd"])


def test_evolve_kinds_match_context_health():
    from context_health import EVOLVE_TRIGGER_KINDS

    assert eo.EVOLVE_KINDS == EVOLVE_TRIGGER_KINDS


# ── 6. Attributed vs unattributed ───────────────────────────────────────────


def test_human_speaker_is_attributed_everything_else_is_not():
    human = _turn(5, "human", 1.0, user_id="123")
    assert sa.is_attributed(human)
    assert sa.speaker_of(human) == "telegram:123"
    assert not sa.is_attributed(_turn(5, "heartbeat", 1.0))
    assert not sa.is_attributed(_turn(5, "cron", 1.0))
    assert not sa.is_attributed(_turn(5, "classifier", 1.0, tag="preflight"))
    assert not sa.is_attributed(_turn(5, "human", 1.0, user_id=None))      # no speaker
    assert not sa.is_attributed(_turn(5, "human", 1.0, user_id="unknown"))


def test_known_role_must_be_a_user_role_unknown_role_is_unattributed():
    t = _turn(5, "human", 1.0, user_id="9")
    r = _turn(5, "human", 1.0, user_id="9", role=None)
    assert sa.is_attributed(r, role_of=lambda s: "primary_user")
    assert sa.is_attributed(r, role_of=lambda s: "user")
    assert not sa.is_attributed(r, role_of=lambda s: "blocked")
    # An unknown role buys no exemption: it fails toward the tight cap.
    bare = _turn(5, "human", 1.0, user_id="9", role=None)
    assert "speaker_role" not in bare and not sa.is_attributed(bare)
    assert not sa.is_attributed(bare, role_of=lambda s: None)
    assert not sa.is_attributed(bare, role_of=lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    assert not sa.is_attributed(bare, role_of=lambda s: "admin")
    assert not sa.is_attributed({**t, "speaker_role": "participant"})
    assert not sa.is_attributed({**t, "speaker_role": "blocked"})
    assert sa.is_attributed({**t, "speaker_role": "primary_user"})


def test_participant_and_roleless_turns_stay_under_the_tight_cap_primary_rides_the_window():
    turns = [_turn(10, "human", 4.0, user_id="p", role="participant"),
             _turn(11, "human", 3.0, user_id="n", role=None),
             _turn(12, "human", 5.0, user_id="o", role="primary_user")]
    s = sa.split_spend(turns)
    assert s["attributed_usd"] == pytest.approx(5.0)
    assert s["unattributed_usd"] == pytest.approx(7.0)


def test_split_spend_and_the_ledger_carry_both_numbers(tmp_path):
    turns = [_turn(10, "human", 3.0, user_id="u1"), _turn(20, "human", 2.0, user_id="u1"),
             _turn(30, "heartbeat", 0.5), _turn(40, "cron", 0.25)]
    s = sa.split_spend(turns)
    assert s["attributed_usd"] == pytest.approx(5.0)
    assert s["unattributed_usd"] == pytest.approx(0.75)
    d7 = eo.compute_bot(tmp_path, "bot-a", now=NOW, turns=turns)["d7"]
    assert d7["attributed_usd"] == pytest.approx(5.0)
    assert d7["unattributed_usd"] == pytest.approx(0.75)


def test_window_origin_never_reaches_back_past_a_resume():
    origin = NOW - timedelta(hours=2)
    assert sa.window_start(NOW, 7, origin) == origin
    assert sa.window_start(NOW, 7, None) == NOW - timedelta(days=7)
    assert sa.window_start(NOW, 1, NOW - timedelta(days=3)) == NOW - timedelta(days=1)


# ── 2. The breaker ──────────────────────────────────────────────────────────


def _loop_hour(n=40, users=3) -> list[dict]:
    t = _week()
    t += [_turn(30 - i * 0.2, "classifier", 0.02, tag="preflight", key=ROUTER_KEY)
          for i in range(n)]
    t += [_turn(25 + i, "human", 0.3, user_id="u1") for i in range(users)]
    return t


CFG = eo.OverheadConfig()


def test_breaker_trips_on_calls_per_user_turn_and_names_the_caller_in_words():
    v = eo.evaluate_hour(_loop_hour(n=40, users=3), now=NOW, cfg=CFG)
    assert v["trip"] is True
    assert "calls_per_user_turn" in v["reasons"]
    assert v["evolve_calls"] == 40 and v["user_turns"] == 3
    assert v["top_session_key"] == ROUTER_KEY
    assert v["top_prefix_words"] == "Evolve's own routing calls"
    assert v["top_count"] == 40


def test_breaker_trips_on_share_alone_when_calls_per_turn_is_fine():
    cfg = eo.OverheadConfig({"calls_per_user_turn_max": 50, "min_calls": 5})
    turns = [_turn(30 - i, "classifier", 0.05, tag="preflight") for i in range(10)]
    turns += [_turn(20 + i, "human", 0.5, user_id="u1") for i in range(10)]
    v = eo.evaluate_hour(turns, now=NOW, cfg=cfg)
    assert v["reasons"] == ["share"]                       # $0.50 of $5.50 = 9%
    assert v["trip"] is True


def test_quiet_bot_and_noise_floors_do_not_trip():
    assert eo.evaluate_hour(_week(), now=NOW, cfg=CFG)["trip"] is False
    # four calls, no user turns: ratio is huge but below the noise floor
    few = [_turn(10 + i, "classifier", 0.05, tag="preflight") for i in range(4)]
    assert eo.evaluate_hour(few, now=NOW, cfg=CFG)["trip"] is False


def test_disabled_never_trips():
    cfg = eo.OverheadConfig({"enabled": False})
    assert eo.evaluate_hour(_loop_hour(), now=NOW, cfg=cfg)["trip"] is False


def test_targets_come_from_network_json_with_defaults_in_code():
    assert (CFG.share_max, CFG.calls_per_user_turn_max) == (0.05, 1.0)
    cfg = eo.OverheadConfig.from_network(
        {"evolve": {"overhead": {"share_max": 0.2, "calls_per_user_turn_max": 3}}})
    assert (cfg.share_max, cfg.calls_per_user_turn_max) == (0.2, 3.0)
    junk = eo.OverheadConfig.from_network({"evolve": {"overhead": {"share_max": "x", "min_calls": -1}}})
    assert junk.share_max == 0.05 and junk.min_calls == 5


def _network():
    return {"bots": {"bot-a": {}, "bot-b": {}}}


def test_cycle_trips_the_right_bot_writes_the_record_and_pauses_nothing_else(tmp_path):
    turns = {"bot-a": _loop_hour(), "bot-b": _week()}
    res = eo.run_cycle(tmp_path, _network(), now=NOW, turns_by_bot=turns)
    assert res["tripped"] == ["bot-a"]
    rec = json.loads(eo.breaker_path(tmp_path, "bot-a").read_text())
    assert rec["type"] == "evolve_overhead" and rec["initiated_by"] == "auto"
    assert rec["detail"]["bot_keeps_answering"] is True
    assert rec["detail"]["top_session_key"] == ROUTER_KEY
    assert rec["detail"]["top_prefix_words"] == "Evolve's own routing calls"
    assert rec["detail"]["top_count"] == 40
    assert "Evolve's own routing calls: 40 calls in the last hour" in rec["reason"]
    assert not eo.breaker_path(tmp_path, "bot-b").exists()
    # It is not a cost breaker: nothing that enforces one can see it.
    from breakers import store

    assert store.list_all(tmp_path) == []
    assert not (tmp_path / "breakers" / "bot-a" / "cost.json").exists()
    assert not (tmp_path / "spend-caps").exists()
    assert json.loads(eo.ledger_path(tmp_path).read_text())["bots"].keys() == {"bot-a", "bot-b"}


def test_a_standing_trip_is_not_re_judged_or_rewritten(tmp_path):
    eo.run_cycle(tmp_path, _network(), now=NOW, turns_by_bot={"bot-a": _loop_hour()})
    first = eo.breaker_path(tmp_path, "bot-a").read_text()
    res = eo.run_cycle(tmp_path, _network(), now=NOW + timedelta(minutes=10),
                       turns_by_bot={"bot-a": _loop_hour()})
    assert res["tripped"] == [] and eo.breaker_path(tmp_path, "bot-a").read_text() == first


def test_trip_expires_and_can_retry(tmp_path):
    eo.run_cycle(tmp_path, _network(), now=NOW, turns_by_bot={"bot-a": _loop_hour()})
    later = NOW + timedelta(hours=5)
    assert eo.read_breaker(tmp_path, "bot-a", now=later) is None
    res = eo.run_cycle(tmp_path, _network(), now=later, turns_by_bot={"bot-a": _week()})
    assert res["expired"] == ["bot-a"] and not eo.breaker_path(tmp_path, "bot-a").exists()


def test_unreadable_trip_file_reads_as_tripped(tmp_path):
    p = eo.breaker_path(tmp_path, "bot-a")
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    rec = eo.read_breaker(tmp_path, "bot-a", now=NOW)
    assert rec is not None and rec["unreadable"] is True


# ── 5. Resume counts from now ───────────────────────────────────────────────


def test_resume_rebases_the_window_no_retrip_on_accepted_calls(tmp_path):
    turns = {"bot-a": _loop_hour()}
    eo.run_cycle(tmp_path, _network(), now=NOW, turns_by_bot=turns)
    resume_at = NOW + timedelta(minutes=5)
    accepted = eo.resume(tmp_path, "bot-a", by="admin:op", reason="understood", now=resume_at)
    assert accepted["was_tripped"] and accepted["accepted_calls"] == 40
    assert not eo.breaker_path(tmp_path, "bot-a").exists()
    # The same hour of turns is still on disk: it must not re-trip.
    res = eo.run_cycle(tmp_path, _network(), now=resume_at + timedelta(minutes=1),
                       turns_by_bot=turns)
    assert res["tripped"] == []
    # ...but a fresh loop after the resume still does (the origin moves, the ceiling doesn't).
    fresh = turns["bot-a"] + [
        _turn(-6 - i * 0.1, "classifier", 0.02, tag="preflight", key=ROUTER_KEY)
        for i in range(30)]
    res = eo.run_cycle(tmp_path, _network(), now=resume_at + timedelta(minutes=10),
                       turns_by_bot={"bot-a": fresh})
    assert res["tripped"] == ["bot-a"]


def test_card_says_what_is_paused_and_that_the_bot_answers(tmp_path):
    eo.run_cycle(tmp_path, _network(), now=NOW, turns_by_bot={"bot-a": _loop_hour()})
    card = eo.card_for(tmp_path, "bot-a", now=NOW)
    assert card["tripped"] and "still answering" in card["headline"]
    assert card["top_prefix_words"] == "Evolve's own routing calls"
    assert card["top_session_key"] == ROUTER_KEY and card["top_count"] == 40
    assert any("routing" in p for p in card["paused"])
    eo.resume(tmp_path, "bot-a", by="admin:op", now=NOW + timedelta(minutes=1))
    after = eo.card_for(tmp_path, "bot-a", now=NOW + timedelta(minutes=2))
    assert after["tripped"] is False and after["resumed_at"]


# ── 4. Hook-fire rate ───────────────────────────────────────────────────────


def _fires(shared: Path, bot: str, day, hour_counts: dict[str, int]):
    d = shared / bot / "turns"
    d.mkdir(parents=True, exist_ok=True)
    hours = {h: {"count": n, "other": 0, "keys": {}} for h, n in hour_counts.items()}
    (d / f"hook-fires-{day.isoformat()}.json").write_text(
        json.dumps({"schema_version": 1, "bot_id": bot, "date": day.isoformat(), "hours": hours}))


def test_hook_rate_signal_on_a_ten_x_hour(tmp_path):
    hh = f"{NOW.hour:02d}"
    for i in range(1, 8):
        _fires(tmp_path, "bot-a", (NOW - timedelta(days=i)).date(), {hh: 12})
    d = tmp_path / "bot-a" / "turns"
    prefix = "You are routing an AI request to the right model tier for response quality."
    (d / f"hook-fires-{NOW.date().isoformat()}.json").write_text(json.dumps({
        "schema_version": 1, "bot_id": "bot-a", "date": NOW.date().isoformat(),
        "hours": {hh: {"count": 461, "other": 0, "keys": {
            ROUTER_KEY: {"n": 455, "prefix": prefix},
            "agent:main:telegram:direct:1": {"n": 6, "prefix": "hello"}}}}}))
    f = eo.hook_rate_check(tmp_path, "bot-a", now=NOW, cfg=CFG)
    assert f is not None
    assert f["count"] == 461 and f["median"] == 12 and f["multiple"] == pytest.approx(38.4)
    assert f["top_session_key"] == ROUTER_KEY
    assert f["top_prefix_words"] == "Evolve's own routing calls"
    res = eo.run_cycle(tmp_path, {"bots": {"bot-a": {}}}, now=NOW, turns_by_bot={"bot-a": _week()})
    assert [x["bot_id"] for x in res["hook_rate"]] == ["bot-a"]
    from signals import store as sigs

    firing = [s for s in sigs.iter_active(tmp_path) if s.type == "evolve_hook_fire_rate"]
    assert len(firing) == 1 and firing[0].flavor == "maintenance"
    assert ROUTER_KEY in firing[0].body and "Evolve's own routing calls" in firing[0].body


def test_hook_rate_quiet_cold_start_and_floor(tmp_path):
    hh = f"{NOW.hour:02d}"
    # no baseline at all: cannot compare, says nothing
    _fires(tmp_path, "bot-a", NOW.date(), {hh: 500})
    assert eo.hook_rate_check(tmp_path, "bot-a", now=NOW, cfg=CFG) is None
    for i in range(1, 8):
        _fires(tmp_path, "bot-a", (NOW - timedelta(days=i)).date(), {hh: 12})
    # 10x of 12 = 120: 100 is not over it
    _fires(tmp_path, "bot-a", NOW.date(), {hh: 100})
    assert eo.hook_rate_check(tmp_path, "bot-a", now=NOW, cfg=CFG) is None
    # a median of 0 does not make 5 fires an incident (absolute floor)
    for i in range(1, 8):
        _fires(tmp_path, "bot-a", (NOW - timedelta(days=i)).date(), {hh: 0})
    _fires(tmp_path, "bot-a", NOW.date(), {hh: 5})
    assert eo.hook_rate_check(tmp_path, "bot-a", now=NOW, cfg=CFG) is None


def test_describe_prompt_quotes_what_it_cannot_name():
    assert eo.describe_prompt("hello there", None) == 'a prompt starting "hello there"'
    assert eo.describe_prompt("x [evolve:internal-model-call] y") == "an Evolve-internal prompt"
    assert eo.describe_prompt("", "agent:main:explicit:evolve:session-summary:9") == \
        "Evolve's session-summary calls"


# ── Receipt ─────────────────────────────────────────────────────────────────


def test_receipt_line_shows_share_target_and_labels_the_estimate(tmp_path, monkeypatch):
    monkeypatch.setattr(eo, "load_turns", lambda bot, **k: _week(ev_per_day=2) if bot == "bot-a"
                        else _week(ev_per_day=40))
    lines = eo.receipt_lines(tmp_path, ["bot-a", "bot-b"], now=NOW)
    assert len(lines) == 1
    assert "Evolve's own overhead" in lines[0] and "target ≤5%" in lines[0]
    assert "(estimate)" in lines[0] and "highest: bot-b" in lines[0]


def test_receipt_line_says_not_measured_never_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(eo, "load_turns", lambda *a, **k: None)
    line = eo.receipt_lines(tmp_path, ["bot-a"], now=NOW)[0]
    assert "not measured" in line and "$0" not in line


def test_render_table_has_a_row_per_bot_and_the_pod(tmp_path):
    led = eo.build_ledger(tmp_path, ["bot-a"], now=NOW, turns_by_bot={"bot-a": _week()})
    table = eo.render_table(led)
    assert "| bot-a |" in table and "**pod**" in table


# ── 6 (continued). The cap measures unattributed spend; the window is wide ──


def test_daily_cap_decision_exempts_a_persons_spend_until_the_window_is_spent():
    from spend_alert import daily_cap_decision
    from live_spend import DaySpend

    day = DaySpend(usd=30.0, priced_turns=10)
    ladder = {"tier_downgrade": None, "l1_breaker": 20.0, "l2_breaker": None}
    # $28 of the $30 is a person working: the tight cap sees $2.
    d = daily_cap_decision(day, threshold=99, ladder=ladder, attributed_usd=28.0)
    assert d["effective_usd"] == pytest.approx(2.0) and d["tripped"] == []
    assert d["attributed_usd"] == 28.0 and d["unattributed_usd"] == pytest.approx(2.0)
    # Same day, but no speaker on any of it: trips exactly as before.
    d = daily_cap_decision(day, threshold=99, ladder=ladder)
    assert d["effective_usd"] == 30.0 and d["tripped"] == ["l1_breaker"]
    # The person has spent the whole wide window: the exemption lapses.
    d = daily_cap_decision(day, threshold=99, ladder=ladder, attributed_usd=28.0,
                           window_exceeded=True)
    assert d["effective_usd"] == 30.0 and d["tripped"] == ["l1_breaker"]
    # Loop spend with no speaker still trips on a day a person is also working.
    d = daily_cap_decision(DaySpend(usd=50.0, priced_turns=3), threshold=99, ladder=ladder,
                           attributed_usd=28.0)
    assert d["unattributed_usd"] == pytest.approx(22.0) and d["tripped"] == ["l1_breaker"]


def test_l2_measures_raw_spend_attributed_spend_cannot_hide_a_runaway():
    from spend_alert import daily_cap_decision
    from live_spend import DaySpend

    ladder = {"tier_downgrade": None, "l1_breaker": 20.0, "l2_breaker": 40.0}
    # $60 in one day, all of it a person's turns: the soft rung is exempted,
    # the hard rung is not.
    d = daily_cap_decision(DaySpend(usd=60.0, priced_turns=9), threshold=99, ladder=ladder,
                           attributed_usd=60.0)
    assert d["effective_usd"] == 0.0 and d["tripped"] == ["l2_breaker"]
    # The acceptance marker still moves L2's origin.
    d = daily_cap_decision(DaySpend(usd=60.0, priced_turns=9), threshold=99, ladder=ladder,
                           accepted_usd=30.0, attributed_usd=60.0)
    assert d["tripped"] == []


def test_acceptance_and_attribution_are_not_applied_twice():
    from spend_alert import daily_cap_decision
    from live_spend import DaySpend

    ladder = {"tier_downgrade": None, "l1_breaker": 20.0, "l2_breaker": None}
    # $30 day, $25 accepted at reactivation, $10 of it a person's: the smaller
    # measure wins — never $30 - $25 - $10 clamped, never double-discounted.
    d = daily_cap_decision(DaySpend(usd=30.0, priced_turns=4), threshold=99, ladder=ladder,
                           accepted_usd=25.0, attributed_usd=10.0)
    assert d["effective_usd"] == pytest.approx(5.0)


def test_attribution_for_reads_window_only_when_a_person_spent_today(tmp_path, monkeypatch):
    calls: list[int] = []

    def fake_load(bot, *, days, end=None, log=None):
        calls.append(days)
        return [_turn(10, "heartbeat", 1.0)] if days == 2 else []

    monkeypatch.setattr(live_spend, "load_live_turns", fake_load)
    out = sa.attribution_for("bot-a", today=NOW.date(), now=NOW)
    assert calls == [2] and out["attributed_usd"] == 0.0
    assert out["window_attributed_usd"] is None        # "not measured", not $0

    calls.clear()
    week = [_turn(10, "human", 4.0, user_id="u1"),
            _turn(3 * 1440, "human", 6.0, user_id="u1")]
    monkeypatch.setattr(live_spend, "load_live_turns",
                        lambda bot, *, days, end=None, log=None: (calls.append(days), week)[1])
    out = sa.attribution_for("bot-a", today=NOW.date(), now=NOW)
    assert calls == [2, 8]
    assert out["attributed_usd"] == pytest.approx(4.0)
    assert out["window_attributed_usd"] == pytest.approx(10.0)


def test_attribution_unreadable_turns_means_no_exemption(monkeypatch):
    monkeypatch.setattr(live_spend, "load_live_turns",
                        lambda *a, **k: live_spend.LIVE_LOAD_FAILED)
    assert sa.attribution_for("bot-a", today=NOW.date(), now=NOW) is None


def test_reactivation_origin_stops_the_window_reaching_back(tmp_path, monkeypatch):
    import spend_caps

    week = [_turn(10, "human", 4.0, user_id="u1"),
            _turn(3 * 1440, "human", 6.0, user_id="u1")]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **k: week)
    origin = spend_caps.write_window_origin(tmp_path, "bot-a", now=NOW - timedelta(hours=1))
    assert spend_caps.read_window_origin(tmp_path, "bot-a") == origin
    out = sa.attribution_for("bot-a", today=NOW.date(), now=NOW,
                             origin=spend_caps.read_window_origin(tmp_path, "bot-a"))
    assert out["window_attributed_usd"] == pytest.approx(4.0)     # the $6 predates it
    assert spend_caps.read_window_origin(tmp_path, "nobody") is None


def test_apply_attribution_writes_the_card_snapshot_and_lapses_at_the_window_cap(tmp_path, monkeypatch):
    import spend_alert
    import spend_caps
    from live_spend import DaySpend

    week = [_turn(10, "human", 15.0, user_id="u1"),
            _turn(1440, "human", 130.0, user_id="u1")]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **k: week)
    day = DaySpend(usd=15.0, priced_turns=1)
    attributed, exceeded = spend_alert._apply_attribution(
        tmp_path, "bot-a", NOW.date(), now=NOW, day_spend=day, l1_cap=20.0,
        thresholds={}, exempt_subkinds=None)
    assert attributed == pytest.approx(15.0)
    assert exceeded is True                       # $145 >= $20 x 7 days
    snap = spend_caps.read_attribution_snapshot(tmp_path, "bot-a", NOW.date())
    assert snap["window_exceeded"] is True and snap["window_cap_usd"] == 140.0
    assert snap["attributed_usd"] == 15.0 and snap["unattributed_usd"] == 0.0
    # no L1 cap to scale the window by: no exemption at all
    attributed, exceeded = spend_alert._apply_attribution(
        tmp_path, "bot-a", NOW.date(), now=NOW, day_spend=day, l1_cap=None,
        thresholds={}, exempt_subkinds=None)
    assert exceeded is False


def test_breaker_card_carries_both_numbers(tmp_path):
    import spend_caps
    from breakers.store import trip

    rec = trip(shared_dir=tmp_path, scope="bot-a", breaker_type="cost", duration=None,
               initiated_by="auto", reason="cap")
    spend_caps.write_attribution_snapshot(tmp_path, "bot-a", spend_caps._pod_today(), {
        "attributed_usd": 12.0, "unattributed_usd": 3.0, "window_days": 7,
        "window_attributed_usd": 40.0, "window_cap_usd": 140.0, "window_exceeded": False})
    entry = spend_caps.breaker_ui_entry(rec, tmp_path)
    assert entry["attribution"]["attributed_usd"] == 12.0
    assert entry["attribution"]["unattributed_usd"] == 3.0


def test_weekly_summary_receipt_carries_the_overhead_line(tmp_path, monkeypatch):
    """The line is wired into the real receipt, not just renderable."""
    import spend_alert

    monkeypatch.setattr(eo, "load_turns", lambda bot, **k: _week(ev_per_day=2))
    sent: dict = {}
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **kw: sent.update(kw) or True)
    monkeypatch.setattr(spend_alert, "_weekly_spend", lambda *a, **k: (7.14, {"bot-a": 7.14}))
    monkeypatch.setattr(spend_alert, "_per_bot_receipt_lines", lambda *a, **k: ["  bot-a: $7.14"])
    monkeypatch.setattr(spend_alert, "emit_estimate_drift", lambda **k: False)
    monkeypatch.setattr(spend_alert, "_accuracy_lines", lambda *a, **k: [])
    monkeypatch.setattr(spend_alert, "_heartbeat_lines", lambda *a, **k: [])
    monkeypatch.setattr(spend_alert, "_cache_shape_lines", lambda *a, **k: [])
    spend_alert._maybe_send_weekly_summary(
        tmp_path, ["bot-a"], NOW.date(), 100.0, {"bots": {"bot-a": {}}})
    text = sent["payload"]["per_bot_breakdown"]
    assert "Evolve's own overhead" in text and "target ≤5%" in text
