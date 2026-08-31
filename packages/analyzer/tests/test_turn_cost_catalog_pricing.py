"""ALPHA-7 / audit B6 — price from the catalog, or say you can't. Never $0.00.

Before this, ``usage_analytics._estimate_turn_cost`` priced from a hardcoded
six-provider table (anthropic / openai / google / xai / mistral / groq) and
``return 0.0``-ed for everything else. On a pod whose provider was off-table —
OpenRouter or a local Ollama, both of which ``docs/help/installation.md``
explicitly supports — a day of real turns summed to ``$0.00``, the tile showed
``usd_28d: 0.0`` with ``live_today: true`` (*measured, and it is zero*) against
232 real turns, and, because the spend cap shares the same expression, the cap
sat green on a pod it could not see.

These tests pin the fix at BOTH ends:

  * the resolver — catalog → offline table → ``None`` ("can't price"), and a
    recorded cost still beats any estimate;
  * every one of the EIGHT call sites that used to do
    ``recorded if recorded else _estimate_turn_cost(t)`` and then arithmetic.
    A mutation that reintroduces ``return 0.0`` in the estimator reds the
    per-site tests below, not just one.

The cap decision is asserted as a DECISION (``spend_alert.daily_cap_decision``),
not as a number: the B6 failure was that a day of unpriceable turns *read as
OK*, and a test on the dollar figure alone cannot see that.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import model_economics  # noqa: E402
import provisioning_budget  # noqa: E402
import spend_alert  # noqa: E402
import tile_metrics  # noqa: E402
import turn_cost as tc  # noqa: E402
import usage_analytics as ua  # noqa: E402
from evolve_admin import pod_rollup  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

# An off-table provider the install docs support and the six-entry offline
# table has never known. The whole finding hangs on this provider existing.
OFF_TABLE_PROVIDER = "openrouter"
# The realistic shape: an OpenAI-compatible gateway records the model under its
# VENDOR while ``provider`` names the gateway. Neither token is in the offline
# six-entry table, so this turn is exactly the one that used to read $0.00.
OFF_TABLE_VENDOR = "placeholder-vendor"
OFF_TABLE_MODEL = f"{OFF_TABLE_VENDOR}/placeholder-model-x"
OFF_TABLE_BARE = "placeholder-model-x"
ON_TABLE_MODEL = "anthropic/claude-sonnet-4-5"


def _turn(
    *,
    model: str,
    provider: str,
    ts: str,
    cost: float = 0.0,
    input_tokens: int = 1_000_000,
    output_tokens: int = 1_000_000,
    session_id: str = "s-1",
    instance: str = "placeholder_bot",
    source: str = "human",
) -> dict:
    """One turn record in the shape TurnObserver writes."""
    return {
        "ts": ts,
        "model": model,
        "provider": provider,
        "cost": cost,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "session_id": session_id,
        "instance": instance,
        "source": source,
        "channel": "placeholder-channel",
    }


def _write_catalog(shared_dir: Path) -> Path:
    """A minimal ``{shared_dir}/model-pricing.json`` in the normalized shape
    ``model_pricing.build_pricing_cache`` emits ($/token, not $/MTok)."""
    doc = {
        "refreshed_at": "2026-08-25T00:00:00Z",
        "sources": {"primary": "litellm", "cross_check": "models.dev"},
        "models": [
            {
                # The NATIVE vendor row LiteLLM keeps (it skips the openrouter
                # re-host surface, so this is the only row for this model).
                "provider": OFF_TABLE_VENDOR,
                "model_id": OFF_TABLE_BARE,
                # $2.00 / MTok in, $8.00 / MTok out.
                "input_cost_per_token": 0.000002,
                "output_cost_per_token": 0.000008,
                "context_window": 200000,
                "family": None,
                "source": "litellm",
            },
            {
                # A row the gateway itself is listed under — the direct hit.
                "provider": OFF_TABLE_PROVIDER,
                "model_id": "placeholder-gateway-native",
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000004,
                "context_window": 100000,
                "family": None,
                "source": "models.dev",
            },
        ],
        "degraded": [],
    }
    path = shared_dir / "model-pricing.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A shared dir the pricing resolver reads from, with no catalog yet."""
    monkeypatch.setenv("EVOLVE_SHARED", str(tmp_path))
    tc.reset_pricing_catalog_cache()
    yield tmp_path
    tc.reset_pricing_catalog_cache()


