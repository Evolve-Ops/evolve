"""End-to-end tests for /api/analytics/ttl-recommendation.

The synthesis step that turns raw cache/gap data into one concrete call. It
recommends across TWO knobs, and the whole point of these tests is that it
never confuses them:

  cache_retention       Anthropic's prompt-cache lifetime. "long" →
                        cache_control.ttl = "1h"; anything else → the
                        5-minute default. The ONLY knob that moves the
                        invalidation rate.
  contextPruning.ttl    Gates when pruning may run — OC skips pruning while
                        the cache is warm so it doesn't rewrite a live prefix.
                        Cannot change cache lifetime at all.

The endpoint originally diagnosed invalidation correctly and then prescribed
contextPruning.ttl for it, the same misconception that produced the "4h"
default fixed in PR #3497. ``test_reasoning_prescribes_the_knob_it_diagnosed``
and its pair are the regression locks for that.

TIMESTAMP DISCIPLINE: fixtures anchor to ``datetime.now(UTC)``, never to a
literal date. Every one of these tests was quarantined as "shape drift" when
in fact the fixtures were pinned to 2026-05-11 and simply fell out of the
trailing 7-day window read_events() applies — they reported not_enough_data
for a reason that had nothing to do with shape. A hardcoded date here is a
time bomb with a ~1 week fuse.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web.server import _register_analytics_routes  # noqa: E402

_MINUTE = 60
_HOUR = 3600

# Anchor far enough back that a multi-day gap series still lands inside the
# 7-day window the tests query, and never in the future (read_events clamps
# the window at ``now``).
_WINDOW_DAYS = 7


def _base() -> datetime:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=5)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _write_cost_events(shared_dir: Path, bot_id: str, events: list[dict]) -> None:
    """Write events into the per-day ledger file matching each event's own ts.

    Grouping by the event's own date (rather than one caller-supplied date)
    keeps multi-day gap series honest — a series that spans midnight would
    otherwise land in a file whose name disagrees with its contents.
    """
    annotations = shared_dir / "annotations" / bot_id
    annotations.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict]] = {}
    for e in events:
        by_day.setdefault(e["ts"][:10], []).append(e)
    for day, day_events in by_day.items():
        with (annotations / f"cost_events-{day}.jsonl").open("a") as f:
            for e in day_events:
                f.write(json.dumps(e) + "\n")


def _evt(
    *,
    ts: str,
    bot_id: str = "admin_bot",
    session_id: str = "sess-1",
    trigger_kind: str = "user_turn",
    cache_state: str = "warm",
    input_tokens: int = 1000,
    cache_read_tokens: int = 5000,
    cache_write_tokens: int = 0,
    output_tokens: int = 200,
    cost_usd: float = 0.01,
) -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": ts,
        "bot_id": bot_id,
        "session_id": session_id,
        "trigger_kind": trigger_kind,
        "model": "claude-sonnet",
        "provider": "anthropic",
        "cache_state": cache_state,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": cost_usd,
    }


def _session(
    *,
    gaps: list[int],
    session_id: str = "s1",
    bot_id: str = "admin_bot",
    trigger_kind: str = "user_turn",
    invalidated: int = 0,
    cache_write_tokens: int = 2000,
    start: datetime | None = None,
) -> list[dict]:
    """One session whose consecutive inter-request deltas are exactly ``gaps``.

    Explicit gaps (rather than a fixed cadence) let a test place the
    break-even ratio precisely: the recommendation turns on how many gaps
    outlive a 5-minute cache versus a 1-hour one.

    The first ``invalidated`` events carry cache_state="invalidated"; the
    rest are warm. Keeping the cache signal inside the SAME session matters —
    every extra session is another unavoidable cacheWrite under both
    retention settings, which drags the ratio toward 1.0.
    """
    t = start or _base()
    out = [
        _evt(
            ts=t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            bot_id=bot_id, session_id=session_id, trigger_kind=trigger_kind,
            cache_state="invalidated" if invalidated > 0 else "warm",
            cache_write_tokens=cache_write_tokens if invalidated > 0 else 0,
        )
    ]
    remaining = invalidated - 1
    for gap in gaps:
        t = t + timedelta(seconds=gap)
        is_inv = remaining > 0
        remaining -= 1
        out.append(_evt(
            ts=t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            bot_id=bot_id, session_id=session_id, trigger_kind=trigger_kind,
            cache_state="invalidated" if is_inv else "warm",
            cache_write_tokens=cache_write_tokens if is_inv else 0,
        ))
    return out


@pytest.fixture
def app(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(
        json.dumps({
            "sharedDir": str(shared),
            "primary": "evolve",
            "members": ["admin_bot", "team_bot_a"],
        })
    )
    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    a.shared_dir = shared
    return a


def _write_openclaw(
    app,
    *,
    bot_id: str = "admin_bot",
    prune_ttl: str | None = "1h",
    prune_mode: str | None = "cache-ttl",
    cache_retention: str | None = None,
) -> None:
    """Drop a minimal openclaw.json carrying both cache-adjacent knobs.

    cache_retention is written where the materializer puts it — fanned out to
    each Anthropic model's ``params.cacheRetention`` — not as a top-level
    field, so the reader is exercised against the real deployed shape.
    """
    home = app.shared_dir / "fake-users" / bot_id / ".openclaw"
    home.mkdir(parents=True, exist_ok=True)
    defaults: dict = {}
    if prune_ttl is not None or prune_mode is not None:
        cp: dict = {}
        if prune_mode is not None:
            cp["mode"] = prune_mode
        if prune_ttl is not None:
            cp["ttl"] = prune_ttl
        defaults["contextPruning"] = cp
    if cache_retention is not None:
        defaults["models"] = {
            "anthropic/claude-sonnet-4-6": {"params": {"cacheRetention": cache_retention}},
            "openai/gpt-5": {"params": {}},
        }
    (home / "openclaw.json").write_text(json.dumps({"agents": {"defaults": defaults}}))


def _get(app, bot_id: str = "admin_bot", days: int = _WINDOW_DAYS) -> dict:
    """Call the endpoint with the bot-home read pointed at the fake tree.

    Patches ``routes_analytics._bot_home`` — the module that actually reads
    openclaw.json. The pre-existing tests patched ``server._bot_home``, which
    is a different binding (server only holds a registration shim), so the
    openclaw.json read fell through to the real /Users/<bot> path and every
    config-dependent assertion was being evaluated against None.
    """
    from evolve_admin.web import routes_analytics as _ra

    with patch.object(_ra, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get(f"/api/analytics/ttl-recommendation?bot={bot_id}&days={days}")
    assert resp.status_code == 200
    return resp.get_json()["recommendations"][0]


# ── Shape ─────────────────────────────────────────────────────────────────────


def test_endpoint_returns_expected_shape(app):
    """Empty pod returns valid recommendation entries (one per bot)."""
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/ttl-recommendation?days={_WINDOW_DAYS}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) == {"bot_id", "window_days", "recommendations"}
    bots_seen = [r["bot_id"] for r in body["recommendations"]]
    assert "evolve" in bots_seen
    assert "admin_bot" in bots_seen
    assert all(r["verdict"] == "not_enough_data" for r in body["recommendations"])


def test_specific_bot_filter(app):
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/ttl-recommendation?bot=admin_bot&days={_WINDOW_DAYS}")
    body = resp.get_json()
    assert body["bot_id"] == "admin_bot"
    assert len(body["recommendations"]) == 1
    assert body["recommendations"][0]["bot_id"] == "admin_bot"


def test_multi_bot_returns_one_entry_per_bot(app):
    """When ``bot`` is empty, return one recommendation per member + evolve."""
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/ttl-recommendation?days={_WINDOW_DAYS}")
    bots_seen = {r["bot_id"] for r in resp.get_json()["recommendations"]}
    assert {"admin_bot", "team_bot_a", "evolve"} <= bots_seen


def test_every_recommendation_names_the_knob_it_targets(app):
    """An actionable verdict must say WHICH setting it means.

    "Raise this bot's TTL" reads identically for two settings that do
    completely different things; ``knob`` is what makes the card unambiguous,
    and target_ttl must stay null unless the call really is about pruning.
    """
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)
    assert rec["verdict"] in ("raise", "lower")
    assert rec["knob"] in ("cache_retention", "context_pruning_ttl")
    assert rec["current_setting"] and rec["target_setting"]
    if rec["knob"] == "cache_retention":
        assert rec["target_ttl"] is None, "a cache call must not look like a pruning call"
        assert rec["target_value"] in ("short", "long")


# ── Confidence gating ────────────────────────────────────────────────────────


def test_not_enough_data_when_evidence_below_50(app):
    """≥50 gaps or ≥50 cache-participating events required for a call."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(gaps=[2 * _MINUTE] * 39))
    _write_openclaw(app)
    rec = _get(app)
    assert rec["verdict"] == "not_enough_data"
    assert rec["confidence"] == "low"
    assert rec["knob"] is None


