"""Stale-signal sweep, generalized to all four spend_alert signal types.

Track 1.1 of the 2026-08-30 alerts-noise review
(internal/review-alerts-findings-noise-2026-08-30.md): three of
spend_alert's four signal types use day-bucketed signatures —
``cost_velocity_forecast``, ``spend_unpriced_turns`` (per-(bot, day)) and
``self_failure_jsonl_discovery`` (per-day, pod-scoped) — but the module's
only sweep was hard-scoped to ``types={"cost_burst"}``. At midnight
rollover the prior day's signature was orphaned in the signal store's
firing/ dir forever (the live mini pod had forecast Signals from
2026-06-09 still firing), and a below-threshold day just returned None
from the emitter with no resolve path.

These tests drive ``spend_alert.main()`` end-to-end against a real signal
store under tmp_path, with the data-loading + dispatch seams mocked, and
pin the generalized contract: at the end of each run, active spend_alert
Signals of ALL FOUR types whose signature is not in the run's
kept/emitted set are auto-resolved. Every ``main()``-driven test here
fails on the pre-fix code (the swept Signal stayed firing).
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pod_time  # noqa: E402
import spend_alert  # noqa: E402
from spend_alert import DaySpend  # noqa: E402

import signals.store as _store  # noqa: E402


# Frozen clock: noon pod-time, so a $4 spend projects to $8/day — 80% of
# the $10 cap, over the 0.70 default warn fraction.
NOW = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 5, 21)
YESTERDAY = TODAY - timedelta(days=1)

CAP_LADDER = {
    "daily_warn": None,
    "weekly_warn": None,
    "tier_downgrade": None,
    "l1_breaker": 10.0,
    "l2_breaker": None,
    "per_session": None,
}


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 — always UTC in spend_alert
        return NOW


def _run_main(monkeypatch, shared: Path, bots: dict, caps=None) -> None:
    """Drive spend_alert.main() with the data seams mocked per bot.

    ``bots`` maps bot_id → {"burst": (cost, turns), "day": DaySpend|None}.
    A burst cost of -1.0 replays the ``_LIVE_LOAD_FAILED`` discovery
    sentinel; ``day=None`` replays a failed daily JSONL read. The signal
    store under ``shared`` is real — only data loading, cap resolution,
    and chat dispatch are stubbed.

    ``caps`` overrides the ``_resolve_per_bot_caps`` stand-in (default: a
    clean $10 L1 ladder). Pass a callable that raises to replay a budget
    ladder that could not be READ, or one returning an all-None ladder to
    replay a ladder that resolved to no cap at all — two states the
    shipped reader used to collapse into the same return value.
    """
    config = {"members": list(bots), "sharedDir": str(shared)}

    monkeypatch.setattr(sys, "argv", ["spend_alert"])
    monkeypatch.setattr(spend_alert, "resolve_network_path", lambda: "unused")
    monkeypatch.setattr(spend_alert, "load_config", lambda n=None: config)
    monkeypatch.setattr(spend_alert, "get_shared_dir", lambda c: shared)
    monkeypatch.setattr(spend_alert, "get_members", lambda c: list(bots))
    monkeypatch.setattr(spend_alert, "datetime", _FrozenDatetime)
    monkeypatch.setattr(pod_time, "pod_today", lambda: TODAY)
    monkeypatch.setattr(
        spend_alert, "burst_window_spend",
        lambda bot_id, **kw: bots[bot_id]["burst"],
    )
    monkeypatch.setattr(
        spend_alert, "load_today_spend_detail",
        lambda sd, bot_id, today, **kw: bots[bot_id]["day"],
    )
    monkeypatch.setattr(
        spend_alert, "_resolve_per_bot_caps",
        caps if caps is not None else (lambda bot_id, sd, **kw: dict(CAP_LADDER)),
    )
    # Keep the daemon's side-effect seams inert: no chat, no flag files,
    # no log writes to the real /Users/Shared path.
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **kw: True)
    monkeypatch.setattr(spend_alert, "send_alert", lambda *a, **kw: False)
    monkeypatch.setattr(
        spend_alert, "_flag_urgent_refresh", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        spend_alert, "_maybe_send_weekly_summary", lambda *a, **kw: None
    )
    monkeypatch.setattr(spend_alert, "_log", lambda msg: None)

    spend_alert.main()


def _seed(shared: Path, signature: str, sig_type: str, bot_id: str | None):
    """Plant a firing spend_alert Signal via the store API (never raw JSON)."""
    _store.observe(
        shared,
        signature=signature,
        producer="spend_alert",
        type=sig_type,
        flavor="activity",
        severity="warn",
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=f"seeded {sig_type}",
        body="…",
    )


def _states(shared: Path) -> dict[str, str]:
    """signature → state across firing/snoozed/archived."""
    return {
        s.signature: s.state
        for s in _store.iter_signals(
            shared, subdirs=("firing", "snoozed", "archived")
        )
        if s.producer == "spend_alert"
    }


# ── cost_velocity_forecast ─────────────────────────────────────────────────


def test_yesterdays_velocity_signal_resolved_by_todays_run(
    tmp_path, monkeypatch,
):
    """Midnight rollover: the prior day's per-(bot, day) forecast signature
    is no longer emitted, so today's run must auto-resolve it. This is the
    exact immortal-signal shape from the live pod (firing since 2026-06-09)."""
    bot = "team_bot_a"
    stale = spend_alert._day_signature("cost_velocity_forecast", bot, YESTERDAY)
    assert stale is not None
    _seed(tmp_path, stale, "cost_velocity_forecast", bot)

    # $4 by noon → projected $8/day = 80% of the $10 cap → today's Signal
    # fires; yesterday's is not in the keep-set and must archive.
    _run_main(monkeypatch, tmp_path, {
        bot: {"burst": (0.0, []), "day": DaySpend(usd=4.0, priced_turns=10)},
    })

    states = _states(tmp_path)
    assert states[stale] == "resolved", (
        f"yesterday's forecast Signal must auto-archive; got {states}"
    )
    fresh = spend_alert._day_signature("cost_velocity_forecast", bot, TODAY)
    assert states.get(fresh) == "firing", (
        f"today's over-threshold forecast must fire; got {states}"
    )


def test_todays_over_threshold_velocity_signal_stays_firing(
    tmp_path, monkeypatch,
):
    """A same-day Signal observed on an earlier tick stays firing when the
    projection is still over the warn fraction — the sweep keeps the
    signature the run just re-observed."""
    bot = "team_bot_a"
    todays = spend_alert._day_signature("cost_velocity_forecast", bot, TODAY)
    _seed(tmp_path, todays, "cost_velocity_forecast", bot)

    _run_main(monkeypatch, tmp_path, {
        bot: {"burst": (0.0, []), "day": DaySpend(usd=4.0, priced_turns=10)},
    })

    assert _states(tmp_path)[todays] == "firing"


def test_below_threshold_bot_same_day_velocity_signal_resolves(
    tmp_path, monkeypatch,
):
    """Spend dropped back under the warn fraction intraday: the emitter
    returns None (no re-observe), so the same-day Signal must resolve
    rather than sit firing until an operator notices."""
    bot = "team_bot_a"
    todays = spend_alert._day_signature("cost_velocity_forecast", bot, TODAY)
    _seed(tmp_path, todays, "cost_velocity_forecast", bot)

    # $0.05 by noon → projected $0.10 = 1% of cap → no emit this tick.
    _run_main(monkeypatch, tmp_path, {
        bot: {"burst": (0.0, []), "day": DaySpend(usd=0.05, priced_turns=2)},
    })

    assert _states(tmp_path)[todays] == "resolved"


def test_day_signals_of_discovery_failed_bot_are_not_swept(
    tmp_path, monkeypatch,
):
    """"If I can't see your data, I page; I don't reassure": a bot whose
    daily JSONL read failed is not day-checked, so its forecast Signals —
    stale or not — must survive until the next clean tick."""
    bot = "team_bot_a"
    stale = spend_alert._day_signature("cost_velocity_forecast", bot, YESTERDAY)
    _seed(tmp_path, stale, "cost_velocity_forecast", bot)

    _run_main(monkeypatch, tmp_path, {
        bot: {"burst": (0.0, []), "day": None},
    })

    assert _states(tmp_path)[stale] == "firing"


# ── cap-ladder gate: sweep only when the forecast's OWN inputs were read ───
#
# ``cost_velocity_forecast`` needs two inputs — the daily JSONL read AND the
# budget ladder — where ``spend_unpriced_turns`` needs only the first. While
# both types were swept on ``day_checked_bots`` (the daily read alone), a tick
# whose ladder could not be read skipped the emitter, kept no signature, and
# resolved a live forecast Signal: a "Cleared …" push while the bot is still
# burning. These tests pin the two sub-cases apart — an UNREADABLE ladder is
# not an answer, a ladder that resolved to NO CAP is.


NO_CAP_LADDER = {rung: None for rung in CAP_LADDER}


def _unreadable_ladder(bot_id, sd, *, strict=False, **kw):
    """Stand-in for the shipped reader when BE config cannot be read.

    Mirrors ``_resolve_per_bot_caps``'s real contract on failure: the
    historical all-None ladder for the enforcement callers, a raise for the
    strict caller that must not mistake it for "no cap configured".
    """
    if strict:
        raise spend_alert._LadderUnavailable("better-engine config unreadable")
    return dict(NO_CAP_LADDER)


def test_velocity_signal_survives_tick_whose_cap_ladder_was_unreadable(
    tmp_path, monkeypatch,
):
    """The daily read succeeded but the ladder did not resolve: the forecast
    condition was never evaluated this tick, so its Signal must NOT be swept.
    Same contract as the discovery-failed bot — if I can't see the input, I
    don't reassure."""
    bot = "team_bot_a"
    todays = spend_alert._day_signature("cost_velocity_forecast", bot, TODAY)
    assert todays is not None
    _seed(tmp_path, todays, "cost_velocity_forecast", bot)

    _run_main(
        monkeypatch, tmp_path,
        {bot: {"burst": (0.0, []), "day": DaySpend(usd=4.0, priced_turns=10)}},
        caps=_unreadable_ladder,
    )

    assert _states(tmp_path)[todays] == "firing", (
        "an unreadable budget ladder must not clear a live forecast Signal"
    )