@pytest.fixture
def pod_with_catalog(pod):
    _write_catalog(pod)
    tc.reset_pricing_catalog_cache()
    return pod


# ── The resolver ──────────────────────────────────────────────────────────────


def test_off_table_provider_prices_from_the_catalog(pod_with_catalog):
    """openrouter is in NEITHER offline table, but the catalog knows it."""
    t = _turn(
        model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T10:00:00Z",
    )
    cost = tc.estimate_turn_cost(t)
    assert cost is not None, "catalog record must price this turn"
    # 1 MTok in @ $2 + 1 MTok out @ $8.
    assert cost == pytest.approx(10.00, abs=0.001)


def test_gateway_native_row_is_the_direct_catalog_hit(pod_with_catalog):
    """When the catalog lists the gateway itself, that row wins outright."""
    t = _turn(
        model="placeholder-gateway-native", provider=OFF_TABLE_PROVIDER,
        ts="2026-08-25T10:00:00Z",
    )
    assert tc.estimate_turn_cost(t) == pytest.approx(5.00, abs=0.001)


def test_provider_prefixed_model_is_not_looked_up_under_a_doubled_prefix(
    pod_with_catalog,
):
    """``openrouter/placeholder-vendor/placeholder-model-x`` must resolve to the
    same vendor row as the un-prefixed form."""
    t = _turn(
        model=f"{OFF_TABLE_PROVIDER}/{OFF_TABLE_MODEL}",
        provider=OFF_TABLE_PROVIDER, ts="2026-08-25T10:00:00Z",
    )
    assert tc.estimate_turn_cost(t) == pytest.approx(10.00, abs=0.001)


def test_off_table_provider_without_a_catalog_says_cant_price(pod):
    """No catalog, no table entry → None. NOT 0.0 — that is the whole finding."""
    t = _turn(
        model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T10:00:00Z",
    )
    assert tc.estimate_turn_cost(t) is None
    assert tc.turn_cost(t) is None


