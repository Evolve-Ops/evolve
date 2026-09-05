"""Tests for model_economics.assemble_model_economics — the pod-wide,
model-centric cost leaderboard (Phase 13 v1; spec
internal/spec-model-economics-page-2026-06-13.md).

Pins:
  1. list-price join (per-token cache → per-1k) + cache-miss → None.
  2. $/turn = cost ÷ turns; share-of-spend = cost ÷ Σcost.
  3. eff-vs-list delta math (None when list unknown).
  4. bot_count + recency passthrough from by_model.
  5. low_confidence passthrough (low-sample row NOT hidden).
  6. human-% join from by_model_by_audience.
  7. configured-but-unused models included with volume 0.
  8. default sort = $/turn desc.

Placeholder model/provider names only (no real pod bot or provider names) so
the scrub guard stays green and no provider literal lands in the assertions.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_economics as me  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────


def _by_model_row(model, *, cost, calls, blended=None, billed=20_000,
                  low_conf=False, bot_count=1, last_ts="2026-06-10T01:00:00Z",
                  inp=None, out=None):
    return {
        "model": model,
        "calls": calls,
        "cost": cost,
        "usd_per_1k_blended": blended,
        "usd_per_1k_input": inp,
        "usd_per_1k_output": out,
        "billed_tokens": billed,
        "low_confidence": low_conf,
        "bot_count": bot_count,
        "last_used_ts": last_ts,
    }


def _aud_row(model, *, total, human):
    return {
        "model": model,
        "total_calls": total,
        "total_cost": 0.0,
        "human": {"calls": human, "cost": 0.0},
        "non_human": {"calls": total - human, "cost": 0.0},
    }


def _pricing_cache(*models):
    """models: (provider, bare_id, in_per_token, out_per_token)."""
    return {
        "models": [
            {
                "provider": p,
                "model_id": mid,
                "input_cost_per_token": ic,
                "output_cost_per_token": oc,
            }
            for (p, mid, ic, oc) in models
        ]
    }


def _summary(by_model, by_aud=None, total_cost=None):
    tc = total_cost if total_cost is not None else sum(m["cost"] for m in by_model)
    return {
        "by_model": by_model,
        "by_model_by_audience": by_aud or [],
        "total_cost": tc,
        "total_turns": sum(m["calls"] for m in by_model),
    }


# ── list-price join ──────────────────────────────────────────────────────────


def test_list_price_join_per_token_to_per_1k():
    s = _summary([_by_model_row("provx/alpha-1", cost=10.0, calls=100, blended=0.02)])
    # 0.000015 $/token → 0.015 $/1k.
    cache = _pricing_cache(("provx", "alpha-1", 0.000015, 0.000075))
    out = me.assemble_model_economics(s, pricing_cache=cache, network={})
    row = out["rows"][0]
    assert row["list_per_1k_input"] == 0.015
    assert row["list_per_1k_output"] == 0.075
    assert out["has_pricing"] is True


def test_list_price_cache_miss_is_none():
    s = _summary([_by_model_row("provx/missing-9", cost=5.0, calls=50, blended=0.03)])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    row = out["rows"][0]
    assert row["list_per_1k_input"] is None
    assert row["list_per_1k_output"] is None
    assert row["eff_vs_list_delta"] is None
    assert out["has_pricing"] is False


# ── $/turn + share ───────────────────────────────────────────────────────────


def test_cost_per_turn_and_share_of_spend():
    s = _summary([
        _by_model_row("provx/alpha-1", cost=30.0, calls=100, blended=0.02),
        _by_model_row("provy/beta-2", cost=10.0, calls=200, blended=0.01),
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    by_id = {r["model_id"]: r for r in out["rows"]}
    assert by_id["alpha-1"]["cost_per_turn"] == 0.3   # 30/100
    assert by_id["beta-2"]["cost_per_turn"] == 0.05   # 10/200
    # share of 40 total.
    assert by_id["alpha-1"]["share_of_spend"] == 0.75
    assert by_id["beta-2"]["share_of_spend"] == 0.25


def test_default_sort_is_cost_per_turn_desc():
    s = _summary([
        _by_model_row("provy/cheap", cost=10.0, calls=1000, blended=0.01),   # $0.01/turn
        _by_model_row("provx/pricey", cost=30.0, calls=100, blended=0.02),   # $0.30/turn
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    assert [r["model_id"] for r in out["rows"]] == ["pricey", "cheap"]


# ── eff-vs-list delta ────────────────────────────────────────────────────────


def test_eff_vs_list_delta_negative_when_cached_below_list():
    # eff blended 0.02; list midpoint = (0.015+0.075)/2 = 0.045 → delta -0.025.
    s = _summary([_by_model_row("provx/alpha-1", cost=10.0, calls=100, blended=0.02)])
    cache = _pricing_cache(("provx", "alpha-1", 0.000015, 0.000075))
    out = me.assemble_model_economics(s, pricing_cache=cache, network={})
    assert out["rows"][0]["eff_vs_list_delta"] == -0.025


def test_eff_vs_list_delta_none_when_list_unknown():
    s = _summary([_by_model_row("provx/alpha-1", cost=10.0, calls=100, blended=0.02)])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    assert out["rows"][0]["eff_vs_list_delta"] is None


# ── bot-count + recency passthrough ──────────────────────────────────────────


def test_bot_count_and_recency_passthrough():
    s = _summary([_by_model_row(
        "provx/alpha-1", cost=10.0, calls=100, blended=0.02,
        bot_count=3, last_ts="2026-06-12T09:30:00Z",
    )])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    row = out["rows"][0]
    assert row["bot_count"] == 3
    assert row["last_used_ts"] == "2026-06-12T09:30:00Z"


# ── confidence passthrough ───────────────────────────────────────────────────


def test_low_confidence_passthrough_and_row_not_hidden():
    s = _summary([
        _by_model_row("provx/tiny", cost=0.01, calls=2, blended=0.5,
                      billed=600, low_conf=True),
        _by_model_row("provy/big", cost=10.0, calls=500, blended=0.02,
                      billed=500_000, low_conf=False),
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    by_id = {r["model_id"]: r for r in out["rows"]}
    # Low-sample row is PRESENT (never hidden) and flagged.
    assert by_id["tiny"]["low_confidence"] is True
    assert by_id["big"]["low_confidence"] is False
    assert len(out["rows"]) == 2


# ── human-% join ─────────────────────────────────────────────────────────────


def test_human_pct_join_from_audience():
    s = _summary(
        [_by_model_row("provx/alpha-1", cost=10.0, calls=100, blended=0.02)],
        by_aud=[_aud_row("provx/alpha-1", total=100, human=40)],
    )
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    assert out["rows"][0]["human_pct"] == 40.0


def test_human_pct_none_without_audience_row():
    s = _summary([_by_model_row("provx/alpha-1", cost=10.0, calls=100, blended=0.02)])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    assert out["rows"][0]["human_pct"] is None


# ── configured-but-unused inclusion ──────────────────────────────────────────


def test_configured_but_unused_models_included_volume_zero():
    # A pod catalog with two configured models; only one has usage. The other
    # must appear in `unused` at volume 0.
    network = {
        "models": {
            "rungs": [
                {"id": "lo", "costClass": "low",
                 "models": ["provx/used-lo", "provx/unused-lo"]},
            ],
            "roles": {"fast": "lo"},
        }
    }
    s = _summary([_by_model_row("provx/used-lo", cost=5.0, calls=100, blended=0.01)])
    out = me.assemble_model_economics(s, pricing_cache=None, network=network)
    used_ids = {r["model_id"] for r in out["rows"]}
    unused_ids = {r["model_id"] for r in out["unused"]}
    assert "used-lo" in used_ids
    assert "unused-lo" in unused_ids
    unused_row = next(r for r in out["unused"] if r["model_id"] == "unused-lo")
    assert unused_row["turns"] == 0
    assert unused_row["total_cost"] == 0.0
    assert unused_row["band"] == "low"
    assert "fast" in unused_row["roles"]
    # The used configured model is marked as configured on its leaderboard row.
    used_row = next(r for r in out["rows"] if r["model_id"] == "used-lo")
    assert used_row["configured"] is True


def test_unused_excludes_models_that_have_usage():
    network = {
        "models": {
            "rungs": [{"id": "lo", "costClass": "low",
                       "models": ["provx/used-lo"]}],
            "roles": {"fast": "lo"},
        }
    }
    s = _summary([_by_model_row("provx/used-lo", cost=5.0, calls=100, blended=0.01)])
    out = me.assemble_model_economics(s, pricing_cache=None, network=network)
    assert all(r["model_id"] != "used-lo" for r in out["unused"])


# ── v1.5 — merge-by-identity ─────────────────────────────────────────────────
# A richer by_model row that carries the input/output token detail the merge
# recomputes the per-1k rates from (every live by_model row carries these).


def _bm(model, *, cost, calls, inp, out, bot_count=1,
        last_ts="2026-06-10T01:00:00Z"):
    billed = inp + out
    return {
        "model": model,
        "calls": calls,
        "cost": cost,
        "input_tokens": inp,
        "output_tokens": out,
        # Precomputed rates left None: the merge MUST recompute from the summed
        # token legs, never read these stale per-series figures.
        "usd_per_1k_blended": None,
        "usd_per_1k_input": None,
        "usd_per_1k_output": None,
        "billed_tokens": billed,
        "low_confidence": billed < 10_000,
        "bot_count": bot_count,
        "last_used_ts": last_ts,
    }


def _anchor_catalog():
    """Four placeholder band anchors (low/medium/high/premium), orders of
    magnitude apart, so resolve_band has a real price scale without any real
    provider/model name."""
    return {
        "rungs": [
            {"id": "lo", "costClass": "low",     "models": ["provz/a-lo"]},
            {"id": "md", "costClass": "medium",  "models": ["provz/a-md"]},
            {"id": "hi", "costClass": "high",    "models": ["provz/a-hi"]},
            {"id": "pr", "costClass": "premium", "models": ["provz/a-pr"]},
        ],
        "roles": {"fast": "lo", "standard": "md", "power": "hi", "max": "pr"},
    }


def _anchor_prices():
    return [
        ("provz", "a-lo", 0.000001, 0.000002),
        ("provz", "a-md", 0.00001,  0.00002),
        ("provz", "a-hi", 0.0001,   0.0002),
        ("provz", "a-pr", 0.001,    0.002),
    ]


def test_merge_by_identity_sums_and_single_row():
    # Same model identity split into two by_model sub-series (the normal series
    # + the :unexpected_billing variant). They MUST collapse to one row.
    s = _summary([
        _bm("provx/alpha-8", cost=10.0, calls=100, inp=400_000, out=100_000,
            bot_count=2),
        _bm("provx/alpha-8:unexpected_billing", cost=5.0, calls=20,
            inp=80_000, out=20_000, bot_count=1, last_ts="2026-06-12T00:00:00Z"),
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    rows = [r for r in out["rows"] if r["model_id"] == "alpha-8"]
    assert len(rows) == 1                       # collapsed to ONE identity row
    r = rows[0]
    assert r["total_cost"] == 15.0              # Σcost
    assert r["turns"] == 120                    # Σcalls
    assert r["cost_per_turn"] == 0.125          # 15 / 120 (Σcost ÷ Σcalls)
    assert r["billed_tokens"] == 600_000        # Σ(input+output)
    # blended = Σcost ÷ (Σbilled / 1000) = 15 / 600 — recomputed, NOT averaged.
    assert r["usd_per_1k_blended"] == 0.025
    assert r["unexpected_billing"] is True      # the UB sub-series folds in
    assert r["model"] == "provx/alpha-8"        # suffix dropped on the merged id
    # turns not supplied → bot_count falls back to max per-series (2).
    assert r["bot_count"] == 2
    assert r["last_used_ts"] == "2026-06-12T00:00:00Z"   # max across series


def test_merge_band_is_pricing_derived_single_not_per_series():
    # Two sub-series with very different observed costs — in v1 each derived its
    # own observed band. The merged identity must carry ONE band, from PRICING.
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "mx", 0.00008, 0.0004),   # input price lands in "high"
    )
    s = _summary([
        _bm("provz/mx", cost=10.0, calls=100, inp=400_000, out=100_000),
        _bm("provz/mx:unexpected_billing", cost=80.0, calls=5,
            inp=10_000, out=2_000),         # wildly pricier series
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network={},
    )
    rows = [r for r in out["rows"] if r["model_id"] == "mx"]
    assert len(rows) == 1
    assert rows[0]["band"] == "high"
    assert rows[0]["band_source"] == "pricing"   # identity priced, not per-series


def test_versions_stay_distinct():
    s = _summary([
        _bm("provx/alpha-8", cost=10.0, calls=100, inp=200_000, out=50_000),
        _bm("provx/alpha-7", cost=5.0, calls=50, inp=100_000, out=25_000),
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    assert sorted(r["model_id"] for r in out["rows"]) == ["alpha-7", "alpha-8"]


def test_audience_split_summed_on_merged_row():
    s = _summary(
        [
            _bm("provx/alpha-8", cost=10.0, calls=100, inp=400_000, out=100_000),
            _bm("provx/alpha-8:unexpected_billing", cost=5.0, calls=20,
                inp=80_000, out=20_000),
        ],
        by_aud=[
            {"model": "provx/alpha-8", "total_calls": 100,
             "human": {"calls": 40, "cost": 4.0},
             "non_human": {"calls": 60, "cost": 6.0}},
            {"model": "provx/alpha-8:unexpected_billing", "total_calls": 20,
             "human": {"calls": 5, "cost": 1.0},
             "non_human": {"calls": 15, "cost": 4.0}},
        ],
    )
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    r = next(r for r in out["rows"] if r["model_id"] == "alpha-8")
    assert r["audience"]["human"]["calls"] == 45        # 40 + 5
    assert r["audience"]["non_human"]["calls"] == 75     # 60 + 15
    assert r["audience"]["human"]["cost"] == 5.0         # 4.0 + 1.0
    assert r["audience"]["non_human"]["cost"] == 10.0    # 6.0 + 4.0
    assert r["human_pct"] == 37.5                        # 45 / 120 * 100


# ── v1.5 — rollups (blended math) ────────────────────────────────────────────


def test_band_rollup_blended_is_ratio_of_sums():
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "hi-1", 0.00008,   0.0004),   # high
        ("provz", "hi-2", 0.00009,   0.0004),   # high
        ("provz", "lo-1", 0.0000015, 0.000003),  # low
    )
    s = _summary([
        _bm("provz/hi-1", cost=30.0, calls=100, inp=400_000, out=100_000),
        _bm("provz/hi-2", cost=10.0, calls=100, inp=200_000, out=50_000),
        _bm("provz/lo-1", cost=2.0,  calls=200, inp=300_000, out=80_000),
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network={},
    )
    by_band = {b["band"]: b for b in out["rollups"]["by_band"]}
    assert by_band["high"]["spend"] == 40.0           # 30 + 10
    assert by_band["high"]["turns"] == 200            # 100 + 100
    assert by_band["high"]["cost_per_turn"] == 0.2    # 40 / 200 — Σcost ÷ Σturns
    assert by_band["high"]["member_count"] == 2
    assert by_band["low"]["cost_per_turn"] == 0.01    # 2 / 200


def test_role_rollup_blended_and_empty_role():
    network = {"models": {
        "rungs": [{"id": "lo", "costClass": "low",
                   "models": ["provz/r1", "provz/r2"]}],
        "roles": {"fast": "lo"},
    }}
    s = _summary([
        _bm("provz/r1", cost=30.0, calls=100, inp=400_000, out=100_000),
        _bm("provz/r2", cost=10.0, calls=300, inp=200_000, out=50_000),
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network=network)
    by_role = {r["role"]: r for r in out["rollups"]["by_role"]}
    fast = by_role["fast"]
    assert fast["rung_id"] == "lo"
    assert fast["spend"] == 40.0            # 30 + 10
    assert fast["turns"] == 400             # 100 + 300
    assert fast["cost_per_turn"] == 0.1     # 40 / 400 — blended over the slot
    assert fast["member_count"] == 2
    # A role with no traffic reports a null $/turn and zero members (a signal).
    assert by_role["power"]["cost_per_turn"] is None
    assert by_role["power"]["member_count"] == 0


# ── v1.5 — (bot × model) matrix + cross-series bot_count ──────────────────────


def test_bot_model_matrix_and_bot_count_from_turns():
    turns = [
        {"ts": "2026-06-12T10:00:00Z", "model": "provz/m1", "instance": "bot_a",
         "cost": 6.0, "input_tokens": 1000, "output_tokens": 200, "source": "human"},
        {"ts": "2026-06-12T11:00:00Z", "model": "provz/m1", "instance": "bot_b",
         "cost": 4.0, "input_tokens": 800, "output_tokens": 100, "source": "human"},
        {"ts": "2026-06-12T12:00:00Z", "model": "provz/m1", "instance": "bot_a",
         "cost": 2.0, "input_tokens": 500, "output_tokens": 50, "source": "cron"},
    ]
    s = _summary([_bm("provz/m1", cost=12.0, calls=3, inp=2300, out=350)])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network={}, turns=turns,
    )
    r = next(r for r in out["rows"] if r["model_id"] == "m1")
    assert r["bot_count"] == 2                      # bot_a + bot_b distinct
    by_cell = {(c["bot_id"], c["model_id"]): c for c in out["bot_model_matrix"]}
    assert by_cell[("bot_a", "m1")]["calls"] == 2
    assert by_cell[("bot_a", "m1")]["cost"] == 8.0           # 6 + 2
    assert by_cell[("bot_a", "m1")]["cost_per_turn"] == 4.0  # 8 / 2
    assert by_cell[("bot_b", "m1")]["calls"] == 1


def test_model_less_unknown_turn_is_dropped_as_sentinel():
    # A model-less turn: compute_summary keys it the "unknown" sentinel. That is
    # NOT a real model and is now dropped pod-wide (sentinel skip-list) — it never
    # reaches the leaderboard or the matrix. (Previously this row survived and the
    # per-series bot_count floor kept its count honest; sentinel exclusion makes
    # the row moot.)
    turns = [{
        "ts": "2026-06-12T10:00:00Z", "instance": "bot_a", "cost": 1.0,
        "input_tokens": 100, "output_tokens": 20, "source": "human",  # no "model"
    }]
    s = _summary([{
        "model": "unknown", "calls": 1, "cost": 1.0,
        "input_tokens": 100, "output_tokens": 20, "billed_tokens": 120,
        "usd_per_1k_blended": None, "usd_per_1k_input": None,
        "usd_per_1k_output": None, "low_confidence": True,
        "bot_count": 1, "last_used_ts": "2026-06-12T10:00:00Z",
    }])
    out = me.assemble_model_economics(s, pricing_cache=None, network={}, turns=turns)
    assert all(r["model_id"] != "unknown" for r in out["rows"])
    assert all(c["model_id"] != "unknown" for c in out["bot_model_matrix"])


# ── v1.5 — facet filters ─────────────────────────────────────────────────────


def test_filter_economics_provider_and_band():
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "hi-1", 0.00008,   0.0004),    # high
        ("provw", "lo-1", 0.0000015, 0.000003),   # low
    )
    s = _summary([
        _bm("provz/hi-1", cost=30.0, calls=100, inp=400_000, out=100_000),
        _bm("provw/lo-1", cost=2.0,  calls=200, inp=300_000, out=80_000),
    ])
    payload = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network={},
    )
    by_provider = me.filter_economics(payload, provider="provz")
    assert {r["model_id"] for r in by_provider["rows"]} == {"hi-1"}
    by_band = me.filter_economics(payload, band="low")
    assert {r["model_id"] for r in by_band["rows"]} == {"lo-1"}
    # "all" / empty are no-ops.
    assert len(me.filter_economics(payload, provider="all")["rows"]) == 2


def test_filter_economics_audience_recasts_and_nulls_eff_cost():
    s = _summary(
        [_bm("provx/m1", cost=10.0, calls=100, inp=400_000, out=100_000)],
        by_aud=[{"model": "provx/m1", "total_calls": 100,
                 "human": {"calls": 40, "cost": 4.0},
                 "non_human": {"calls": 60, "cost": 6.0}}],
    )
    payload = me.assemble_model_economics(s, pricing_cache=None, network={})
    human = me.filter_economics(payload, audience="human")
    r = next(x for x in human["rows"] if x["model_id"] == "m1")
    assert r["turns"] == 40              # human leg calls
    assert r["total_cost"] == 4.0        # human leg cost
    assert r["cost_per_turn"] == 0.1     # 4 / 40
    assert r["share_of_spend"] == 1.0    # only row → 100% of the audience total
    # Eff. cost/1k is unavailable per-audience → nulled (the UI greys it).
    assert r["usd_per_1k_blended"] is None
    assert r["eff_vs_list_delta"] is None
    assert r["audience_view"] == "human"
    # "auto" maps to the non_human leg.
    auto = me.filter_economics(payload, audience="auto")
    r2 = next(x for x in auto["rows"] if x["model_id"] == "m1")
    assert r2["turns"] == 60
    assert r2["total_cost"] == 6.0


def test_normalize_audience():
    assert me.normalize_audience("human") == "human"
    assert me.normalize_audience("auto") == "non_human"
    assert me.normalize_audience("non_human") == "non_human"
    assert me.normalize_audience("all") is None
    assert me.normalize_audience(None) is None
    assert me.normalize_audience("nonsense") is None


# ── provider-normalized identity (qualified + bare twins merge) ───────────────
# The gateway logs the same model both provider-qualified ("provz/mx") and bare
# ("mx"). The bare form carries no provider, so without resolution it lands on a
# DIFFERENT identity than its qualified twin — duplicating the model across rows
# and dropping the bare twin to the observed-cost "premium" / "off-catalog"
# fallback. These pin that bare keys resolve to their owning provider (from the
# pricing / listings / configured / twin DATA maps) and the twins collapse.


def _listings_cache(*pairs):
    """pairs: (provider, bare_id) → a minimal model-listings cache document."""
    providers: dict[str, list[dict]] = {}
    for prov, mid in pairs:
        providers.setdefault(prov, []).append({"model_id": mid})
    return {"providers": providers}


def test_qualified_and_bare_twin_merge_to_one_priced_row():
    # DOD 1+2: a qualified "provz/mx" and a bare "mx" are the SAME model logged
    # two ways. They must collapse to ONE identity — turns summed, ONE band from
    # PRICING — not a qualified "high" row plus a bare observed-cost "premium"
    # row. (Pre-fix: two rows, the bare one off-catalog/premium.)
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "mx", 0.00008, 0.0004),   # input price lands in "high"
    )
    # provz/mx is also a configured rung model, so the merged row reads
    # configured (the catalog), not off-catalog.
    network = {"models": {
        "rungs": [{"id": "pw", "costClass": "high", "models": ["provz/mx"]}],
        "roles": {"power": "pw"},
    }}
    s = _summary([
        _bm("provz/mx", cost=2.0,  calls=2,  inp=40_000,  out=10_000),   # qualified
        _bm("mx",       cost=24.0, calls=24, inp=480_000, out=120_000),  # bare twin
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network=network,
    )
    rows = [r for r in out["rows"] if r["model_id"] == "mx"]
    assert len(rows) == 1                       # merged, NOT two rows
    r = rows[0]
    assert r["provider"] == "provz"             # bare twin resolved to provz
    assert r["model"] == "provz/mx"             # rendered qualified
    assert r["turns"] == 26                     # 2 + 24 summed
    assert r["total_cost"] == 26.0              # 2.0 + 24.0
    assert r["band"] == "high"                  # priced, NOT observed "premium"
    assert r["band_source"] == "pricing"
    assert r["configured"] is True              # reflects the rung catalog
    # And the model is NOT also listed as configured-but-unused (deduped).
    assert all(u["model_id"] != "mx" for u in out["unused"])


def test_bare_key_resolves_via_pricing_without_twin():
    # A bare key with NO qualified twin in by_model still resolves — the pricing
    # catalog knows "mx" belongs to provz. (Exercises the pricing source alone.)
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "mx", 0.00008, 0.0004),       # high
    )
    s = _summary([_bm("mx", cost=20.0, calls=100, inp=400_000, out=100_000)])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network={},
    )
    rows = [r for r in out["rows"] if r["model_id"] == "mx"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "provz"       # resolved from pricing
    assert rows[0]["band"] == "high"
    assert rows[0]["band_source"] == "pricing"


def test_bare_key_resolves_via_listings_cache():
    # A bare key resolves from the listings cache (the new listings_cache arg) —
    # provz lists "mx", so the bare key adopts provz.
    s = _summary([_bm("mx", cost=20.0, calls=100, inp=400_000, out=100_000)])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network={},
        listings_cache=_listings_cache(("provz", "mx")),
    )
    rows = [r for r in out["rows"] if r["model_id"] == "mx"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "provz"
    assert rows[0]["model"] == "provz/mx"


def test_unknown_bare_id_stays_unqualified_no_over_merge():
    # DOD 3: a genuinely-unknown bare id — absent from pricing, listings,
    # configured, and with no qualified twin — stays its OWN unqualified row
    # (provider ""), NOT force-merged onto some other provider's model.
    cache = _pricing_cache(("provz", "known", 0.00008, 0.0004))
    s = _summary([
        _bm("known",   cost=10.0, calls=100, inp=200_000, out=50_000),   # in pricing
        _bm("mystery", cost=5.0,  calls=50,  inp=100_000, out=25_000),   # in nothing
    ])
    out = me.assemble_model_economics(s, pricing_cache=cache, network={})
    mystery = [r for r in out["rows"] if r["model_id"] == "mystery"]
    assert len(mystery) == 1
    assert mystery[0]["provider"] == ""          # unresolved — stays unqualified
    assert mystery[0]["model"] == "mystery"      # no fabricated provider prefix
    # The resolvable one DID pick up its provider, so the two never collide.
    known = [r for r in out["rows"] if r["model_id"] == "known"]
    assert known[0]["provider"] == "provz"


def test_ambiguous_bare_id_not_force_merged():
    # A bare id that maps to TWO providers in the only source (pricing) is
    # ambiguous → dropped from that source → stays unqualified, never
    # arbitrarily merged onto one of the two.
    cache = _pricing_cache(
        ("provx", "dup", 0.00008, 0.0004),
        ("provy", "dup", 0.00009, 0.0004),
    )
    s = _summary([_bm("dup", cost=5.0, calls=50, inp=100_000, out=25_000)])
    out = me.assemble_model_economics(s, pricing_cache=cache, network={})
    rows = [r for r in out["rows"] if r["model_id"] == "dup"]
    assert len(rows) == 1
    assert rows[0]["provider"] == ""             # ambiguous → unresolved


def test_twin_source_beats_ambiguous_lower_source():
    # Pricing is ambiguous for "dup" (two providers), but by_model carries a
    # qualified twin "provx/dup" — the highest-authority source resolves what
    # the lower one could not, so the bare twin merges onto provx.
    cache = _pricing_cache(
        ("provx", "dup", 0.00008, 0.0004),
        ("provy", "dup", 0.00009, 0.0004),
    )
    s = _summary([
        _bm("provx/dup", cost=10.0, calls=100, inp=200_000, out=50_000),  # twin
        _bm("dup",       cost=5.0,  calls=50,  inp=100_000, out=25_000),  # bare
    ])
    out = me.assemble_model_economics(s, pricing_cache=cache, network={})
    rows = [r for r in out["rows"] if r["model_id"] == "dup"]
    assert len(rows) == 1                         # twin source resolved → merged
    assert rows[0]["provider"] == "provx"
    assert rows[0]["turns"] == 150                # 100 + 50 summed


def test_matrix_resolves_identically_to_rows():
    # DOD 4: the (bot × model) matrix must resolve bare turns the SAME way as the
    # rows, so a model logged qualified by one bot and bare by another folds onto
    # ONE identity — bot_count counts both, and the per-bot legs reconcile.
    turns = [
        {"ts": "2026-06-12T10:00:00Z", "model": "provz/mx", "instance": "bot_a",
         "cost": 6.0, "input_tokens": 1000, "output_tokens": 200, "source": "human"},
        {"ts": "2026-06-12T11:00:00Z", "model": "mx", "instance": "bot_b",
         "cost": 4.0, "input_tokens": 800, "output_tokens": 100, "source": "human"},
        {"ts": "2026-06-12T12:00:00Z", "model": "mx", "instance": "bot_a",
         "cost": 2.0, "input_tokens": 500, "output_tokens": 50, "source": "cron"},
    ]
    s = _summary([
        _bm("provz/mx", cost=6.0, calls=1, inp=1000, out=200, bot_count=1),
        _bm("mx",       cost=6.0, calls=2, inp=1300, out=150, bot_count=2),
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network={}, turns=turns,
    )
    rows = [r for r in out["rows"] if r["model_id"] == "mx"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "provz"
    assert rows[0]["bot_count"] == 2              # bot_a + bot_b, across both forms
    # Matrix cells key on the resolved (bot, model_id) — bot_a's qualified turn
    # and bare turn fold onto ONE ("bot_a","mx") cell, never two.
    cells = [c for c in out["bot_model_matrix"] if c["model_id"] == "mx"]
    by_cell = {(c["bot_id"], c["model_id"]): c for c in cells}
    assert by_cell[("bot_a", "mx")]["calls"] == 2     # qualified + bare folded
    assert by_cell[("bot_a", "mx")]["cost"] == 8.0    # 6 + 2
    assert by_cell[("bot_a", "mx")]["provider"] == "provz"
    assert by_cell[("bot_b", "mx")]["calls"] == 1
    # Exactly one cell per bot for this identity (no qualified/bare split).
    assert len(cells) == 2


def test_audience_merges_across_qualified_and_bare_twins():
    # The audience fold must use the same resolution, so a bare audience row and
    # its qualified twin land on one identity and human-% reads summed legs.
    s = _summary(
        [
            _bm("provz/mx", cost=2.0,  calls=20,  inp=40_000,  out=10_000),
            _bm("mx",       cost=10.0, calls=100, inp=200_000, out=50_000),
        ],
        by_aud=[
            {"model": "provz/mx", "total_calls": 20,
             "human": {"calls": 8, "cost": 1.0},
             "non_human": {"calls": 12, "cost": 1.0}},
            {"model": "mx", "total_calls": 100,
             "human": {"calls": 40, "cost": 5.0},
             "non_human": {"calls": 60, "cost": 5.0}},
        ],
    )
    out = me.assemble_model_economics(
        s, pricing_cache=_pricing_cache(("provz", "mx", 0.00008, 0.0004)),
        network={},
    )
    rows = [r for r in out["rows"] if r["model_id"] == "mx"]
    assert len(rows) == 1
    r = rows[0]
    assert r["audience"]["human"]["calls"] == 48      # 8 + 40
    assert r["audience"]["non_human"]["calls"] == 72   # 12 + 60
    assert r["human_pct"] == 40.0                      # 48 / 120 * 100


# ── resolve_bare_to_provider (public #2889 entrypoint, shared with v2 perf) ───


def test_resolve_bare_to_provider_maps_bare_to_qualified_twin():
    # The public entrypoint the v2 perf layer joins on: a bare key whose
    # qualified twin appears in by_model resolves to that twin's provider.
    s = _summary([
        _bm("provz/mx", cost=6.0, calls=10, inp=40_000, out=10_000),
        _bm("mx",       cost=2.0, calls=5,  inp=10_000, out=2_000),
    ])
    m = me.resolve_bare_to_provider(s, network={}, pricing_cache=None, listings_cache=None)
    assert m.get("mx") == "provz"


def test_resolve_bare_to_provider_matches_assembler_internal_resolution():
    # The same function the assembler calls internally → identical map, so a perf
    # row resolves to the SAME identity the cost row merged onto.
    s = _summary([
        _bm("provz/mx", cost=6.0, calls=10, inp=40_000, out=10_000),
        _bm("mx",       cost=2.0, calls=5,  inp=10_000, out=2_000),
    ])
    m = me.resolve_bare_to_provider(s, network={}, pricing_cache=None, listings_cache=None)
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    # by_model split a qualified + bare twin; the assembler collapsed them to one
    # row via the map → exactly one "mx" row under provider provz.
    rows = [r for r in out["rows"] if r["model_id"] == "mx"]
    assert len(rows) == 1 and rows[0]["provider"] == "provz"
    # And the standalone map resolves the bare key to that same provider.
    assert me._identity("mx", m) == ("provz", "mx")


def test_resolve_bare_to_provider_unresolvable_stays_empty():
    # A bare-only key with no qualified twin / catalog entry stays unresolved —
    # so cost AND perf both key it ("", bare) and still join.
    s = _summary([_bm("solo", cost=1.0, calls=3, inp=6_000, out=1_000)])
    m = me.resolve_bare_to_provider(s, network={}, pricing_cache=None, listings_cache=None)
    assert "solo" not in m
    assert me._identity("solo", m) == ("", "solo")


# ── sentinel-model exclusion (delivery-mirror / unknown dropped pod-wide) ─────
# These are internal routing artifacts, not real models — they must never reach
# the leaderboard rows, the provider/band facets, or the rollups.


def test_sentinel_models_excluded_from_rows_and_rollups():
    s = _summary([
        _by_model_row("provx/alpha-1", cost=30.0, calls=100, blended=0.02),
        _by_model_row("delivery-mirror", cost=5.0, calls=40, blended=0.01),
        _by_model_row("unknown", cost=2.0, calls=10, blended=0.01),
        _by_model_row("provider/delivery-mirror", cost=1.0, calls=3, blended=0.01),
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    ids = {r["model_id"] for r in out["rows"]}
    assert ids == {"alpha-1"}                 # only the real model survives
    # sentinels gone from the band rollup members too.
    rollup_members = sum(b["member_count"] for b in out["rollups"]["by_band"])
    assert rollup_members == 1


def test_sentinel_models_excluded_case_insensitive_and_from_matrix():
    turns = [
        {"ts": "2026-06-12T10:00:00Z", "model": "provz/m1", "instance": "bot_a",
         "cost": 6.0, "input_tokens": 1000, "output_tokens": 200, "source": "human"},
        {"ts": "2026-06-12T11:00:00Z", "model": "Delivery-Mirror", "instance": "bot_b",
         "cost": 4.0, "input_tokens": 800, "output_tokens": 100, "source": "human"},
        {"ts": "2026-06-12T12:00:00Z", "model": "UNKNOWN", "instance": "bot_c",
         "cost": 2.0, "input_tokens": 500, "output_tokens": 50, "source": "cron"},
    ]
    s = _summary([
        _bm("provz/m1", cost=6.0, calls=1, inp=1000, out=200),
        # case-variant sentinels must still be dropped.
        _bm("Delivery-Mirror", cost=4.0, calls=1, inp=800, out=100),
        _bm("UNKNOWN", cost=2.0, calls=1, inp=500, out=50),
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={}, turns=turns)
    assert {r["model_id"] for r in out["rows"]} == {"m1"}
    # the matrix carries no sentinel cells / bots either.
    matrix_ids = {c["model_id"] for c in out["bot_model_matrix"]}
    assert matrix_ids == {"m1"}


# ── rollup excludes low-confidence rows (fixes HIGH < MEDIUM anomaly) ─────────


def test_band_rollup_excludes_low_confidence_members():
    # A confident high-band model and a low-confidence high-band model. The
    # blended $/turn must reflect ONLY the confident member — the low-sample row
    # (a tiny, distorting $/turn) must NOT move the band's blend.
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "hi-big",   0.00008, 0.0004),   # high
        ("provz", "hi-tiny",  0.00009, 0.0004),   # high
    )
    s = _summary([
        # confident: 40 over 200 turns → blended $0.20/turn.
        _bm("provz/hi-big", cost=40.0, calls=200, inp=400_000, out=100_000),
        # low-confidence: sub-10k tokens, a wild $1.25/turn over 4 turns.
        _bm("provz/hi-tiny", cost=5.0, calls=4, inp=4_000, out=2_000),
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network={},
    )
    by_band = {b["band"]: b for b in out["rollups"]["by_band"]}
    hi = by_band["high"]
    # blend is over the confident member ONLY: 40 / 200, NOT (40+5)/(200+4).
    assert hi["cost_per_turn"] == 0.2
    assert hi["spend"] == 40.0
    assert hi["turns"] == 200
    assert hi["member_count"] == 1          # the low-conf row is excluded
    # but the low-confidence row is still a leaderboard row (never hidden here).
    assert any(r["model_id"] == "hi-tiny" for r in out["rows"])


def test_band_rollup_all_low_confidence_emits_null_blend():
    # A band whose only member is low-confidence still EMITS (visible as a
    # signal), but with cost_per_turn None and member_count 0.
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "hi-tiny", 0.00009, 0.0004),   # high
    )
    s = _summary([
        _bm("provz/hi-tiny", cost=5.0, calls=4, inp=4_000, out=2_000),  # low-conf
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network={},
    )
    by_band = {b["band"]: b for b in out["rollups"]["by_band"]}
    assert "high" in by_band
    assert by_band["high"]["cost_per_turn"] is None
    assert by_band["high"]["member_count"] == 0
    assert by_band["high"]["turns"] == 0


# ── used-but-low-volume legibility (the "Opus is missing" / empty-High-card fix) ─
# A used-but-low-volume model (under the 10k-token gate) must stay legible: it
# lands in `rows` flagged low_confidence (never dropped), and the band/role
# rollup it belongs to reports used_count > 0 even when the confident blend is
# empty — so the card reads "N models · insufficient data", not "0 models".


def test_low_volume_model_lands_in_rows_as_low_confidence_not_dropped():
    # STEP-0 / operator-report regression ("Opus is missing"): a model with under
    # 10k billed tokens — the power rung gets little pod traffic — is GATED
    # low_confidence, but must still LAND IN `rows` (never dropped), so the
    # leaderboard's "+N below the confidence line" affordance can surface it.
    # Placeholder names (scrub-safe) standing in for the opus / power-rung case.
    s = _summary([
        _bm("provx/power-1", cost=0.9, calls=3, inp=6_000, out=1_500),   # < 10k → gated
        _bm("provx/fast-1",  cost=4.0, calls=2000, inp=4_000_000, out=900_000),  # confident
    ])
    out = me.assemble_model_economics(s, pricing_cache=None, network={})
    by_id = {r["model_id"]: r for r in out["rows"]}
    assert "power-1" in by_id, "low-volume model must NOT be dropped — only gated"
    assert by_id["power-1"]["low_confidence"] is True
    assert by_id["power-1"]["billed_tokens"] == 7_500   # 6k + 1.5k, under the 10k gate
    # The confident sibling is not flagged — proves the gate, not a blanket flag.
    assert by_id["fast-1"]["low_confidence"] is False


def test_rollup_used_count_visible_when_all_members_low_confidence():
    # The empty-"High"-card fix: a band/role whose members are ALL low-volume
    # blends to nothing (member_count 0, $/turn None — the blend stays
    # confident-only) BUT reports used_count > 0, so the card can render
    # "N models · insufficient data" instead of the misleading "0 models · 0
    # turns" that erased the used-but-low-volume models. used_count is a COUNT
    # only — it never folds the low-conf sample into the blend.
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "hi-tiny", 0.00009, 0.0004),   # high band
    )
    # A role pointing at a rung whose only model is the low-volume one, so the
    # role rollup exercises used_count too.
    network = {"models": {
        "rungs": [{"id": "pw", "costClass": "high", "models": ["provz/hi-tiny"]}],
        "roles": {"power": "pw"},
    }}
    s = _summary([
        _bm("provz/hi-tiny", cost=5.0, calls=4, inp=4_000, out=2_000),  # < 10k → low-conf
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network=network,
    )
    by_band = {b["band"]: b for b in out["rollups"]["by_band"]}
    hi = by_band["high"]
    assert hi["member_count"] == 0          # blend is confident-only → no members
    assert hi["cost_per_turn"] is None      # no authoritative blended cost
    assert hi["turns"] == 0
    assert hi["used_count"] == 1            # …but the band DOES have a used model

    by_role = {r["role"]: r for r in out["rollups"]["by_role"]}
    power = by_role["power"]
    assert power["member_count"] == 0
    assert power["cost_per_turn"] is None
    assert power["used_count"] == 1        # role backed only by the low-volume model


def test_rollup_used_count_counts_confident_and_low_conf_members():
    # used_count = ALL members (confident + low-conf); member_count = confident
    # only. A band with one confident + one low-conf member reports member_count
    # 1, used_count 2 — and the blend reflects only the confident member.
    cache = _pricing_cache(
        *_anchor_prices(),
        ("provz", "hi-big",  0.00008, 0.0004),   # high
        ("provz", "hi-tiny", 0.00009, 0.0004),   # high
    )
    s = _summary([
        _bm("provz/hi-big",  cost=40.0, calls=200, inp=400_000, out=100_000),  # confident
        _bm("provz/hi-tiny", cost=5.0,  calls=4,   inp=4_000,   out=2_000),    # low-conf
    ])
    out = me.assemble_model_economics(
        s, pricing_cache=cache, catalog=_anchor_catalog(), network={},
    )
    hi = {b["band"]: b for b in out["rollups"]["by_band"]}["high"]
    assert hi["member_count"] == 1     # confident-only blend
    assert hi["used_count"] == 2       # both used models counted
    assert hi["cost_per_turn"] == 0.2  # 40 / 200 — low-conf row excluded from blend


# ── uncredentialed-catalog honesty (spec addendum 2026-06-25) ─────────────────
# A configured model whose provider holds no api_key on the pod can't run — it
# is flagged `credentialed:False`/`status:no_credentials` and separated from the
# credentialed-but-idle ("no usage yet") rows. The credentialed set is data-
# derived (provider strings only appear as fixture data, never in assertions
# beyond the fixture's own provider names). Fail-open: a None set flags nothing.


def _uncred_network():
    # Two providers, two rungs: one credentialed (provk), one not (provu).
    return {"models": {
        "rungs": [
            {"id": "lo", "costClass": "low", "models": ["provk/used-lo", "provk/idle-lo"]},
            {"id": "hi", "costClass": "high", "models": ["provu/nokey-hi"]},
        ],
        "roles": {"fast": "lo", "power": "hi"},
    }}


def test_unused_uncredentialed_model_flagged_no_credentials():
    # provu holds no key on the pod → its configured-but-unused model is flagged
    # not-viable; the credentialed idle model stays "no usage yet".
    s = _summary([_by_model_row("provk/used-lo", cost=5.0, calls=100, blended=0.01)])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network=_uncred_network(),
        credentialed_providers={"provk"},
    )
    unused = {r["model_id"]: r for r in out["unused"]}
    # Idle-but-credentialed: can run, just no traffic yet.
    assert unused["idle-lo"]["credentialed"] is True
    assert unused["idle-lo"]["status"] == "no_usage"
    # Uncredentialed: can't run — no key for its provider.
    assert unused["nokey-hi"]["credentialed"] is False
    assert unused["nokey-hi"]["status"] == "no_credentials"


def test_headline_unused_uncredentialed_count_separates_unusable():
    s = _summary([_by_model_row("provk/used-lo", cost=5.0, calls=100, blended=0.01)])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network=_uncred_network(),
        credentialed_providers={"provk"},
    )
    # The headline count is exactly the credentialed:False partition of `unused`
    # (the same split the JS uses to subtract unusable from the "unused" stat).
    expected = sum(1 for r in out["unused"] if r["credentialed"] is False)
    assert out["unused_uncredentialed"] == expected
    # Our fixture's two unused models land on the right side of the split.
    unused = {r["model_id"]: r for r in out["unused"]}
    assert unused["idle-lo"]["credentialed"] is True       # credentialed, idle
    assert unused["nokey-hi"]["credentialed"] is False     # counted as unusable


def test_credentialed_set_none_flags_nothing_fail_open():
    # The cardinal invariant: an unknown credentialed set (e.g. transient read
    # miss) must behave exactly as before — nothing flagged, no status forced
    # off "no_usage", role slots never read uncredentialed.
    s = _summary([_by_model_row("provk/used-lo", cost=5.0, calls=100, blended=0.01)])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network=_uncred_network(),
        credentialed_providers=None,
    )
    for r in out["unused"]:
        assert r["credentialed"] is True
        assert r["status"] == "no_usage"
    assert out["unused_uncredentialed"] == 0
    assert out["credentialed_providers"] is None
    assert all(not r["uncredentialed"] for r in out["rollups"]["by_role"])


def test_bare_provider_unused_model_not_flagged():
    # A configured model with no provider prefix (bare id — almost always an
    # Anthropic twin) is treated as credentialed/unknown, never force-flagged,
    # even when a credentialed set is present and excludes everything else.
    network = {"models": {
        "rungs": [{"id": "lo", "costClass": "low", "models": ["bare-only"]}],
        "roles": {"fast": "lo"},
    }}
    s = _summary([_by_model_row("provk/used-lo", cost=5.0, calls=100, blended=0.01)])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network=network,
        credentialed_providers={"provk"},
    )
    bare = next(r for r in out["unused"] if r["model_id"] == "bare-only")
    assert bare["credentialed"] is True
    assert bare["status"] == "no_usage"


def test_role_slot_all_uncredentialed_reads_unfilled():
    # The "power" role points at the uncredentialed "hi" rung — its slot must
    # read uncredentialed (unfilled), while the credentialed "fast" slot does not.
    s = _summary([_by_model_row("provk/used-lo", cost=5.0, calls=100, blended=0.01)])
    out = me.assemble_model_economics(
        s, pricing_cache=None, network=_uncred_network(),
        credentialed_providers={"provk"},
    )
    by_role = {r["role"]: r for r in out["rollups"]["by_role"]}
    assert by_role["power"]["uncredentialed"] is True
    assert by_role["fast"]["uncredentialed"] is False