def test_velocity_signal_still_resolves_on_the_next_clean_tick(
    tmp_path, monkeypatch,
):
    """The ladder gate suppresses the sweep for one tick, it does not disable
    it: once the ladder resolves and the projection has genuinely dropped
    below the warn fraction, the Signal resolves as before."""
    bot = "team_bot_a"
    todays = spend_alert._day_signature("cost_velocity_forecast", bot, TODAY)
    _seed(tmp_path, todays, "cost_velocity_forecast", bot)

    _run_main(
        monkeypatch, tmp_path,
        {bot: {"burst": (0.0, []), "day": DaySpend(usd=4.0, priced_turns=10)}},
        caps=_unreadable_ladder,
    )
    assert _states(tmp_path)[todays] == "firing"

    # Ladder readable again; $0.05 by noon → projected $0.10 = 1% of cap.
    _run_main(
        monkeypatch, tmp_path,
        {bot: {"burst": (0.0, []), "day": DaySpend(usd=0.05, priced_turns=2)}},
    )
    assert _states(tmp_path)[todays] == "resolved"


def test_velocity_signal_resolved_when_ladder_resolves_to_no_cap(
    tmp_path, monkeypatch,
):
    """The other sub-case: the ladder READ fine and the operator has no cap
    configured. The forecast is meaningless without a cap, so a Signal left
    over from when a cap existed must still resolve — the gate keys on
    "was the ladder read", never on "is a cap set"."""
    bot = "team_bot_a"
    todays = spend_alert._day_signature("cost_velocity_forecast", bot, TODAY)
    _seed(tmp_path, todays, "cost_velocity_forecast", bot)

    _run_main(
        monkeypatch, tmp_path,
        {bot: {"burst": (0.0, []), "day": DaySpend(usd=4.0, priced_turns=10)}},
        caps=lambda bot_id, sd, **kw: dict(NO_CAP_LADDER),
    )

    assert _states(tmp_path)[todays] == "resolved"