def test_unpriced_turn_contributes_nothing_rather_than_zero(pod):
    """An unpriced turn is COUNTED, never summed as a real $0."""
    turns = [
        _turn(model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T10:00:00Z")
        for _ in range(3)
    ]
    total = tc.sum_turn_costs(turns)
    assert total.unpriced_turns == 3
    assert total.priced_turns == 0
    assert total.usd == 0.0
    assert total.measurable is False
    assert total.unpriced_providers == (OFF_TABLE_PROVIDER,)


def test_on_table_provider_still_prices_offline(pod):
    """No regression: the six-entry table is still the OFFLINE fallback."""
    t = _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T10:00:00Z")
    cost = tc.estimate_turn_cost(t)
    # 1 MTok in @ $3 + 1 MTok out @ $15 from the offline table.
    assert cost == pytest.approx(18.00, abs=0.001)


def test_recorded_cost_beats_the_estimate(pod_with_catalog):
    """A recorded non-zero cost always wins — catalog present or not."""
    t = _turn(
        model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
        ts="2026-08-25T10:00:00Z", cost=0.42,
    )
    assert tc.turn_cost(t) == pytest.approx(0.42)
    unpriceable = _turn(
        model="mystery-model", provider="mystery-provider",
        ts="2026-08-25T10:00:00Z", cost=1.25,
    )
    assert tc.turn_cost(unpriceable) == pytest.approx(1.25)


def test_catalog_beats_the_offline_table_for_a_known_provider(pod):
    """The catalog is consulted FIRST — the offline table is the fallback, not
    the authority. Widening the table is never a substitute for reading it."""
    doc = {
        "models": [{
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000001,
        }],
    }
    (pod / "model-pricing.json").write_text(json.dumps(doc))
    tc.reset_pricing_catalog_cache()
    t = _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T10:00:00Z")
    # $1/MTok both ways from the catalog, not $3/$15 from the table.
    assert tc.estimate_turn_cost(t) == pytest.approx(2.00, abs=0.001)


def test_unpriced_note_is_operator_legible(pod):
    assert tc.unpriced_note(0) == ""
    assert tc.unpriced_note(1) == "can't price 1 turn"
    assert tc.unpriced_note(41, ["openrouter"]) == "can't price 41 turns (openrouter)"


def test_estimator_returns_zero_only_for_a_real_zero_price(pod):
    """A locally-hosted model the catalog prices at $0 is a MEASURED zero and
    must stay 0.0 — the sentinel is reserved for "no price exists"."""
    doc = {
        "models": [{
            "provider": "placeholder-local",
            "model_id": "placeholder-local-model",
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
        }],
    }
    (pod / "model-pricing.json").write_text(json.dumps(doc))
    tc.reset_pricing_catalog_cache()
    t = _turn(
        model="placeholder-local-model", provider="placeholder-local",
        ts="2026-08-25T10:00:00Z",
    )
    assert tc.estimate_turn_cost(t) == 0.0


# ── Call site 1 + 2: usage_analytics.compute_summary ───────────────────────────


def test_compute_summary_reports_unpriced_instead_of_a_confident_total(pod):
    """The Usage page's own numbers. 232 turns, none priceable — the summary
    must say so rather than print $0.00 as if it were the spend."""
    turns = [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts=f"2026-08-25T{h:02d}:00:00Z", session_id=f"s-{h}",
        )
        for h in range(10)
    ]
    summary = ua.compute_summary(turns)
    assert summary["total_turns"] == 10
    assert summary["unpriced_turns"] == 10
    assert summary["unpriced_providers"] == [OFF_TABLE_PROVIDER]
    assert summary["unpriced_note"] == "can't price 10 turns (openrouter)"
    assert summary["total_cost"] == 0.0  # a floor, and the note says so
    # Call site 2 — the by_provider / by_source split (_tcost). Calls are
    # counted, dollars are not invented.
    assert summary["billing"]["by_provider"][OFF_TABLE_PROVIDER]["calls"] == 10
    assert summary["billing"]["by_provider"][OFF_TABLE_PROVIDER]["cost"] == 0.0