def test_not_enough_data_when_window_below_3_days(app):
    """Even with abundant gaps, a sub-3d window gates to not_enough_data."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(gaps=[_MINUTE] * 299))
    _write_openclaw(app)
    rec = _get(app, days=2)
    assert rec["verdict"] == "not_enough_data"


# ── cache_retention: the knob that actually controls cache lifetime ──────────


def test_raise_cache_retention_when_invalidation_high_and_gaps_in_the_1h_band(app):
    """15-minute gaps + 67% invalidation on the 5m default → go to "long".

    This is the case the endpoint used to answer with a contextPruning.ttl
    bump, which could not have changed the invalidation rate by a point.
    """
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)

    assert rec["verdict"] == "raise"
    assert rec["knob"] == "cache_retention"
    assert rec["target_value"] == "long"
    assert rec["effective_cache_retention"] == "short"
    assert rec["cache_ttl_seconds"] == 300
    # Never dressed up as a pruning change.
    assert rec["target_ttl"] is None and rec["target_ttl_seconds"] is None
    assert rec["estimated_monthly_savings_usd"] > 0
    assert rec["metrics"]["rewrite_factor"] > rec["metrics"]["long_retention_breakeven"]


def test_no_long_retention_for_a_bot_whose_turns_are_hours_apart(app):
    """A 2h-cadence bot breaks a 1h cache as reliably as a 5m one.

    It would pay 2.00x per cacheWrite instead of 1.25x and convert nothing to
    reads. High invalidation alone must NOT be enough to recommend "long" —
    that is the trap an invalidation-only rule walks straight into.
    """
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _HOUR] * 59, invalidated=40,
    ))
    _write_openclaw(app, prune_ttl="1h", cache_retention=None)
    rec = _get(app)

    assert rec["metrics"]["invalidated_ratio"] > 0.15, "the invalidation bait is present"
    assert rec["metrics"]["rewrite_factor"] == pytest.approx(1.0)
    assert rec["verdict"] == "hold"
    assert rec["knob"] is None
    assert "would not pay for itself" in rec["reasoning"]


def test_lower_cache_retention_when_long_cannot_pay_for_itself(app):
    """Already on "long" with hours-apart turns → drop back to "short"."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _HOUR] * 59, invalidated=40,
    ))
    _write_openclaw(app, prune_ttl="1h", cache_retention="long")
    rec = _get(app)

    assert rec["verdict"] == "lower"
    assert rec["knob"] == "cache_retention"
    assert rec["target_value"] == "short"
    assert rec["effective_cache_retention"] == "long"
    assert rec["cache_ttl_seconds"] == 3600
    # The saving is the premium being burned, reported as a gain from switching.
    assert rec["estimated_monthly_savings_usd"] > 0
    assert "2.00x" in rec["reasoning"] and "1.25x" in rec["reasoning"]