def test_unpriced_sweep_unaffected_by_an_unreadable_cap_ladder(
    tmp_path, monkeypatch,
):
    """``spend_unpriced_turns`` depends only on the daily read, so the ladder
    gate must not reach it: yesterday's stale unpriced Signal still resolves
    on a tick whose ladder failed — while the forecast Signal survives."""
    bot = "team_bot_a"
    stale_unpriced = spend_alert._day_signature(
        "spend_unpriced_turns", bot, YESTERDAY
    )
    todays_velocity = spend_alert._day_signature(
        "cost_velocity_forecast", bot, TODAY
    )
    _seed(tmp_path, stale_unpriced, "spend_unpriced_turns", bot)
    _seed(tmp_path, todays_velocity, "cost_velocity_forecast", bot)

    # Today is fully priced → no unpriced Signal emitted → the stale one goes.
    _run_main(
        monkeypatch, tmp_path,
        {bot: {"burst": (0.0, []), "day": DaySpend(usd=4.0, priced_turns=10)}},
        caps=_unreadable_ladder,
    )

    states = _states(tmp_path)
    assert states[stale_unpriced] == "resolved"
    assert states[todays_velocity] == "firing"


# ── keeps record the PREDICATE, not the store write's outcome ──────────────
#
# A transient ``observe()`` raise used to drop the signature from the
# keep-set, so the sweep resolved a Signal whose condition was still true on
# exactly the tick the store misbehaved (2026-08-30 review, concern 2).