def test_compute_summary_prices_the_off_table_provider_from_the_catalog(
    pod_with_catalog,
):
    turns = [
        _turn(model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T01:00:00Z"),
        _turn(model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T02:00:00Z"),
    ]
    summary = ua.compute_summary(turns)
    assert summary["unpriced_turns"] == 0
    assert summary["unpriced_note"] == ""
    assert summary["total_cost"] == pytest.approx(20.00, abs=0.01)


def test_compute_summary_partial_pricing_keeps_the_priced_dollars(pod):
    """A mixed day: the anthropic turn prices offline, the openrouter one
    cannot. The total is the priced part AND the count is carried out."""
    turns = [
        _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T01:00:00Z"),
        _turn(model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T02:00:00Z"),
    ]
    summary = ua.compute_summary(turns)
    assert summary["total_cost"] == pytest.approx(18.00, abs=0.01)
    assert summary["unpriced_turns"] == 1


# ── Call site 3: spend_alert — the cap ────────────────────────────────────────


def _stage_bot_turns(shared_dir: Path, bot_id: str, day: date, turns: list[dict]) -> None:
    d = shared_dir / bot_id / "turns"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"turns-{day.isoformat()}.jsonl").write_text(
        "\n".join(json.dumps(t) for t in turns) + "\n"
    )


@pytest.fixture
def live_turns(pod, monkeypatch):
    """Point usage_analytics' turn-dir discovery at the staged shared dir.

    Also pins the POD TIMEZONE to UTC. That is not incidental: tile_metrics
    buckets turns by the pod-LOCAL day of each ``ts``, so a test staging
    ``2026-08-25T01:00:00Z`` gets a different day on a Pacific laptop than on
    a UTC CI runner. Before the pin these tests were quietly TZ-dependent —
    green in CI, red on a developer machine west of UTC. Pinning makes the
    day arithmetic explicit and the same everywhere; the tests that exercise
    the local-day conversion itself pin a NON-UTC zone on purpose (see
    test_tile_local_day_bucketing.py).
    """
    def fake_find_turns_dirs(bot_id: str, network_path: str | None = None):
        return [pod / bot_id / "turns"]

    monkeypatch.setattr(ua, "_find_turns_dirs", fake_find_turns_dirs)
    monkeypatch.setattr(tile_metrics, "_pod_tz", lambda: timezone.utc)
    return pod


def test_load_today_spend_detail_separates_zero_from_cant_price(live_turns):
    day = date(2026, 8, 25)
    now = datetime(2026, 8, 25, 23, 59, tzinfo=timezone.utc)
    _stage_bot_turns(live_turns, "placeholder_bot", day, [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts=f"2026-08-25T{h:02d}:00:00Z",
        )
        for h in range(12)
    ])
    detail = spend_alert.load_today_spend_detail(
        live_turns, "placeholder_bot", day, now=now,
    )
    assert detail is not None
    assert detail.unpriced_turns == 12
    assert detail.priced_turns == 0
    assert detail.measurable is False
    assert detail.usd == 0.0


def test_the_cap_does_not_read_a_day_of_unpriced_turns_as_ok(live_turns):
    """THE finding, asserted on the DECISION.

    A day of real, unpriceable turns must not come back ``ok`` — before B6 it
    did, at ``$0.00``, and the operator's cap sat green while the pod spent.
    """
    day = date(2026, 8, 25)
    now = datetime(2026, 8, 25, 23, 59, tzinfo=timezone.utc)
    _stage_bot_turns(live_turns, "placeholder_bot", day, [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts=f"2026-08-25T{h:02d}:00:00Z",
        )
        for h in range(24)
    ])
    detail = spend_alert.load_today_spend_detail(
        live_turns, "placeholder_bot", day, now=now,
    )
    ladder = {"tier_downgrade": 2.0, "l1_breaker": 5.0, "l2_breaker": 10.0}
    decision = spend_alert.daily_cap_decision(detail, threshold=5.0, ladder=ladder)

    assert decision["verdict"] == "unmeasurable"
    assert decision["verdict"] != "ok"
    assert decision["measurable"] is False
    assert decision["unpriced_turns"] == 24
    assert decision["unpriced_providers"] == (OFF_TABLE_PROVIDER,)


def test_the_cap_still_trips_on_the_priced_floor_of_a_partly_unpriced_day(
    live_turns,
):
    """A floor over the cap is still over the cap — the ladder rungs the priced
    portion crosses are tripped even though the day is unmeasurable."""
    day = date(2026, 8, 25)
    now = datetime(2026, 8, 25, 23, 59, tzinfo=timezone.utc)
    _stage_bot_turns(live_turns, "placeholder_bot", day, [
        # $18 of priceable anthropic spend...
        _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T01:00:00Z"),
        # ...plus a turn nothing can price.
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T02:00:00Z",
        ),
    ])
    detail = spend_alert.load_today_spend_detail(
        live_turns, "placeholder_bot", day, now=now,
    )
    ladder = {"tier_downgrade": 2.0, "l1_breaker": 5.0, "l2_breaker": 10.0}
    decision = spend_alert.daily_cap_decision(detail, threshold=5.0, ladder=ladder)
    assert decision["verdict"] == "unmeasurable"
    assert decision["tripped"] == ["tier_downgrade", "l1_breaker", "l2_breaker"]