def test_hold_when_long_retention_is_earning_its_premium(app):
    """On "long" with 15-minute gaps → the 1h cache is converting writes."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=5,
    ))
    _write_openclaw(app, prune_ttl="1h", cache_retention="long")
    rec = _get(app)

    assert rec["verdict"] == "hold"
    assert rec["knob"] is None
    assert "earning its" in rec["reasoning"]


# ── The break-even boundary ──────────────────────────────────────────────────
#
# long wins iff writes_short / writes_long > (2.00 - 0.10) / (1.25 - 0.10),
# i.e. > 1.652. Both sides of that line, with one gap moved between them.


def _straddle_gaps(long_breaking: int, rescued: int) -> list[int]:
    """``long_breaking`` gaps that outlive even a 1h cache + ``rescued`` that
    a 1h cache would absorb but a 5m one would not."""
    return [2 * _HOUR] * long_breaking + [15 * _MINUTE] * rescued


def test_no_raise_just_below_the_long_retention_breakeven(app):
    """36 cache-breaking gaps of 59 → 60/37 = 1.62x, under the 1.65x line."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=_straddle_gaps(36, 23), invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)

    assert rec["metrics"]["rewrite_factor"] == pytest.approx(60 / 37, abs=0.005)
    assert rec["verdict"] != "raise" or rec["knob"] != "cache_retention"


