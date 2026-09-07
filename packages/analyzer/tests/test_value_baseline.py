"""tests/test_value_baseline.py — value_baseline metrics + signal tests.

Spec: internal/spec-value-baseline-2026-06-10.md (slices B1 + B2).
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import value_baseline as vb  # noqa: E402
from signals import store as signals_store  # noqa: E402

ANCHOR = date(2026, 6, 9)


# ── fixtures ────────────────────────────────────────────────────────────────


def _write_metrics(shared: Path, bot: str, d: date, turn_count: int = 0,
                   app_usage: dict | None = None) -> None:
    out = shared / "metrics" / d.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{bot}.json").write_text(json.dumps({
        "turn_count": turn_count,
        "session_count": 1 if turn_count else 0,
        "app_usage": app_usage or {},
    }))


def _write_events(shared: Path, bot: str, d: date, kinds: list[str]) -> None:
    out = shared / "annotations" / bot
    out.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"trigger_kind": k}) for k in kinds]
    (out / f"cost_events-{d.isoformat()}.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else "")
    )


def _seed_window(shared: Path, bot: str, *, anchor: date = ANCHOR,
                 days: int = 56, kinds_by_offset: dict[int, list[str]] | None = None,
                 turn_count_by_offset: dict[int, int] | None = None) -> None:
    """Write `days` consecutive daily metrics files ending at `anchor`.

    ``kinds_by_offset[i]`` = cost-event trigger kinds for the day
    ``anchor - i`` (also sets that day's turn_count to len(kinds) unless
    overridden). Days without an entry get turn_count 0 and no events
    file — measurable, inactive.
    """
    kinds_by_offset = kinds_by_offset or {}
    turn_count_by_offset = turn_count_by_offset or {}
    for i in range(days):
        d = anchor - timedelta(days=i)
        kinds = kinds_by_offset.get(i)
        turns = turn_count_by_offset.get(i, len(kinds) if kinds else 0)
        _write_metrics(shared, bot, d, turn_count=turns)
        if kinds is not None:
            _write_events(shared, bot, d, kinds)


# ── day-level tri-state classification (spec §4.3) ──────────────────────────


def test_day_without_metrics_file_is_unmeasurable(tmp_path):
    facts = vb.classify_day(tmp_path, "bot_a", ANCHOR)
    assert facts.measurable is False


def test_day_with_turns_but_no_cost_events_file_is_unmeasurable(tmp_path):
    """Activity happened but nothing can attribute it — must not count
    as active OR inactive."""
    _write_metrics(tmp_path, "bot_a", ANCHOR, turn_count=7)
    facts = vb.classify_day(tmp_path, "bot_a", ANCHOR)
    assert facts.measurable is False


def test_day_with_turns_and_empty_cost_events_file_is_unmeasurable(tmp_path):
    """An events file with zero readable events is as blind as no file."""
    _write_metrics(tmp_path, "bot_a", ANCHOR, turn_count=7)
    _write_events(tmp_path, "bot_a", ANCHOR, [])
    facts = vb.classify_day(tmp_path, "bot_a", ANCHOR)
    assert facts.measurable is False


def test_quiet_day_without_events_file_is_measurable_inactive(tmp_path):
    """turn_count == 0 needs no cross-check — a quiet day is a real
    (measured) zero, not a measurement gap."""
    _write_metrics(tmp_path, "bot_a", ANCHOR, turn_count=0)
    facts = vb.classify_day(tmp_path, "bot_a", ANCHOR)
    assert facts.measurable is True
    assert facts.human_events == 0
    assert facts.cron_app_events == 0


def test_measurable_day_counts_user_turns_and_cron_apps(tmp_path):
    _write_metrics(tmp_path, "bot_a", ANCHOR, turn_count=5)
    _write_events(tmp_path, "bot_a", ANCHOR,
                  ["user_turn", "user_turn", "cron_app", "heartbeat", "subagent"])
    facts = vb.classify_day(tmp_path, "bot_a", ANCHOR)
    assert facts.measurable is True
    assert facts.human_events == 2
    assert facts.cron_app_events == 1  # heartbeat + subagent excluded


# ── window metrics + measurability floor ────────────────────────────────────


def test_window_below_floor_is_null_not_zero(tmp_path):
    """22 of 28 measurable days < the 80% floor → metric value is None.
    Null must never be coerced to 0."""
    # 22 measurable days inside the window (offsets 0..21), the rest of
    # the window missing. One old file pushes age past the gate.
    for i in range(22):
        _write_metrics(tmp_path, "bot_a", ANCHOR - timedelta(days=i))
    _write_metrics(tmp_path, "bot_a", ANCHOR - timedelta(days=40))
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    m = entry["active_human_days_28d"]
    assert m["measurable_days"] == 22
    assert m["value"] is None
    assert entry["utilization_state"] == "unmeasurable"
    assert "22 of the last 28" in entry["state_reason"]


def test_window_at_floor_produces_values(tmp_path):
    """23 of 28 measurable days meets the floor (23/28 ≥ 0.8)."""
    for i in range(23):
        _write_metrics(tmp_path, "bot_a", ANCHOR - timedelta(days=i))
    _write_metrics(tmp_path, "bot_a", ANCHOR - timedelta(days=40))
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    m = entry["active_human_days_28d"]
    assert m["measurable_days"] == 23
    assert m["value"] == 0


def test_active_human_days_is_day_granular(tmp_path):
    """Ten user turns in one day = 1 active day — structurally
    volume-insensitive (spec §2.1)."""
    _seed_window(tmp_path, "bot_a", kinds_by_offset={3: ["user_turn"] * 10})
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["active_human_days_28d"]["value"] == 1
    assert entry["active_human_days_7d"]["value"] == 1


def test_proactive_runs_counts_events_not_days(tmp_path):
    _seed_window(tmp_path, "bot_a",
                 kinds_by_offset={2: ["cron_app", "cron_app", "cron_app"]})
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["proactive_runs_28d"]["value"] == 3


def test_heartbeat_heavy_bot_is_still_underused(tmp_path):
    """A bot posting thousands of heartbeat turns serves no one — volume
    is activity, not value (spec §2.1)."""
    _seed_window(tmp_path, "bot_a",
                 kinds_by_offset={i: ["heartbeat"] * 24 for i in range(56)})
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["active_human_days_28d"]["value"] == 0
    assert entry["proactive_runs_28d"]["value"] == 0
    assert entry["utilization_state"] == "underused"


# ── app coverage ────────────────────────────────────────────────────────────


def test_app_coverage_null_when_no_apps(tmp_path):
    """No apps installed → coverage undefined, not 0%."""
    _seed_window(tmp_path, "bot_a")
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    cov = entry["app_coverage_28d"]
    assert cov["value"] is None
    assert cov["apps_total"] == 0


def test_app_coverage_fraction(tmp_path):
    apps_dir = tmp_path / "applications" / "bot_a"
    apps_dir.mkdir(parents=True)
    for name in ("app1", "app2", "app3", "app4"):
        (apps_dir / f"{name}.json").write_text("{}")
    _seed_window(tmp_path, "bot_a")
    _write_metrics(tmp_path, "bot_a", ANCHOR - timedelta(days=1),
                   app_usage={"app1": {"sessions": 2}, "app2": {"sessions": 1}})
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    cov = entry["app_coverage_28d"]
    assert cov["apps_total"] == 4
    assert cov["apps_used"] == 2
    assert cov["value"] == 0.5


# ── trend ───────────────────────────────────────────────────────────────────


def test_value_trend_delta(tmp_path):
    """Current 28d has 2 human days; prior 28d had 5 → delta -3."""
    kinds = {i: ["user_turn"] for i in (1, 2)}
    kinds.update({i: ["user_turn"] for i in (30, 33, 36, 39, 42)})
    _seed_window(tmp_path, "bot_a", kinds_by_offset=kinds)
    trend = vb.compute_bot_entry(tmp_path, "bot_a")["value_trend_28d"]
    assert trend == {"value": -3, "current": 2, "prior": 5}


def test_value_trend_null_without_prior_history(tmp_path):
    """Only 30 days of history → prior bucket below floor → trend null
    (the known current value is still reported)."""
    _seed_window(tmp_path, "bot_a", days=30,
                 kinds_by_offset={1: ["user_turn"]})
    trend = vb.compute_bot_entry(tmp_path, "bot_a")["value_trend_28d"]
    assert trend["value"] is None
    assert trend["prior"] is None
    assert trend["current"] == 1


# ── usage breadth ───────────────────────────────────────────────────────────


def test_usage_breadth_null_when_observations_absent(tmp_path):
    """No observations dir (incl. DNT opt-out) → null, never zero."""
    _seed_window(tmp_path, "bot_a")
    assert vb.compute_bot_entry(tmp_path, "bot_a")["usage_breadth_28d"] == {
        "value": None
    }


def test_usage_breadth_counts_distinct_noun_verb_cells(tmp_path):
    _seed_window(tmp_path, "bot_a")
    obs = tmp_path / "observations" / "bot_a"
    obs.mkdir(parents=True)
    day = ANCHOR - timedelta(days=2)
    (obs / f"{day.isoformat()}.jsonl").write_text("\n".join([
        json.dumps({"noun": "email", "verb": "summarize"}),
        json.dumps({"noun": "email", "verb": "summarize"}),  # dup cell
        json.dumps({"noun": "email", "verb": "draft"}),
        json.dumps({"noun": "calendar", "verb": "plan"}),
    ]))
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["usage_breadth_28d"]["value"] == 3


# ── utilization state (the single predicate, spec §5.2/§6.2/§6.3) ───────────


def test_young_bot_is_unmeasurable_not_underused(tmp_path):
    """First metrics file < 28 days old → onboarding, never 'underused'."""
    _seed_window(tmp_path, "bot_a", days=10)
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["age_days"] == 9
    assert entry["utilization_state"] == "unmeasurable"
    assert "too new" in entry["state_reason"]


def test_zero_zero_old_bot_is_underused(tmp_path):
    _seed_window(tmp_path, "bot_a")
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["age_days"] == 55
    assert entry["utilization_state"] == "underused"


def test_briefing_only_bot_is_active(tmp_path):
    """Negative control (spec §9.4b): zero human turns but a daily
    scheduled briefing — proactive runs protect it by construction."""
    _seed_window(tmp_path, "bot_a",
                 kinds_by_offset={i: ["cron_app"] for i in range(56)})
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["utilization_state"] == "active"


def test_low_volume_human_bot_is_active(tmp_path):
    """Negative control (spec §9.4a): one human interaction a week is a
    quiet success, not churn."""
    _seed_window(tmp_path, "bot_a",
                 kinds_by_offset={i: ["user_turn"] for i in (3, 10, 17, 24)})
    entry = vb.compute_bot_entry(tmp_path, "bot_a")
    assert entry["utilization_state"] == "active"


def test_bot_with_no_metrics_files_is_unmeasurable(tmp_path):
    entry = vb.compute_bot_entry(tmp_path, "bot_a", fallback_anchor=ANCHOR)
    assert entry["age_days"] is None
    assert entry["utilization_state"] == "unmeasurable"


# ── signals (slice B2) ──────────────────────────────────────────────────────


def _entry(state: str, *, age: int = 55, anchor: date = ANCHOR,
           measurable: int = 28) -> dict:
    return {
        "utilization_state": state,
        "age_days": age,
        "anchor_date": anchor.isoformat(),
        "active_human_days_28d": {
            "value": 0 if state == "underused" else 5,
            "measurable_days": measurable,
            "window_days": 28,
        },
    }


def _rollup(bots: dict) -> dict:
    return {
        "version": 1,
        "computed_at": "2026-06-10T01:00:00+00:00",
        "anchor_date": ANCHOR.isoformat(),
        "bots": bots,
    }


def _firing(shared: Path) -> list:
    return list(signals_store.iter_signals(shared, subdirs=("firing",)))


def test_underused_fires_bot_underused_signal(tmp_path):
    n_fired, _ = vb.fire_signals(
        tmp_path, _rollup({"bot_a": _entry("underused"), "bot_b": _entry("active")})
    )
    assert n_fired == 1
    sigs = _firing(tmp_path)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == "bot_underused"
    assert sig.producer == "value_baseline"
    assert sig.scope == "bot"
    assert sig.bot_id == "bot_a"
    assert sig.severity == "info"  # producer default from the registry
    assert sig.category == "hygiene"  # category default from the registry
    assert "bot_a" in sig.body


def test_unmeasurable_bot_never_fires(tmp_path):
    """An unmeasurable bot must never be reported as unused (§6.2).

    The unmeasurable bot is a minority here so the fleet-coverage signal
    stays out of the picture and the assertion isolates bot_underused.
    """
    n_fired, _ = vb.fire_signals(
        tmp_path, _rollup({
            "bot_a": _entry("unmeasurable", measurable=10),
            "bot_b": _entry("active"),
            "bot_c": _entry("active"),
        })
    )
    assert n_fired == 0
    assert _firing(tmp_path) == []


def test_recovered_bot_sweep_resolves(tmp_path):
    vb.fire_signals(tmp_path, _rollup({"bot_a": _entry("underused")}))
    assert len(_firing(tmp_path)) == 1
    _, n_resolved = vb.fire_signals(tmp_path, _rollup({"bot_a": _entry("active")}))
    assert n_resolved == 1
    assert _firing(tmp_path) == []
    archived = list(signals_store.iter_signals(tmp_path, subdirs=("archived",)))
    assert len(archived) == 1
    assert archived[0].state == "resolved"


def test_refire_bumps_instead_of_duplicating(tmp_path):
    """Signature dedup: at most one active Signal per bot, ever."""
    rollup = _rollup({"bot_a": _entry("underused")})
    vb.fire_signals(tmp_path, rollup)
    vb.fire_signals(tmp_path, rollup)
    sigs = _firing(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].observation_count == 2


def test_coverage_signal_when_majority_of_old_fleet_unmeasurable(tmp_path):
    rollup = _rollup({
        "bot_a": _entry("active"),
        "bot_b": _entry("unmeasurable", measurable=3),
        "bot_c": _entry("unmeasurable", measurable=0),
    })
    vb.fire_signals(tmp_path, rollup)
    sigs = _firing(tmp_path)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == "value_baseline_coverage"
    assert sig.scope == "pod"
    assert sig.severity == "warn"
    assert "bot_b" in sig.body and "bot_c" in sig.body


def test_no_coverage_signal_for_young_fleet(tmp_path):
    """A fresh pod is onboarding, not broken — age-gated bots don't count
    toward the fleet-coverage warning."""
    rollup = _rollup({
        "bot_a": _entry("unmeasurable", age=5),
        "bot_b": _entry("unmeasurable", age=3),
    })
    n_fired, _ = vb.fire_signals(tmp_path, rollup)
    assert n_fired == 0
    assert _firing(tmp_path) == []


def test_operator_copy_passes_plex_test(tmp_path):
    """No internal vocabulary in operator-facing strings (spec §7.3)."""
    banned = ("baseline", "signal", "producer", "tri-state", "measurable")
    specs = [
        vb._underused_signal_spec("bot_a", _entry("underused")),
        vb._coverage_signal_spec(tmp_path, ["bot_a", "bot_b"], 3),
    ]
    for spec in specs:
        copy = " ".join([
            spec["title"], spec["body"],
            spec["details"]["what_it_means"], spec["details"]["fix_steps"],
        ]).lower()
        for word in banned:
            assert word not in copy, f"{word!r} in operator copy: {copy[:80]}"
        assert len(spec["title"]) <= 80


def test_registration_entries():
    """Spec §6.6 steps 1+2 — explicit registry entries, not fallbacks."""
    from schema.signal import PRODUCER_CATEGORY_DEFAULT
    from signals.producer_severity import PRODUCER_SEVERITY
    assert PRODUCER_SEVERITY["value_baseline"] == "info"
    assert PRODUCER_CATEGORY_DEFAULT["value_baseline"] == "hygiene"


# ── rollup file + retention + nightly entrypoint ────────────────────────────


def test_run_nightly_writes_rollup_and_fires(tmp_path):
    _seed_window(tmp_path, "bot_idle")
    _seed_window(tmp_path, "bot_busy",
                 kinds_by_offset={i: ["user_turn"] for i in range(0, 28, 3)})
    rollup = vb.run_nightly(tmp_path, ["bot_idle", "bot_busy"], today=ANCHOR)

    out = tmp_path / "metrics" / "value" / f"{ANCHOR.isoformat()}.json"
    assert out.exists()
    on_disk = json.loads(out.read_text())
    assert on_disk["anchor_date"] == ANCHOR.isoformat()
    assert on_disk["bots"]["bot_idle"]["utilization_state"] == "underused"
    assert on_disk["bots"]["bot_busy"]["utilization_state"] == "active"
    assert rollup["bots"].keys() == on_disk["bots"].keys()

    sigs = _firing(tmp_path)
    assert [s.bot_id for s in sigs] == ["bot_idle"]


def test_rollup_retention_prunes_old_files(tmp_path):
    value_dir = tmp_path / "metrics" / "value"
    value_dir.mkdir(parents=True)
    old = ANCHOR - timedelta(days=100)
    recent = ANCHOR - timedelta(days=30)
    (value_dir / f"{old.isoformat()}.json").write_text("{}")
    (value_dir / f"{recent.isoformat()}.json").write_text("{}")
    (value_dir / "not-a-date.json").write_text("{}")
    removed = vb.prune_rollups(tmp_path, today=ANCHOR)
    assert [p.stem for p in removed] == [old.isoformat()]
    assert not (value_dir / f"{old.isoformat()}.json").exists()
    assert (value_dir / f"{recent.isoformat()}.json").exists()
    assert (value_dir / "not-a-date.json").exists()  # never guess at non-date files


# ── rank table (proof artifact, spec §9) ────────────────────────────────────


def test_rank_table_orders_underused_last(tmp_path):
    def full_entry(state, human, runs):
        e = _entry(state)
        e["active_human_days_28d"]["value"] = human
        e["proactive_runs_28d"] = {"value": runs, "measurable_days": 28,
                                   "window_days": 28}
        e["app_coverage_28d"] = {"value": None, "apps_total": 0, "apps_used": 0}
        e["value_trend_28d"] = {"value": None, "current": None, "prior": None}
        return e

    table = vb.render_rank_table(_rollup({
        "bot_idle": full_entry("underused", 0, 0),
        "bot_daily": full_entry("active", 20, 5),
        "bot_weekly": full_entry("active", 4, 0),
        "bot_new": full_entry("unmeasurable", None, None),
    }))
    order = [line.split()[0] for line in table.splitlines()[2:6]]
    assert order == ["bot_daily", "bot_weekly", "bot_new", "bot_idle"]


# ── rollup readers (slice B3 surfacing: tile + Value view) ──────────────────


def _write_rollup_file(shared: Path, anchor: date, body: dict | str) -> Path:
    value_dir = shared / "metrics" / "value"
    value_dir.mkdir(parents=True, exist_ok=True)
    path = value_dir / f"{anchor.isoformat()}.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


def test_load_latest_rollup_returns_newest(tmp_path):
    _write_rollup_file(tmp_path, ANCHOR - timedelta(days=1),
                       _rollup({"bot_a": _entry("active")}))
    newest = _rollup({"bot_a": _entry("underused")})
    newest["anchor_date"] = ANCHOR.isoformat()
    _write_rollup_file(tmp_path, ANCHOR, newest)
    got = vb.load_latest_rollup(tmp_path)
    assert got is not None
    assert got["anchor_date"] == ANCHOR.isoformat()
    assert got["bots"]["bot_a"]["utilization_state"] == "underused"


def test_load_latest_rollup_skips_corrupt_newest(tmp_path):
    """An interrupted write must not blank the view when an older rollup
    is still readable."""
    _write_rollup_file(tmp_path, ANCHOR - timedelta(days=1),
                       _rollup({"bot_a": _entry("active")}))
    _write_rollup_file(tmp_path, ANCHOR, "{truncated")
    got = vb.load_latest_rollup(tmp_path)
    assert got is not None
    assert got["bots"]["bot_a"]["utilization_state"] == "active"


def test_load_latest_rollup_none_when_absent(tmp_path):
    assert vb.load_latest_rollup(tmp_path) is None


def test_rollup_is_stale_by_computed_at():
    from datetime import datetime, timezone

    rollup = _rollup({})  # computed_at 2026-06-10T01:00:00+00:00
    fresh_now = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)   # +32h
    stale_now = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)   # +56h
    assert vb.rollup_is_stale(rollup, now=fresh_now) is False
    assert vb.rollup_is_stale(rollup, now=stale_now) is True


def test_rollup_is_stale_falls_back_to_anchor_date():
    from datetime import datetime, timezone

    rollup = _rollup({})
    del rollup["computed_at"]
    # anchor 2026-06-09; written nightly on the 10th → fresh through the 11th
    assert vb.rollup_is_stale(
        rollup, now=datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)
    ) is False
    assert vb.rollup_is_stale(
        rollup, now=datetime(2026, 6, 13, 9, 0, tzinfo=timezone.utc)
    ) is True


def test_rollup_is_stale_when_unparseable():
    """"Can't tell how old" must not render as "fresh"."""
    assert vb.rollup_is_stale({"computed_at": 42, "anchor_date": "garbage"}) is True


def test_rank_bots_matches_table_order(tmp_path):
    def full_entry(state, human, runs):
        e = _entry(state)
        e["active_human_days_28d"]["value"] = human
        e["proactive_runs_28d"] = {"value": runs, "measurable_days": 28,
                                   "window_days": 28}
        return e

    rollup = _rollup({
        "bot_idle": full_entry("underused", 0, 0),
        "bot_daily": full_entry("active", 20, 5),
        "bot_briefing": full_entry("active", 20, 30),  # runs break the tie
        "bot_new": full_entry("unmeasurable", None, None),
    })
    assert [b for b, _ in vb.rank_bots(rollup)] == [
        "bot_briefing", "bot_daily", "bot_new", "bot_idle",
    ]
    assert vb.rank_bots({"bots": "not-a-dict"}) == []


# ── measure.py post-step wiring (spec §5.1 Option A) ────────────────────────


def test_measure_full_run_invokes_value_baseline(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"members": ["bot_a"], "sharedDir": str(shared)}))

    calls: list = []
    monkeypatch.setattr(
        vb, "run_nightly",
        lambda sd, bots, **kw: calls.append((Path(sd), sorted(bots))) or {},
    )
    import measure
    monkeypatch.setattr(sys, "argv", [
        "measure.py", "--network", str(net), "--shared-dir", str(shared),
        "--date", ANCHOR.isoformat(),
    ])
    measure.main()
    assert calls == [(shared, ["bot_a", "evolve"])]


def test_measure_single_bot_run_skips_post_step(tmp_path, monkeypatch):
    """A --bot-id run must not write a partial pod rollup or sweep other
    bots' signals."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"members": ["bot_a"], "sharedDir": str(shared)}))

    calls: list = []
    monkeypatch.setattr(vb, "run_nightly",
                        lambda *a, **kw: calls.append(a) or {})
    import measure
    monkeypatch.setattr(sys, "argv", [
        "measure.py", "--network", str(net), "--shared-dir", str(shared),
        "--date", ANCHOR.isoformat(), "--bot-id", "bot_a",
    ])
    measure.main()
    assert calls == []