def test_a_fully_priced_quiet_day_is_still_plain_ok(live_turns):
    """The real-zero case must NOT be swept up as unmeasurable."""
    day = date(2026, 8, 25)
    now = datetime(2026, 8, 25, 23, 59, tzinfo=timezone.utc)
    _stage_bot_turns(live_turns, "placeholder_bot", day, [
        _turn(
            model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T01:00:00Z",
            input_tokens=0, output_tokens=0,
        ),
    ])
    detail = spend_alert.load_today_spend_detail(
        live_turns, "placeholder_bot", day, now=now,
    )
    decision = spend_alert.daily_cap_decision(detail, threshold=5.0, ladder={})
    assert decision["verdict"] == "ok"
    assert decision["measurable"] is True


def test_cap_decision_keeps_load_failure_distinct_from_unmeasurable(pod):
    decision = spend_alert.daily_cap_decision(None, threshold=5.0, ladder={})
    assert decision["verdict"] == "load_failed"
    assert decision["usd"] is None


def test_unpriced_signal_is_emitted_for_an_unmeasurable_day(pod, monkeypatch):
    """Unpriced is a REPORTED state, not a silent one."""
    observed: list[dict] = []

    class _FakeSig:
        id = "sig-placeholder"

    class _FakeStore:
        @staticmethod
        def observe(shared_dir, **kw):
            observed.append(kw)
            return _FakeSig()

    monkeypatch.setitem(sys.modules, "signals.store", _FakeStore)
    import signals  # noqa: F401
    monkeypatch.setattr("signals.store", _FakeStore, raising=False)

    day = spend_alert.DaySpend(
        usd=0.0, priced_turns=0, unpriced_turns=232,
        unpriced_providers=(OFF_TABLE_PROVIDER,),
    )
    sig_id = spend_alert.emit_unpriced_spend_signal(
        shared_dir=pod, bot_id="placeholder_bot", day=day,
        cap_usd=5.0, today=date(2026, 8, 25),
    )
    assert sig_id == "sig-placeholder"
    assert observed, "an unmeasurable cap day must raise a Signal"
    kw = observed[0]
    assert kw["type"] == "spend_unpriced_turns"
    # Nothing priced at all ⇒ the cap is fully blind for this bot.
    assert kw["severity"] == "alert"
    assert "can't price 232 turns" in kw["title"]
    assert kw["details"]["unpriced_turns"] == 232
    assert kw["details"]["unpriced_providers"] == [OFF_TABLE_PROVIDER]


def test_unpriced_signal_is_not_emitted_for_a_fully_priced_day(pod):
    day = spend_alert.DaySpend(usd=1.0, priced_turns=4, unpriced_turns=0)
    assert spend_alert.emit_unpriced_spend_signal(
        shared_dir=pod, bot_id="placeholder_bot", day=day,
        cap_usd=5.0, today=date(2026, 8, 25),
    ) is None


# ── Call sites 4 + 5: tile_metrics ────────────────────────────────────────────


def test_tile_live_today_overlay_counts_unpriced(live_turns, monkeypatch):
    today = date(2026, 8, 25)
    _stage_bot_turns(live_turns, "placeholder_bot", today, [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T01:00:00Z",
        ),
        _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T02:00:00Z"),
    ])
    tile_metrics._LIVE_OVERLAY_CACHE.clear()
    out = tile_metrics._live_today_overlay("placeholder_bot", today)
    assert out["turns_today"] == 2
    assert out["unpriced_today"] == 1
    assert out["unpriced_providers"] == [OFF_TABLE_PROVIDER]
    # The one priceable turn's dollars are still there — unpriced does not
    # zero the window, it annotates it.
    assert out["cost_today"] == pytest.approx(18.00, abs=0.01)


def test_tile_live_window_costs_counts_unpriced_per_date(live_turns):
    today = date(2026, 8, 25)
    _stage_bot_turns(live_turns, "placeholder_bot", today, [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T01:00:00Z",
        ),
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T02:00:00Z",
        ),
    ])
    tile_metrics._LIVE_WINDOW_CACHE.clear()
    out = tile_metrics._live_window_costs("placeholder_bot", today, days=2)
    slot = out["per_date"]["2026-08-25"]
    assert slot["turns"] == 2
    assert slot["unpriced"] == 2
    assert slot["cost"] == 0.0
    assert out["unpriced_providers"] == [OFF_TABLE_PROVIDER]