def test_raise_just_above_the_long_retention_breakeven(app):
    """One fewer cache-breaking gap → 60/36 = 1.67x, over the line."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=_straddle_gaps(35, 24), invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)

    assert rec["metrics"]["rewrite_factor"] == pytest.approx(60 / 36, abs=0.005)
    assert rec["verdict"] == "raise"
    assert rec["knob"] == "cache_retention"


def test_breakeven_constant_matches_the_price_multipliers(app):
    """The reported break-even is derived, not a magic number.

    write(1h)=2.00x, write(5m)=1.25x, read=0.10x on base input price ⇒
    (2.00-0.10)/(1.25-0.10).
    """
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40,
    ))
    _write_openclaw(app)
    rec = _get(app)
    assert rec["metrics"]["long_retention_breakeven"] == pytest.approx(
        (2.00 - 0.10) / (1.25 - 0.10), abs=0.001
    )


# ── Heartbeat traffic counts ─────────────────────────────────────────────────


def test_heartbeat_requests_count_toward_the_cache_economics(app):
    """A heartbeat touches the prefix and resets the cache clock like a turn.

    Sizing off user turns alone made heartbeat-driven bots read as "sparse
    data" when they are exactly the bots whose cache economics are decided by
    machine cadence.
    """
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, trigger_kind="heartbeat", invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)

    assert rec["metrics"]["gap_count"] == 0, "no user turns at all"
    assert rec["metrics"]["request_gap_count"] == 59
    assert rec["verdict"] == "raise"
    assert rec["knob"] == "cache_retention"


# ── contextPruning.ttl: coherence only, never a cache fix ────────────────────


def test_raise_prune_ttl_when_it_prunes_while_the_cache_is_still_warm(app):
    """prune 15m under a 1h cache discards a prefix the bot just paid for."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=5,
    ))
    _write_openclaw(app, prune_ttl="15m", cache_retention="long")
    rec = _get(app)

    assert rec["verdict"] == "raise"
    assert rec["knob"] == "context_pruning_ttl"
    assert rec["target_value"] == "1h"
    assert rec["target_ttl_seconds"] == 3600
    assert rec["current_ttl"] == "15m"


def test_lower_prune_ttl_when_it_exceeds_every_cache_anthropic_offers(app):
    """Above 1h there is no cache configuration that makes the ttl coherent."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _HOUR] * 59, invalidated=40,
    ))
    _write_openclaw(app, prune_ttl="4h", cache_retention=None)
    rec = _get(app)

    assert rec["verdict"] == "lower"
    assert rec["knob"] == "context_pruning_ttl"
    assert rec["target_ttl_seconds"] == 300
    assert rec["target_value"] == "5m"


def test_shipped_default_prune_ttl_is_not_flagged(app):
    """prune 1h over the 5-minute default cache is the deploy default (#3497).

    It sits inside the bound that PR established, and the operator's likely
    next move — extending the cache to 1h — makes it exactly matched. A
    recommender that flags it has every deployed bot arguing with its own
    default on first page load.
    """
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _HOUR] * 59, invalidated=40,
    ))
    _write_openclaw(app, prune_ttl="1h", prune_mode="cache-ttl", cache_retention=None)
    rec = _get(app)

    assert rec["current_ttl"] == "1h"
    assert rec["cache_ttl_seconds"] == 300
    assert rec["knob"] != "context_pruning_ttl"


def test_prune_ttl_at_the_coherence_ceiling_is_never_flagged_too_high(app):
    """The ceiling agrees with cost_profiles, the module that owns the bound."""
    import cost_profiles

    ceiling = cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _HOUR] * 59, invalidated=40,
    ))
    _write_openclaw(app, prune_ttl=f"{ceiling}s", cache_retention=None)
    rec = _get(app)
    assert rec["knob"] != "context_pruning_ttl"


def test_prune_ttl_is_inert_when_pruning_mode_is_not_cache_ttl(app):
    """No `cache-ttl` mode means no cache-coupled wait — the ttl does nothing."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _HOUR] * 59, invalidated=40,
    ))
    _write_openclaw(app, prune_ttl="4h", prune_mode="aggressive", cache_retention=None)
    rec = _get(app)

    assert rec["knob"] != "context_pruning_ttl"
    assert "Pruning is off" in rec["reasoning"]


# ── The regression locks: stated cause must match prescribed knob ────────────