def _observe_raising_for(sig_type: str):
    """Wrap the real ``store.observe``, raising for one signal type only."""
    real = _store.observe

    def _fake(shared_dir, **kw):
        if kw.get("type") == sig_type:
            raise RuntimeError("simulated transient signal-store write failure")
        return real(shared_dir, **kw)

    return _fake


def test_velocity_keep_survives_a_transient_store_write_failure(
    tmp_path, monkeypatch,
):
    bot = "team_bot_a"
    todays = spend_alert._day_signature("cost_velocity_forecast", bot, TODAY)
    _seed(tmp_path, todays, "cost_velocity_forecast", bot)
    monkeypatch.setattr(
        _store, "observe", _observe_raising_for("cost_velocity_forecast")
    )

    # $4 by noon → still 80% of the $10 cap: the condition HOLDS, only the
    # write failed. Resolving here would push a false "Cleared …".
    _run_main(monkeypatch, tmp_path, {
        bot: {"burst": (0.0, []), "day": DaySpend(usd=4.0, priced_turns=10)},
    })

    assert _states(tmp_path)[todays] == "firing"


def test_unpriced_keep_survives_a_transient_store_write_failure(
    tmp_path, monkeypatch,
):
    bot = "team_bot_a"
    todays = spend_alert._day_signature("spend_unpriced_turns", bot, TODAY)
    _seed(tmp_path, todays, "spend_unpriced_turns", bot)
    monkeypatch.setattr(
        _store, "observe", _observe_raising_for("spend_unpriced_turns")
    )

    _run_main(monkeypatch, tmp_path, {
        bot: {
            "burst": (0.0, []),
            "day": DaySpend(
                usd=0.50, priced_turns=5, unpriced_turns=3,
                unpriced_providers=("someprovider",),
            ),
        },
    })

    assert _states(tmp_path)[todays] == "firing"


# ── spend_unpriced_turns ───────────────────────────────────────────────────


def test_yesterdays_unpriced_signal_resolved_by_todays_run(
    tmp_path, monkeypatch,
):
    bot = "team_bot_a"
    stale = spend_alert._day_signature("spend_unpriced_turns", bot, YESTERDAY)
    _seed(tmp_path, stale, "spend_unpriced_turns", bot)

    # Today is fully priced — no unpriced Signal emitted, stale one goes.
    _run_main(monkeypatch, tmp_path, {
        bot: {"burst": (0.0, []), "day": DaySpend(usd=0.50, priced_turns=5)},
    })

    assert _states(tmp_path)[stale] == "resolved"