# ── The injected-clock contract ───────────────────────────────────────────────
#
# The two tests above are pinned to 2026-08-25, and for ~2 days after that
# date they passed for the WRONG REASON. Both call sites bucket their results
# against the injected ``today`` but used to LOAD via ``load_turns(bot_id,
# days=N)`` with no ``end_date`` — and ``load_turns`` defaults that to
# ``datetime.now()``. So the window actually read followed the wall clock.
# While the real date stayed within ``days`` of the fixture the staged file
# was still picked up and everything looked fine; on 2026-08-27 a 2-day
# window became {08-26, 08-27}, the fixture fell out of it, and both tests
# went red on every open PR at once — with ``origin/main`` unchanged.
#
# A test that only passes "near today" cannot see that. These two inject a
# date far outside any plausible real-clock window, so they fail closed if
# the ``end_date`` forwarding is ever dropped again.


def _stage_two_priceable_turns(root, day: date) -> None:
    _stage_bot_turns(root, "placeholder_bot", day, [
        _turn(model=ON_TABLE_MODEL, provider="anthropic",
              ts=f"{day.isoformat()}T01:00:00Z"),
        _turn(model=ON_TABLE_MODEL, provider="anthropic",
              ts=f"{day.isoformat()}T02:00:00Z"),
    ])


def test_live_today_overlay_reads_the_injected_date_not_the_wall_clock(live_turns):
    """``today`` must reach the LOADER, not just the bucketing.

    2020 is unreachable from any real ``datetime.now()`` window, so this
    passes only if ``end_date`` is pinned to the caller's ``today``.
    """
    day = date(2020, 1, 15)
    _stage_two_priceable_turns(live_turns, day)

    tile_metrics._LIVE_OVERLAY_CACHE.clear()
    out = tile_metrics._live_today_overlay("placeholder_bot", day)

    assert out["turns_today"] == 2, (
        "the injected `today` did not reach load_turns — the window read is "
        "following the wall clock again (see _utc_end_for_local_day)"
    )
    assert out["cost_today"] > 0.0


def test_live_window_costs_reads_the_injected_date_not_the_wall_clock(live_turns):
    """Same contract for the per-date window."""
    day = date(2020, 1, 15)
    _stage_two_priceable_turns(live_turns, day)

    tile_metrics._LIVE_WINDOW_CACHE.clear()
    out = tile_metrics._live_window_costs("placeholder_bot", day, days=2)

    assert "2020-01-15" in out["per_date"], (
        "the injected `today` did not reach load_turns — the window read is "
        "following the wall clock again (see _utc_end_for_local_day)"
    )
    assert out["per_date"]["2020-01-15"]["turns"] == 2


# ── Call site 6: pod_rollup ───────────────────────────────────────────────────


def test_pod_rollup_carries_unpriced_per_date(pod):
    today = date(2026, 8, 25)
    _stage_bot_turns(pod, "placeholder_bot", today, [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T01:00:00Z",
        ),
        _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T02:00:00Z"),
    ])
    out = pod_rollup._load_live_daily_costs(pod, "placeholder_bot", days=2, today=today)
    assert out["ok"] is True
    slot = out["per_date"]["2026-08-25"]
    assert slot["turns"] == 2
    assert slot["unpriced"] == 1
    assert slot["usd"] == pytest.approx(18.00, abs=0.01)
    assert out["unpriced_providers"] == [OFF_TABLE_PROVIDER]


def test_pod_rollup_live_surfaces_the_pod_wide_unpriced_count(pod):
    today = date(2026, 8, 25)
    _stage_bot_turns(pod, "placeholder_bot", today, [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T01:00:00Z",
        ),
    ])
    roll = pod_rollup.compute_spend_rollup_live(
        pod, ["placeholder_bot"], today=today,
        now=datetime(2026, 8, 25, 12, 0),
    )
    assert roll["source"] == "live_jsonl"
    assert roll["pod_unpriced_28d"] == 1
    assert roll["pod_unpriced_providers"] == [OFF_TABLE_PROVIDER]
    assert roll["by_bot"]["placeholder_bot"]["unpriced_28d"] == 1