def test_reasoning_prescribes_the_knob_it_diagnosed_for_invalidation(app):
    """An invalidation diagnosis must prescribe cache_retention, and say so.

    The shipped bug read: "Most cached turns expire before the user replies"
    → raise contextPruning.ttl. Correct diagnosis, inert prescription.
    """
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    reasoning = _get(app)["reasoning"]

    assert "invalidated" in reasoning
    assert "cache_retention" in reasoning
    # The disclaimer is the load-bearing part: an operator reading this card
    # must not walk away thinking the pruning ttl was the cache lever.
    assert "contextPruning.ttl does not" in reasoning


def test_reasoning_for_a_prune_ttl_call_disclaims_any_cache_effect(app):
    """A pruning-coherence fix must not be sold as an invalidation fix."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=5,
    ))
    _write_openclaw(app, prune_ttl="15m", cache_retention="long")
    rec = _get(app)

    assert rec["knob"] == "context_pruning_ttl"
    assert "contextPruning.ttl" in rec["reasoning"]
    assert "only cache_retention moves that number" in rec["reasoning"]


def test_reasoning_states_the_economics_of_the_1h_cache(app):
    """The card carries the 2x-vs-1.25x trade, not just "raise it"."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    reasoning = _get(app)["reasoning"]

    assert "2.00x" in reasoning and "1.25x" in reasoning
    assert "1.65x" in reasoning, "the break-even is quoted, not rounded up to 1.7"
    assert "per_bot_cache_retention" in reasoning, "names the write surface"


# ── Config read ──────────────────────────────────────────────────────────────


def test_hold_when_openclaw_json_missing(app):
    """Can't read config → hold, low confidence, no spurious recommendation."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _MINUTE] * 59,
    ))
    # No openclaw.json written at all.
    rec = _get(app)

    assert rec["verdict"] == "hold"
    assert rec["knob"] is None
    assert rec["current_ttl"] is None
    assert rec["confidence"] == "low"
    assert "Could not read openclaw.json" in rec["reasoning"]


def test_cache_retention_is_read_from_the_per_model_fan_out(app):
    """cacheRetention is materialized per Anthropic model, not top-level."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=5,
    ))
    _write_openclaw(app, prune_ttl="1h", cache_retention="long")
    rec = _get(app)
    assert rec["cache_retention"] == "long"
    assert rec["effective_cache_retention"] == "long"
    assert rec["cache_ttl_seconds"] == 3600


def test_unset_cache_retention_reports_the_oc_default(app):
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[2 * _MINUTE] * 59,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)
    assert rec["cache_retention"] is None
    assert rec["effective_cache_retention"] == "short"
    assert rec["cache_ttl_seconds"] == 300


# ── Impact estimate ──────────────────────────────────────────────────────────


def test_impact_estimate_scales_with_cache_write_tokens(app):
    """A heavier cacheWrite footprint means more to reclaim."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40, cache_write_tokens=50_000,
    ))
    _write_openclaw(app, cache_retention=None)
    heavy = _get(app)["estimated_monthly_savings_usd"]
    assert heavy is not None and heavy > 0

    # Same traffic shape, 25x less cacheWrite volume → proportionally less.
    import shutil
    shutil.rmtree(app.shared_dir / "annotations")
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40, cache_write_tokens=2_000,
    ))
    light = _get(app)["estimated_monthly_savings_usd"]
    assert 0 < light < heavy


# ── Motivating signal id for inline snooze ───────────────────────────────────


def test_motivating_signal_id_set_when_firing_signal_exists(app):
    """A firing session_economics signal rides along so the card can snooze it."""
    from signals import store as signals_store
    from schema.signal import make_signature

    signals_store.observe(
        app.shared_dir,
        signature=make_signature(
            "session_economics", "cache_invalidation_elevated", "admin_bot",
        ),
        producer="session_economics",
        type="cache_invalidation_elevated",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="admin_bot: invalidation elevated",
        details={
            "bot_id": "admin_bot", "window_days": 7,
            "invalidated_count": 40, "participating_count": 60,
            "invalidated_ratio": 0.67, "threshold_ratio": 0.15,
        },
    )
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)

    assert rec["verdict"] == "raise"
    assert rec["motivating_signal_id"], (
        "motivating_signal_id should be populated when a firing signal exists"
    )


def test_motivating_signal_id_null_when_no_signal_firing(app):
    """No signal in the store → null id; rec still computed from cost_events."""
    _write_cost_events(app.shared_dir, "admin_bot", _session(
        gaps=[15 * _MINUTE] * 59, invalidated=40,
    ))
    _write_openclaw(app, cache_retention=None)
    rec = _get(app)

    assert rec["verdict"] == "raise"
    assert rec["motivating_signal_id"] is None