def test_todays_unpriced_signal_stays_firing_while_still_unpriced(
    tmp_path, monkeypatch,
):
    bot = "team_bot_a"
    todays = spend_alert._day_signature("spend_unpriced_turns", bot, TODAY)
    stale = spend_alert._day_signature("spend_unpriced_turns", bot, YESTERDAY)
    _seed(tmp_path, stale, "spend_unpriced_turns", bot)

    _run_main(monkeypatch, tmp_path, {
        bot: {
            "burst": (0.0, []),
            "day": DaySpend(
                usd=0.50, priced_turns=5, unpriced_turns=3,
                unpriced_providers=("someprovider",),
            ),
        },
    })

    states = _states(tmp_path)
    assert states[stale] == "resolved"
    assert states.get(todays) == "firing"


# ── self_failure_jsonl_discovery (pod-scoped, per-day) ─────────────────────


def test_yesterdays_self_failure_signal_resolved_when_discovery_healthy(
    tmp_path, monkeypatch,
):
    """The pod-scoped Signal carries no bot_id, so the generalized sweep
    must run it WITHOUT a bot_ids filter — a bot-filtered sweep would
    never match it and it would stay firing forever."""
    stale = spend_alert._self_failure_signature(
        datetime.combine(YESTERDAY, datetime.min.time(), tzinfo=timezone.utc)
    )
    assert stale is not None
    _seed(tmp_path, stale, "self_failure_jsonl_discovery", None)

    _run_main(monkeypatch, tmp_path, {
        "team_bot_a": {"burst": (0.0, []), "day": DaySpend(usd=0.10, priced_turns=1)},
    })

    assert _states(tmp_path)[stale] == "resolved"


def test_todays_self_failure_signal_stays_while_discovery_still_failing(
    tmp_path, monkeypatch,
):
    stale = spend_alert._self_failure_signature(
        datetime.combine(YESTERDAY, datetime.min.time(), tzinfo=timezone.utc)
    )
    _seed(tmp_path, stale, "self_failure_jsonl_discovery", None)

    # Discovery fails for the bot today (burst sentinel -1.0 + day=None):
    # today's self-failure Signal fires and is kept; yesterday's resolves.
    _run_main(monkeypatch, tmp_path, {
        "team_bot_a": {"burst": (-1.0, []), "day": None},
    })

    states = _states(tmp_path)
    assert states[stale] == "resolved"
    todays = spend_alert._self_failure_signature(NOW)
    assert states.get(todays) == "firing"


# ── emitter ↔ sweep signature parity (drift guard) ─────────────────────────


def test_velocity_emitter_signature_matches_day_signature_helper(tmp_path):
    spend_alert.emit_velocity_forecast_signal(
        shared_dir=tmp_path, bot_id="team_bot_a", spend_usd=4.0, cap_usd=10.0,
        warn_fraction=0.70, today=TODAY, now=NOW,
    )
    sig = _store.find_active_by_signature(
        tmp_path, spend_alert._day_signature(
            "cost_velocity_forecast", "team_bot_a", TODAY
        ),
    )
    assert sig is not None and sig.type == "cost_velocity_forecast"


def test_unpriced_emitter_signature_matches_day_signature_helper(tmp_path):
    spend_alert.emit_unpriced_spend_signal(
        shared_dir=tmp_path, bot_id="team_bot_a",
        day=DaySpend(usd=1.0, priced_turns=1, unpriced_turns=2,
                     unpriced_providers=("someprovider",)),
        cap_usd=10.0, today=TODAY,
    )
    sig = _store.find_active_by_signature(
        tmp_path, spend_alert._day_signature(
            "spend_unpriced_turns", "team_bot_a", TODAY
        ),
    )
    assert sig is not None and sig.type == "spend_unpriced_turns"


def test_self_failure_emitter_signature_matches_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(spend_alert, "_log", lambda msg: None)
    spend_alert._emit_self_failure_signal(tmp_path, ["team_bot_a"], NOW)
    sig = _store.find_active_by_signature(
        tmp_path, spend_alert._self_failure_signature(NOW),
    )
    assert sig is not None and sig.type == "self_failure_jsonl_discovery"