# ── Call site 7: provisioning_budget ──────────────────────────────────────────


def test_window_spend_refuses_to_report_an_unmeasurable_standup_window(
    live_turns, monkeypatch,
):
    """The provisioning ceiling must not read unpriceable standup spend as
    headroom. ``None`` is the module's documented "can't read the ledger"
    state; a priced floor masquerading as the total is not."""
    created = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 25, 23, 0, tzinfo=timezone.utc)
    _stage_bot_turns(live_turns, "placeholder_bot", created.date(), [
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T01:00:00Z",
        ),
    ])
    spent = provisioning_budget.window_spend_usd(
        "placeholder_bot", created_at=created, now=now, window_days=7,
    )
    assert spent is None


def test_window_spend_still_sums_a_fully_priced_window(live_turns):
    created = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 25, 23, 0, tzinfo=timezone.utc)
    _stage_bot_turns(live_turns, "placeholder_bot", created.date(), [
        _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T01:00:00Z"),
    ])
    spent = provisioning_budget.window_spend_usd(
        "placeholder_bot", created_at=created, now=now, window_days=7,
    )
    assert spent == pytest.approx(18.00, abs=0.01)


def test_provisioning_turn_cost_no_longer_manufactures_zero(pod):
    """The module's ImportError shim used to return 0.0 — a SECOND source of
    the silent zero, on the very path that gates new-bot spend."""
    t = _turn(
        model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T01:00:00Z",
    )
    assert provisioning_budget._turn_cost(t) is None


def test_spend_alert_turn_cost_no_longer_manufactures_zero(pod):
    t = _turn(
        model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER, ts="2026-08-25T01:00:00Z",
    )
    assert spend_alert._turn_cost(t) is None


# ── Call site 8: model_economics ──────────────────────────────────────────────


def test_bot_model_matrix_does_not_dilute_cost_per_turn_with_unpriced(pod):
    turns = [
        _turn(model=ON_TABLE_MODEL, provider="anthropic", ts="2026-08-25T01:00:00Z"),
        _turn(
            model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER,
            ts="2026-08-25T02:00:00Z",
        ),
    ]
    matrix, _ = model_economics._bot_model_matrix(turns)
    by_model = {c["model_id"]: c for c in matrix}

    priced = by_model["claude-sonnet-4-5"]
    assert priced["calls"] == 1
    assert priced["unpriced"] == 0
    assert priced["cost_per_turn"] == pytest.approx(18.00, abs=0.01)

    unpriceable = by_model[OFF_TABLE_BARE]
    assert unpriceable["calls"] == 1
    assert unpriceable["unpriced"] == 1
    # No price for any call in the cell → no rate, rather than a $0.00 rate.
    assert unpriceable["cost_per_turn"] is None


# ── Cache-token rates under catalog pricing ───────────────────────────────────
#
# A cache-heavy agent turn is mostly cache-READ tokens, billed at roughly a
# tenth of the input rate. Pricing them at the full input rate inflates the
# total by an order of magnitude — a false cap trip is as damaging as the
# silent zero B6 is about, so the resolution order gets its own tests.


def _cached_turn(**kw) -> dict:
    t = _turn(
        ts="2026-08-25T10:00:00Z", input_tokens=0, output_tokens=0, **kw,
    )
    t["cache_read_tokens"] = 1_000_000
    t["cache_write_tokens"] = 1_000_000
    return t


def test_catalog_cache_rates_are_used_when_the_catalog_carries_them(pod):
    (pod / "model-pricing.json").write_text(json.dumps({"models": [{
        "provider": OFF_TABLE_VENDOR,
        "model_id": OFF_TABLE_BARE,
        "input_cost_per_token": 0.000010,
        "output_cost_per_token": 0.000040,
        "cache_read_cost_per_token": 0.000001,   # $1 / MTok
        "cache_write_cost_per_token": 0.000012,  # $12 / MTok
    }]}))
    tc.reset_pricing_catalog_cache()
    t = _cached_turn(model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER)
    # 1 MTok cache-write @ $12 + 1 MTok cache-read @ $1 — NOT 2 × the $10 input.
    assert tc.estimate_turn_cost(t) == pytest.approx(13.00, abs=0.001)


