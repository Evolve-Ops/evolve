"""Replay of the 2026-05-20 spike against the burst detector.

The original incident: security_bot hit $16.29 and team_bot_a hit $12.78 in a single
UTC day; the old spend_alert daemon ran 24 hourly invocations and logged
``no metrics for 2026-05-20 — skipping`` for every one of them because
it read end-of-day aggregate files that didn't exist yet.

These tests pin the *new* contract:

  - load_today_spend reads live turn JSONL via usage_analytics, so it
    returns a meaningful number during the day (not None / 0)
  - burst_window_spend sums cost across a rolling minute window
  - the detector fires within one polling cycle once a bot crosses
    the configured burst threshold (default $5 in 60 min)
  - severity escalates to ``alert`` at ``critical_multiplier × threshold``
  - the cost_burst Signal carries top-2 sessions by cost so the operator
    sees which sessions are responsible

Replay fixtures live in ``tests/fixtures/spike-2026-05-20/``. They are
the literal turn JSONL files from the mini (sudo-copied from
``/Users/Shared/evolve/{security_bot,team_bot_a}/turns/turns-2026-05-20.jsonl``).
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spend_alert  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures" / "spike-2026-05-20"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _stage_turns(tmp_path: Path, bot: str) -> Path:
    """Lay out a /Users/Shared/evolve-style tree under tmp_path with one bot's
    turn JSONL from the fixtures dir."""
    src = _FIXTURES / f"{bot}-turns-2026-05-20.jsonl"
    if not src.exists():
        pytest.skip(f"fixture missing: {src} — run scripts/fetch_spike_fixtures.sh")
    dst_dir = tmp_path / bot / "turns"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst_dir / "turns-2026-05-20.jsonl")
    return tmp_path


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    """Stage security_bot + team_bot_a JSONL under tmp_path; monkey-patch
    usage_analytics._find_turns_dirs to look at tmp_path."""
    _stage_turns(tmp_path, "security_bot")
    _stage_turns(tmp_path, "team_bot_a")

    # usage_analytics resolves turn dirs via resolve_bot_paths (which reads
    # network.json + openclaw.json on the host). Short-circuit that and
    # point straight at our staged fixtures so the test is hermetic.
    import usage_analytics as _ua

    def fake_find_turns_dirs(bot_id: str, network_path: str | None = None):
        return [tmp_path / bot_id / "turns"]

    monkeypatch.setattr(_ua, "_find_turns_dirs", fake_find_turns_dirs)
    return tmp_path


# ── load_today_spend (live JSONL) ───────────────────────────────────────────


def test_load_today_spend_sums_live_jsonl(replay_env):
    # End of day 2026-05-20, security_bot total is $16.29 (per the incident
    # report). The detector should see that running sum when we
    # evaluate at 23:59 UTC on 2026-05-20.
    now = datetime(2026, 5, 20, 23, 59, tzinfo=timezone.utc)
    spent = spend_alert.load_today_spend(
        replay_env, "security_bot", now.date(), now=now
    )
    # Allow rounding wobble — the recorded costs sum to about $16.29.
    assert spent is not None
    assert 16.0 < spent < 17.0, f"expected ~$16.29, got ${spent:.4f}"


def test_load_today_spend_during_the_day(replay_env):
    # Mid-afternoon UTC on 2026-05-20 — should only count turns up to that
    # point, NOT silently return 0 or None like the legacy metrics-file path.
    now = datetime(2026, 5, 20, 19, 10, tzinfo=timezone.utc)
    spent = spend_alert.load_today_spend(
        replay_env, "security_bot", now.date(), now=now
    )
    # By 19:10 security_bot has already spent its early-morning + 19:05+ bursts
    # (cumulative is several dollars). Anything > $2 confirms we're not
    # silently zeroing.
    assert spent is not None and spent > 2.0, (
        f"intraday should reflect live spend; got ${spent}"
    )


# ── burst_window_spend ─────────────────────────────────────────────────────


def test_burst_window_first_crosses_threshold_around_1910(replay_env):
    """The security_bot burst first crosses the $5/60-min default at 19:10 UTC.

    Verifies the sliding-window math against the literal turn timestamps
    in the fixture. Before this PR the daemon had no concept of a rolling
    window at all — every check was against the end-of-day total."""
    # 19:05 — just before the bursty turns start landing
    cost_at_19_05, _ = spend_alert.burst_window_spend(
        "security_bot",
        now=datetime(2026, 5, 20, 19, 5, tzinfo=timezone.utc),
        window_minutes=60,
    )
    assert cost_at_19_05 < 5.0

    # 19:10 — burst should have crossed $5
    cost_at_19_10, turns = spend_alert.burst_window_spend(
        "security_bot",
        now=datetime(2026, 5, 20, 19, 10, tzinfo=timezone.utc),
        window_minutes=60,
    )
    assert cost_at_19_10 >= 5.0, (
        f"security_bot should cross $5 by 19:10; saw ${cost_at_19_10:.2f}"
    )
    assert len(turns) > 0


def test_burst_window_team_bot_a_crosses_threshold_around_2015(replay_env):
    """The team_bot_a burst first crosses the $5/60-min default at ~20:15 UTC."""
    cost_at_19_55, _ = spend_alert.burst_window_spend(
        "team_bot_a",
        now=datetime(2026, 5, 20, 19, 55, tzinfo=timezone.utc),
        window_minutes=60,
    )
    assert cost_at_19_55 < 5.0

    cost_at_20_15, _ = spend_alert.burst_window_spend(
        "team_bot_a",
        now=datetime(2026, 5, 20, 20, 15, tzinfo=timezone.utc),
        window_minutes=60,
    )
    assert cost_at_20_15 >= 5.0, (
        f"team_bot_a should cross $5 by 20:15; saw ${cost_at_20_15:.2f}"
    )


def test_top_sessions_carries_session_metadata(replay_env):
    """The burst Signal needs top-2 sessions so the alert can name names."""
    _, turns = spend_alert.burst_window_spend(
        "security_bot",
        now=datetime(2026, 5, 20, 19, 35, tzinfo=timezone.utc),
        window_minutes=60,
    )
    top = spend_alert._top_sessions(turns, n=2)
    assert len(top) >= 1
    leader = top[0]
    assert leader["cost"] > 0
    assert leader["channel"] != ""
    # Session IDs are truncated to 8 chars; the runaway session prefix is
    # ``51bac282`` per the fixture.
    assert leader["session_id"] == "51bac282", (
        f"expected security_bot's runaway session to lead; saw {leader}"
    )


# ── Severity ladder ────────────────────────────────────────────────────────


def test_burst_warn_emits_warn_severity_signal(replay_env, monkeypatch):
    """At cost between $X and 3×$X, the Signal severity should be warn."""
    captured: list = []

    def fake_observe(shared_dir, **kw):
        captured.append(kw)
        # Return enough of a Signal to satisfy the caller
        from types import SimpleNamespace
        return SimpleNamespace(id="sig-warn")

    import signals.store as _store
    monkeypatch.setattr(_store, "observe", fake_observe)

    rid = spend_alert.emit_burst_signal(
        shared_dir=replay_env,
        bot_id="security_bot",
        cost_usd=6.0,         # > $5 threshold, < $15 critical
        threshold_usd=5.0,
        critical_multiplier=3.0,
        window_minutes=60,
        top_sessions=[{"session_id": "51bac282", "channel": "heartbeat",
                       "cost": 4.30, "turns_count": 20}],
        now=datetime(2026, 5, 20, 19, 10, tzinfo=timezone.utc),
    )
    assert rid == "sig-warn"
    assert captured[0]["severity"] == "warn"
    assert captured[0]["type"] == "cost_burst"
    assert captured[0]["details"]["top_sessions"][0]["session_id"] == "51bac282"


def test_burst_critical_emits_alert_severity_signal(replay_env, monkeypatch):
    """At cost >= 3×$X, the Signal severity should be alert."""
    captured: list = []
    import signals.store as _store
    from types import SimpleNamespace
    monkeypatch.setattr(
        _store, "observe",
        lambda shared_dir, **kw: (captured.append(kw), SimpleNamespace(id="sig-crit"))[1],
    )
    spend_alert.emit_burst_signal(
        shared_dir=replay_env,
        bot_id="security_bot",
        cost_usd=16.30,        # ≥ 3 × $5
        threshold_usd=5.0,
        critical_multiplier=3.0,
        window_minutes=60,
        top_sessions=[{"session_id": "51bac282", "channel": "heartbeat",
                       "cost": 8.70, "turns_count": 40}],
        now=datetime(2026, 5, 20, 19, 35, tzinfo=timezone.utc),
    )
    assert captured[0]["severity"] == "alert"


def test_burst_signature_includes_hour_bucket(replay_env, monkeypatch):
    """Same hour → same signature (dedup); next hour → new signature."""
    captured: list = []
    import signals.store as _store
    from types import SimpleNamespace
    monkeypatch.setattr(
        _store, "observe",
        lambda shared_dir, **kw: (captured.append(kw), SimpleNamespace(id="sig"))[1],
    )

    for t in (
        datetime(2026, 5, 20, 19, 10, tzinfo=timezone.utc),
        datetime(2026, 5, 20, 19, 50, tzinfo=timezone.utc),  # same hour bucket
        datetime(2026, 5, 20, 20, 5, tzinfo=timezone.utc),   # new hour bucket
    ):
        spend_alert.emit_burst_signal(
            shared_dir=replay_env, bot_id="security_bot",
            cost_usd=6.0, threshold_usd=5.0, critical_multiplier=3.0,
            window_minutes=60, top_sessions=[], now=t,
        )

    sigs = [c["signature"] for c in captured]
    # First two in the same hour bucket dedup to the same signature; the
    # third is a fresh hour bucket so signatures[1] == signatures[0] and
    # signatures[2] != signatures[0].
    assert sigs[0] == sigs[1]
    assert sigs[2] != sigs[0]


def test_burst_below_threshold_emits_no_signal(replay_env, monkeypatch):
    """Sub-threshold cost should NOT create a signal."""
    captured: list = []
    import signals.store as _store
    monkeypatch.setattr(_store, "observe", lambda *a, **kw: captured.append(kw))
    out = spend_alert.emit_burst_signal(
        shared_dir=replay_env, bot_id="security_bot",
        cost_usd=2.0,          # < $5 threshold
        threshold_usd=5.0, critical_multiplier=3.0,
        window_minutes=60, top_sessions=[],
        now=datetime(2026, 5, 20, 18, 30, tzinfo=timezone.utc),
    )
    assert out is None
    assert captured == []


# ── send_burst_alert (catalog event routing) ───────────────────────────────


def test_burst_alert_routes_through_catalog_warn(replay_env, monkeypatch):
    """A non-critical burst should dispatch to cost.burst_detected."""
    captured: list = []

    def fake_dispatch(**kw):
        captured.append(kw)
        return True
    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)

    sent = spend_alert.send_burst_alert(
        "security_bot", 6.0, 5.0,
        critical_multiplier=3.0, window_minutes=60,
        top_sessions=[
            {"session_id": "51bac282", "channel": "heartbeat", "cost": 4.30},
            {"session_id": "aae2c6bf", "channel": "heartbeat", "cost": 1.20},
        ],
        shared_dir=replay_env, network={}, hour_bucket="2026-05-20T19",
    )
    assert sent is True
    call = captured[0]
    assert call["catalog_event"] == "cost.burst_detected"
    assert call["severity_name"] == "warning"
    assert call["payload"]["bot_id"] == "security_bot"
    assert call["payload"]["cost"] == 6.0
    assert call["payload"]["window_minutes"] == 60
    assert call["payload"]["top_session_id"] == "51bac282"
    assert "aae2c6bf" in call["payload"]["top2_line"]
    assert call["dedup_key"] == "spend_alert/burst/security_bot/2026-05-20T19"


def test_load_today_spend_returns_none_on_discovery_failure(monkeypatch):
    """Discovery failure must NOT silently look like "$0 — bot is fine."

    The 2026-05-20 incident root cause was load_today_spend returning a
    falsy value (None for missing file) and the daemon treating that
    interchangeably with "no spend yet" — printing "skipping" and
    moving on. The new contract: discovery failure → None;
    successful read with no rows → 0.0. The daemon's main() loop
    relies on this distinction to emit the self-failure Signal.
    """
    monkeypatch.setattr(
        spend_alert, "_load_live_turns",
        lambda *a, **kw: spend_alert._LIVE_LOAD_FAILED,
    )
    out = spend_alert.load_today_spend(
        Path("/nonexistent"), "security_bot", datetime(2026, 5, 20).date(),
        now=datetime(2026, 5, 20, 19, 10, tzinfo=timezone.utc),
    )
    assert out is None


def test_burst_window_returns_negative_on_discovery_failure(monkeypatch):
    """Same contract for burst_window_spend — discovery failure is
    distinguishable from 'no burst' via a negative cost sentinel."""
    monkeypatch.setattr(
        spend_alert, "_load_live_turns",
        lambda *a, **kw: spend_alert._LIVE_LOAD_FAILED,
    )
    cost, turns = spend_alert.burst_window_spend(
        "security_bot",
        now=datetime(2026, 5, 20, 19, 10, tzinfo=timezone.utc),
        window_minutes=60,
    )
    assert cost < 0, (
        "discovery-failure must surface as a sentinel, not '$0 — fine'"
    )
    assert turns == []


def test_emit_self_failure_signal_observes_alert_severity(replay_env, monkeypatch):
    """When >=1 bot fails JSONL discovery, the daemon emits a single
    pod-scope Signal with severity=alert (this is THE silent-failure
    class — operator must hear about it)."""
    captured: list = []
    import signals.store as _store
    from types import SimpleNamespace
    monkeypatch.setattr(
        _store, "observe",
        lambda shared_dir, **kw: (captured.append(kw), SimpleNamespace(id="self-fail"))[1],
    )
    spend_alert._emit_self_failure_signal(
        replay_env, ["security_bot", "team_bot_a"],
        datetime(2026, 5, 20, 19, 10, tzinfo=timezone.utc),
    )
    assert len(captured) == 1
    call = captured[0]
    assert call["severity"] == "alert"
    assert call["type"] == "self_failure_jsonl_discovery"
    assert call["scope"] == "pod"
    assert call["details"]["failed_bots"] == ["security_bot", "team_bot_a"]


def test_burst_alert_routes_through_catalog_critical(replay_env, monkeypatch):
    """A 3×-threshold burst still escalates severity even though it now
    shares a single catalog entry with the warn tier (2026-05-29 collapse).
    The producer-rendered payload (level_emoji / event_phrase /
    threshold_clause) carries the visual tier signal."""
    captured: list = []
    monkeypatch.setattr(
        spend_alert, "_dispatch",
        lambda **kw: (captured.append(kw), True)[1],
    )
    spend_alert.send_burst_alert(
        "security_bot", 16.3, 5.0,
        critical_multiplier=3.0, window_minutes=60,
        top_sessions=[{"session_id": "51bac282", "channel": "heartbeat",
                       "cost": 8.7}],
        shared_dir=replay_env, network={}, hour_bucket="2026-05-20T19",
    )
    assert captured[0]["catalog_event"] == "cost.burst_detected"
    assert captured[0]["severity_name"] == "critical"
    payload = captured[0]["payload"]
    assert payload["level_emoji"] == "🔴"
    assert payload["event_phrase"] == "cost runaway"
    assert "3" in payload["threshold_clause"] and "×" in payload["threshold_clause"]


# ── Window range label (disambiguates mid-straddle coexisting bursts) ──────


def test_burst_body_and_details_carry_window_range(replay_env, monkeypatch):
    """Each cost_burst Signal's body must lead with its rolling-window
    range so two coexisting Signals (mid-hour-boundary straddle) are
    visibly distinct on the Alerts page without expanding raw JSON.
    """
    captured: list = []
    import signals.store as _store
    from types import SimpleNamespace
    monkeypatch.setattr(
        _store, "observe",
        lambda shared_dir, **kw: (captured.append(kw), SimpleNamespace(id="sig"))[1],
    )

    spend_alert.emit_burst_signal(
        shared_dir=replay_env, bot_id="security_bot",
        cost_usd=6.0, threshold_usd=5.0, critical_multiplier=3.0,
        window_minutes=60, top_sessions=[],
        now=datetime(2026, 5, 20, 20, 5, tzinfo=timezone.utc),
    )
    call = captured[0]
    # Body leads with the window — operator sees it without clicking in.
    assert call["body"].startswith("Burst window: 2026-05-20 19:05–20:05 UTC\n")
    # Structured fields so the UI can render a chip later.
    assert call["details"]["window_label"] == "2026-05-20 19:05–20:05 UTC"
    assert call["details"]["window_start_iso"].startswith("2026-05-20T19:05")
    assert call["details"]["window_end_iso"].startswith("2026-05-20T20:05")


def test_burst_window_label_differs_per_straddled_bucket(replay_env, monkeypatch):
    """Two coexisting Signals (one per hour bucket) carry different
    window labels in their last-observed body — the disambiguation the
    polish step exists for."""
    captured: list = []
    import signals.store as _store
    from types import SimpleNamespace
    monkeypatch.setattr(
        _store, "observe",
        lambda shared_dir, **kw: (captured.append(kw), SimpleNamespace(id="sig"))[1],
    )

    # Two ticks: 19:55 → bucket T19, 20:05 → bucket T20. Each writes
    # its own Signal with a window range ending at the tick time.
    spend_alert.emit_burst_signal(
        shared_dir=replay_env, bot_id="security_bot",
        cost_usd=6.0, threshold_usd=5.0, critical_multiplier=3.0,
        window_minutes=60, top_sessions=[],
        now=datetime(2026, 5, 20, 19, 55, tzinfo=timezone.utc),
    )
    spend_alert.emit_burst_signal(
        shared_dir=replay_env, bot_id="security_bot",
        cost_usd=6.0, threshold_usd=5.0, critical_multiplier=3.0,
        window_minutes=60, top_sessions=[],
        now=datetime(2026, 5, 20, 20, 5, tzinfo=timezone.utc),
    )
    label_a = captured[0]["details"]["window_label"]
    label_b = captured[1]["details"]["window_label"]
    assert label_a != label_b
    assert "18:55–19:55" in label_a
    assert "19:05–20:05" in label_b


# ── Stale-burst sweep (regression for the "2 findings" Alerts duplicate) ───


def test_sweep_archives_stale_burst_signals(tmp_path):
    """Once a burst ends, previous-hour cost_burst Signals must auto-archive.

    The 2026-06-06 "2 findings" duplicate on the Alerts page was caused
    by spend_alert never calling sweep_resolve for cost_burst — a burst
    that straddled an hour boundary left two near-identical Signals
    stranded in firing/ once the bleed stopped.
    """
    import signals.store as _store

    bot_id = "security_bot"
    now = datetime(2026, 5, 20, 20, 30, tzinfo=timezone.utc)
    prev = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)

    # Two real Signals in firing/ — one per straddled hour bucket.
    for t in (prev, now):
        sig = spend_alert._burst_signature(bot_id, t)
        assert sig is not None
        _store.observe(
            tmp_path,
            signature=sig,
            producer="spend_alert",
            type="cost_burst",
            flavor="activity",
            severity="alert",
            scope="bot",
            bot_id=bot_id,
            title=f"cost burst: {bot_id} $33.65 / 60m",
            body="…",
        )
    assert sum(1 for _ in _store.iter_signals(tmp_path)) == 2

    # Tick where the burst is no longer firing — no signatures kept.
    spend_alert._sweep_stale_burst_signals(tmp_path, {bot_id}, set())

    remaining = [
        s for s in _store.iter_signals(tmp_path) if s.producer == "spend_alert"
    ]
    assert remaining == [], (
        f"expected both stranded cost_burst Signals archived; saw {remaining}"
    )


def test_sweep_keeps_current_bucket_signal_while_firing(tmp_path):
    """Mid-straddle: previous-hour Signal archives, current-hour stays."""
    import signals.store as _store

    bot_id = "security_bot"
    now = datetime(2026, 5, 20, 20, 5, tzinfo=timezone.utc)
    prev = datetime(2026, 5, 20, 19, 55, tzinfo=timezone.utc)

    for t in (prev, now):
        sig = spend_alert._burst_signature(bot_id, t)
        _store.observe(
            tmp_path,
            signature=sig,
            producer="spend_alert",
            type="cost_burst",
            flavor="activity",
            severity="alert",
            scope="bot",
            bot_id=bot_id,
            title=f"cost burst: {bot_id} $33.65 / 60m",
            body="…",
        )

    # Sweep with the current-hour bucket in the keep set — old one goes,
    # current one stays.
    kept = {spend_alert._burst_signature(bot_id, now)}
    spend_alert._sweep_stale_burst_signals(tmp_path, {bot_id}, kept)

    remaining = [
        s.signature
        for s in _store.iter_signals(tmp_path)
        if s.producer == "spend_alert" and s.state == "firing"
    ]
    assert remaining == [spend_alert._burst_signature(bot_id, now)]


def test_sweep_does_not_touch_unchecked_bots(tmp_path):
    """Discovery-failed bots must NOT have their Signals swept.

    A flaky JSONL read on bot A shouldn't auto-archive bot A's burst
    Signal — the daemon's contract is "if I can't see your data, I
    page; I don't reassure." Mirrors the load-failure sentinel rules
    in load_today_spend / burst_window_spend.
    """
    import signals.store as _store

    failing = "team_bot_a"
    fine = "security_bot"
    t = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    for bot in (failing, fine):
        _store.observe(
            tmp_path,
            signature=spend_alert._burst_signature(bot, t),
            producer="spend_alert",
            type="cost_burst",
            flavor="activity",
            severity="alert",
            scope="bot",
            bot_id=bot,
            title=f"cost burst: {bot} $33.65 / 60m",
            body="…",
        )

    # Only `fine` was checked this tick; sweep keeps nothing for fine.
    spend_alert._sweep_stale_burst_signals(tmp_path, {fine}, set())

    remaining = sorted(
        s.bot_id for s in _store.iter_signals(tmp_path)
        if s.producer == "spend_alert" and s.state == "firing"
    )
    assert remaining == [failing], (
        f"expected only the unchecked bot's Signal to survive; got {remaining}"
    )