def test_offline_cache_columns_fill_in_for_a_catalog_without_them(pod):
    """The pod's existing catalog predates the cache columns. A model the
    offline table knows must keep the table's real cache rates rather than
    jump to the input rate."""
    (pod / "model-pricing.json").write_text(json.dumps({"models": [{
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-5",
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
    }]}))
    tc.reset_pricing_catalog_cache()
    t = _cached_turn(model=ON_TABLE_MODEL, provider="anthropic")
    # Offline table cache columns: write $3.75 / MTok, read $0.30 / MTok.
    assert tc.estimate_turn_cost(t) == pytest.approx(4.05, abs=0.001)


def test_input_rate_is_the_last_resort_for_cache_tokens(pod):
    """No catalog cache rate AND no offline row → the model's own input rate.
    Over-stating is the safe direction for a cap; understating is the B6 shape."""
    (pod / "model-pricing.json").write_text(json.dumps({"models": [{
        "provider": OFF_TABLE_VENDOR,
        "model_id": OFF_TABLE_BARE,
        "input_cost_per_token": 0.000002,
        "output_cost_per_token": 0.000008,
    }]}))
    tc.reset_pricing_catalog_cache()
    t = _cached_turn(model=OFF_TABLE_MODEL, provider=OFF_TABLE_PROVIDER)
    assert tc.estimate_turn_cost(t) == pytest.approx(4.00, abs=0.001)


def test_normalizers_carry_cache_rates_into_the_catalog(pod):
    """The catalog must be ABLE to carry cache rates — otherwise the fallback
    above is the permanent path and the finer-grained zero never gets fixed."""
    import model_pricing as mp

    litellm = mp.normalize_litellm({
        "placeholder-vendor/placeholder-model-x": {
            "litellm_provider": "placeholder-vendor",
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_token_cost": 0.0000003,
            "cache_creation_input_token_cost": 0.00000375,
        },
    })
    assert litellm[0].cache_read_cost_per_token == pytest.approx(0.0000003)
    assert litellm[0].cache_write_cost_per_token == pytest.approx(0.00000375)

    modelsdev = mp.normalize_modelsdev({
        "placeholder-vendor": {"models": {"placeholder-model-y": {
            "cost": {
                "input": 3.0, "output": 15.0,
                "cache_read": 0.3, "cache_write": 3.75,
            },
        }}},
    })
    assert modelsdev[0].cache_read_cost_per_token == pytest.approx(0.0000003)
    assert modelsdev[0].cache_write_cost_per_token == pytest.approx(0.00000375)


def test_build_cache_borrows_cache_rates_from_the_cross_check(pod):
    """LiteLLM stays primary on input/output, but a row with no cache rate
    borrows models.dev's rather than leaving the cost layer to guess."""
    import model_pricing as mp

    doc = mp.build_pricing_cache(
        litellm_raw={
            "placeholder-vendor/placeholder-model-x": {
                "litellm_provider": "placeholder-vendor",
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000015,
            },
        },
        modelsdev_raw={
            "placeholder-vendor": {"models": {"placeholder-model-x": {
                "cost": {
                    "input": 9.99, "output": 9.99,
                    "cache_read": 0.3, "cache_write": 3.75,
                },
            }}},
        },
        refreshed_at="2026-08-25T00:00:00Z",
    )
    rec = doc["models"][0]
    # LiteLLM wins on input/output...
    assert rec["input_cost_per_token"] == pytest.approx(0.000003)
    # ...and borrows the cache rates it does not have.
    assert rec["cache_read_cost_per_token"] == pytest.approx(0.0000003)
    assert rec["cache_write_cost_per_token"] == pytest.approx(0.00000375)
