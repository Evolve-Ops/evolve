"""tests/test_model_cost_bands.py — anchor-relative cost bands (Addendum 8 §B/§C).

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §"Addendum 8 §B + §C".

Covers the 11b-2 band layer:
  - anchors derived from DEFAULT_MODEL_CATALOG (no provider/model literals here
    drive logic — the catalog DATA does);
  - anchor-relative thresholds (geometric-midpoint boundaries between the
    haiku/sonnet/opus/fable anchor prices) classify a model's input price into
    low/medium/high/premium;
  - the boundaries TRACK the anchors: shift the anchor prices and the same input
    price re-bands;
  - family-fallback for a cache miss (frontier-lag window);
  - an unknown family → no band (the caller emits a Signal; no invented band);
  - resolve_band order: exact pricing row → family map → unknown, with cited
    evidence on the pricing path.

ALL data is fixtures — no network, no live config (the catalog is passed in
explicitly where the test needs a fixed anchor set).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_cost_bands as cb  # noqa: E402
import model_pricing as mp  # noqa: E402


# ── A fixed anchor catalog (the four canonical rungs, primaries only) ─────────
# Mirrors DEFAULT_MODEL_CATALOG's shape: each rung carries a costClass and a
# models[] cluster whose FIRST entry is the band's anchor. Passed explicitly to
# the band functions so the test pins the anchor set (independent of code drift).
ANCHOR_CATALOG: dict = {
    "rungs": [
        {"id": "haiku-class", "costClass": "low",
         "models": ["anthropic/claude-haiku-4-5"]},
        {"id": "sonnet-class", "costClass": "medium",
         "models": ["anthropic/claude-sonnet-4-6"]},
        {"id": "opus-class", "costClass": "high",
         "models": ["anthropic/claude-opus-4-8"]},
        {"id": "fable-class", "costClass": "premium",
         "models": ["anthropic/claude-fable-5"]},
    ],
}

# Anchor prices ($/token), realistically spread across orders of magnitude:
#   haiku  $0.80/M  → 0.8e-6
#   sonnet $3.00/M  → 3.0e-6
#   opus   $15.0/M  → 15.0e-6
#   fable  $30.0/M  → 30.0e-6
_HAIKU = 0.8e-6
_SONNET = 3.0e-6
_OPUS = 15.0e-6
_FABLE = 30.0e-6


def _pricing_cache(prices: dict[tuple[str, str], float]) -> dict:
    """Build a minimal pricing-cache doc from {(provider, model_id): in_cost}.

    Shaped like ``model_pricing.build_pricing_cache`` output (``models`` list of
    normalized records) so the real ``lookup_price`` reader resolves it.
    """
    models = [
        {
            "provider": provider,
            "model_id": model_id,
            "input_cost_per_token": in_cost,
            "output_cost_per_token": in_cost * 5,
            "context_window": 200000,
            "family": None,
            "source": "litellm",
        }
        for (provider, model_id), in_cost in prices.items()
    ]
    return {"refreshed_at": "2026-06-11T00:00:00Z", "models": models, "degraded": []}


_ANCHOR_CACHE = _pricing_cache({
    ("anthropic", "claude-haiku-4-5"): _HAIKU,
    ("anthropic", "claude-sonnet-4-6"): _SONNET,
    ("anthropic", "claude-opus-4-8"): _OPUS,
    ("anthropic", "claude-fable-5"): _FABLE,
})


# ── anchor derivation ─────────────────────────────────────────────────────────

def test_anchors_derive_from_catalog_primaries():
    anchors = cb.anchor_models(ANCHOR_CATALOG)
    assert anchors["low"] == ("anthropic", "claude-haiku-4-5")
    assert anchors["medium"] == ("anthropic", "claude-sonnet-4-6")
    assert anchors["high"] == ("anthropic", "claude-opus-4-8")
    assert anchors["premium"] == ("anthropic", "claude-fable-5")


def test_anchors_first_rung_wins_on_duplicate_costclass():
    cat = {
        "rungs": [
            {"id": "a", "costClass": "low", "models": ["anthropic/m-a"]},
            {"id": "b", "costClass": "low", "models": ["openai/m-b"]},
        ],
    }
    assert cb.anchor_models(cat)["low"] == ("anthropic", "m-a")


# ── band computation: anchor-relative thresholds → correct bands ──────────────

def test_anchor_prices_classify_to_their_own_band():
    """Each anchor's own price lands in its own band — the boundaries straddle
    the anchors, never split one onto the wrong side."""
    assert cb.compute_band(_HAIKU, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "low"
    assert cb.compute_band(_SONNET, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "medium"
    assert cb.compute_band(_OPUS, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "high"
    assert cb.compute_band(_FABLE, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "premium"


def test_between_anchor_prices_band_by_geometric_midpoint():
    import math
    # Just below the haiku↔sonnet geometric midpoint → low; just above → medium.
    mid_lo = math.sqrt(_HAIKU * _SONNET)
    assert cb.compute_band(mid_lo * 0.99, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "low"
    assert cb.compute_band(mid_lo * 1.01, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "medium"
    # A price between sonnet and opus → medium (below the sonnet↔opus midpoint).
    mid_hi = math.sqrt(_SONNET * _OPUS)
    assert cb.compute_band(mid_hi * 0.99, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "medium"
    assert cb.compute_band(mid_hi * 1.01, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "high"


def test_price_above_top_anchor_is_premium():
    # Anything above the fable anchor (the top open band) is premium.
    assert cb.compute_band(_FABLE * 3, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "premium"


def test_price_below_bottom_anchor_is_low():
    # Cheaper than the cheapest anchor → still the cheapest band.
    assert cb.compute_band(_HAIKU / 10, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "low"


def test_bands_track_anchors_when_prices_shift():
    """The thresholds are RELATIVE: re-price the anchors upward and a price that
    used to be 'high' re-bands. This is the whole point of anchor-relative — no
    hardcoded magic dollar number."""
    # In the base cache, _OPUS is 'high'. Now build a cache where every anchor
    # is 10× cheaper: the same absolute price _OPUS is now well ABOVE the (new,
    # lower) fable anchor → premium.
    cheap_cache = _pricing_cache({
        ("anthropic", "claude-haiku-4-5"): _HAIKU / 10,
        ("anthropic", "claude-sonnet-4-6"): _SONNET / 10,
        ("anthropic", "claude-opus-4-8"): _OPUS / 10,
        ("anthropic", "claude-fable-5"): _FABLE / 10,
    })
    assert cb.compute_band(_OPUS, cache=cheap_cache, catalog=ANCHOR_CATALOG) == "premium"
    # And it was 'high' against the original anchors — proving it re-banded.
    assert cb.compute_band(_OPUS, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) == "high"


def test_no_anchor_prices_yields_no_band():
    """With an empty cache no anchor scale can be built → None (the caller falls
    through to the family map / Signal), never a fabricated band."""
    assert cb.compute_band(_OPUS, cache=None, catalog=ANCHOR_CATALOG) is None
    empty = {"models": []}
    assert cb.compute_band(_OPUS, cache=empty, catalog=ANCHOR_CATALOG) is None


def test_missing_or_nonpositive_price_yields_no_band():
    assert cb.compute_band(None, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) is None
    assert cb.compute_band(0.0, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) is None
    assert cb.compute_band(-1.0, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG) is None


def test_scale_degrades_to_priced_anchors_only():
    """If only some anchors are priced, the scale is built from those — never a
    fabricated boundary for the unpriced ones."""
    partial = _pricing_cache({
        ("anthropic", "claude-haiku-4-5"): _HAIKU,
        ("anthropic", "claude-opus-4-8"): _OPUS,
    })
    # Two priced anchors (low, high) → boundary at their geometric midpoint.
    import math
    mid = math.sqrt(_HAIKU * _OPUS)
    assert cb.compute_band(mid * 0.99, cache=partial, catalog=ANCHOR_CATALOG) == "low"
    assert cb.compute_band(mid * 1.01, cache=partial, catalog=ANCHOR_CATALOG) == "high"


# ── family-fallback for a cache miss (frontier-lag window) ────────────────────

def test_family_band_resolves_known_families():
    assert cb.family_band("anthropic/claude-haiku-4-5") == "low"
    assert cb.family_band("anthropic/claude-sonnet-4-6") == "medium"
    assert cb.family_band("anthropic/claude-opus-4-8") == "high"
    assert cb.family_band("anthropic/claude-fable-5") == "premium"
    # A brand-new fable VERSION the cache hasn't priced yet still maps by family.
    assert cb.family_band("anthropic/claude-fable-6") == "premium"
    # Mythos (parallel-frontier SKU) → premium.
    assert cb.family_band("anthropic/claude-mythos-5") == "premium"
    # DeepSeek lines (chat = cheap workhorse; reasoner = mid).
    assert cb.family_band("deepseek/deepseek-chat") == "low"
    assert cb.family_band("deepseek/deepseek-reasoner") == "medium"
    # Mistral lines (small = cheap; large/codestral = mid). Versioned ids still
    # map by family stem (mistral-large-latest -> mistral-large).
    assert cb.family_band("mistral/mistral-small-latest") == "low"
    assert cb.family_band("mistral/mistral-large-latest") == "medium"
    assert cb.family_band("mistral/mistral-large-2411") == "medium"
    assert cb.family_band("mistral/codestral-latest") == "medium"


def test_family_band_unknown_family_is_none():
    # An alien family the map has never seen → None (no invented band).
    assert cb.family_band("acme/zonk-9000") is None
    assert cb.family_band("") is None


# ── resolve_band: exact pricing → family map → unknown ────────────────────────

def test_resolve_band_prices_known_model_with_cited_evidence():
    """A priced model resolves via the anchor-relative computed band AND carries
    cited pricing evidence ($/token + catalog source)."""
    res = cb.resolve_band(
        "anthropic", "claude-opus-4-8", cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    )
    assert res.band == "high"
    assert res.source == "pricing"
    assert res.evidence["input_cost_per_token"] == _OPUS
    assert res.evidence["pricing_source"] == "litellm"
    assert res.evidence["provider"] == "anthropic"


def test_resolve_band_falls_back_to_family_map_on_cache_miss():
    """A brand-new model the cache doesn't price yet, but whose family IS known,
    resolves via the family map (the frontier-lag cover) — NOT a Signal."""
    # claude-fable-6 isn't in the cache; family 'claude-fable' → premium.
    res = cb.resolve_band(
        "anthropic", "claude-fable-6", cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    )
    assert res.band == "premium"
    assert res.source == "family-map"
    assert res.evidence["family"] == "claude-fable"


def test_resolve_band_unknown_family_returns_no_band():
    """A model absent from every pricing source AND with an unknown family
    resolves to NO band — the caller emits a model_discovery Signal; this never
    fabricates a tier (the rot the effort removes)."""
    res = cb.resolve_band(
        "acme", "zonk-9000", cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    )
    assert res.band is None
    assert res.source == "unknown"
    assert res.evidence == {}


def test_resolve_band_real_build_pricing_cache_roundtrip():
    """resolve_band works over a cache built by the real 11b-1 ingest path
    (not just a hand-shaped fixture) — proves the reader contract holds."""
    litellm_raw = {
        "claude-haiku-4-5": {"litellm_provider": "anthropic",
                              "input_cost_per_token": _HAIKU,
                              "output_cost_per_token": _HAIKU * 5},
        "claude-sonnet-4-6": {"litellm_provider": "anthropic",
                              "input_cost_per_token": _SONNET,
                              "output_cost_per_token": _SONNET * 5},
        "claude-opus-4-8": {"litellm_provider": "anthropic",
                            "input_cost_per_token": _OPUS,
                            "output_cost_per_token": _OPUS * 5},
        "claude-fable-5": {"litellm_provider": "anthropic",
                           "input_cost_per_token": _FABLE,
                           "output_cost_per_token": _FABLE * 5},
    }
    doc = mp.build_pricing_cache(
        litellm_raw=litellm_raw, modelsdev_raw={},
        refreshed_at="2026-06-11T00:00:00Z",
    )
    res = cb.resolve_band(
        "anthropic", "claude-sonnet-4-6", cache=doc, catalog=ANCHOR_CATALOG,
    )
    assert res.band == "medium"
    assert res.source == "pricing"


# ── observed-cost fallback (the pod's own telemetry as a band source) ─────────
# A frontier model the external catalogs haven't priced yet, but that the pod is
# already paying for, is banded from its measured usd_per_1k_blended mapped onto
# the SAME anchor scale — gated on the min-sample (low_confidence) flag.

def _observed_row(model: str, blended_per_1k, *, low_confidence=False,
                  billed=1_000_000) -> dict:
    """One usage_analytics ``by_model`` row, trimmed to the fields the band
    fallback reads (the real row carries more — calls/cost/cache/etc.)."""
    return {
        "model": model,
        "usd_per_1k_blended": blended_per_1k,
        "billed_tokens": billed,
        "low_confidence": low_confidence,
    }


def test_observed_band_maps_blended_per_1k_onto_anchor_scale():
    """observed_band converts the per-1k blended figure to $/token (÷1000) and
    rides the same anchor scale: the sonnet anchor's rate → medium, well above
    the fable anchor → premium."""
    # _SONNET $/token expressed as a per-1k blended rate (×1000) → medium.
    assert cb.observed_band(
        _SONNET * 1000, low_confidence=False,
        cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    ) == "medium"
    # 3× the fable anchor → premium (the open top band).
    assert cb.observed_band(
        _FABLE * 3 * 1000, low_confidence=False,
        cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    ) == "premium"


def test_observed_band_gated_by_min_sample_flag():
    """The min-sample gate: a figure flagged low_confidence — or with a
    missing/None flag (provenance unknown) — is NEVER banded, no matter how
    clean the number looks."""
    assert cb.observed_band(
        _SONNET * 1000, low_confidence=True,
        cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    ) is None
    assert cb.observed_band(
        _SONNET * 1000, low_confidence=None,
        cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    ) is None


def test_observed_band_rejects_nonpositive_or_missing():
    assert cb.observed_band(
        None, low_confidence=False, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    ) is None
    assert cb.observed_band(
        0.0, low_confidence=False, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    ) is None
    assert cb.observed_band(
        -1.0, low_confidence=False, cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    ) is None


def test_observed_row_for_matches_and_strips_billing_suffix():
    """The lookup matches on (provider, bare-id), case-insensitive on provider,
    and ignores the ``:unexpected_billing`` series suffix."""
    rows = [
        _observed_row("frontier/nova-1:unexpected_billing", _OPUS * 1000),
        _observed_row("other/zonk-1", _HAIKU * 1000),
    ]
    hit = cb.observed_row_for(rows, "Frontier", "nova-1")
    assert hit is not None and hit["usd_per_1k_blended"] == _OPUS * 1000
    assert cb.observed_row_for(rows, "frontier", "absent-9") is None
    assert cb.observed_row_for(None, "frontier", "nova-1") is None


def test_resolve_band_uses_observed_cost_for_unpriced_model():
    """An un-priced frontier model (family unknown to the map) with TRUSTWORTHY
    observed cost is banded from the pod's own telemetry — source 'observed',
    with the measured figure cited."""
    observed = [_observed_row("frontier/nova-1", _OPUS * 1000)]  # ≈ opus → high
    res = cb.resolve_band(
        "frontier", "nova-1", cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
        observed=observed,
    )
    assert res.band == "high"
    assert res.source == "observed"
    assert res.evidence["usd_per_1k_blended"] == _OPUS * 1000
    assert res.evidence["billed_tokens"] == 1_000_000


def test_resolve_band_priced_model_unchanged_by_observed():
    """A model that IS externally priced keeps its pricing-derived band even when
    an observed figure would map it elsewhere — external pricing stays primary
    (no regression for priced models)."""
    # opus is priced (→ high). Hand it an observed figure that maps to 'low';
    # the result must STAY 'high' from pricing, source 'pricing'.
    observed = [_observed_row("anthropic/claude-opus-4-8", (_HAIKU / 5) * 1000)]
    res = cb.resolve_band(
        "anthropic", "claude-opus-4-8", cache=_ANCHOR_CACHE,
        catalog=ANCHOR_CATALOG, observed=observed,
    )
    assert res.band == "high"
    assert res.source == "pricing"


def test_resolve_band_low_confidence_observed_is_not_banded():
    """An un-priced model whose observed cost is LOW-CONFIDENCE (below the
    min-sample threshold) is NOT banded off the noisy figure — it falls through
    to unknown (its family is unknown too)."""
    observed = [_observed_row(
        "frontier/nova-1", _OPUS * 1000, low_confidence=True, billed=500,
    )]
    res = cb.resolve_band(
        "frontier", "nova-1", cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
        observed=observed,
    )
    assert res.band is None
    assert res.source == "unknown"
    assert res.evidence == {}


def test_resolve_band_observed_preferred_over_family_map():
    """When an un-priced model has BOTH a curated family band AND trustworthy
    observed cost, the measured observed cost wins — it's real pod spend, the
    family map is only a name-match guess for the same gap."""
    # claude-fable-6: family 'claude-fable' → premium via the map. Observed cost
    # lands it at 'high' (cheaper than the fable anchor). Observed wins.
    observed = [_observed_row("anthropic/claude-fable-6", _OPUS * 1000)]
    res = cb.resolve_band(
        "anthropic", "claude-fable-6", cache=_ANCHOR_CACHE,
        catalog=ANCHOR_CATALOG, observed=observed,
    )
    assert res.band == "high"
    assert res.source == "observed"


def test_resolve_band_family_map_still_covers_when_no_observed():
    """Default (no observed data passed): the family map still covers the
    frontier-lag window — proves the new param defaults cleanly, no behavior
    change for the existing caller."""
    res = cb.resolve_band(
        "anthropic", "claude-fable-6", cache=_ANCHOR_CACHE, catalog=ANCHOR_CATALOG,
    )
    assert res.band == "premium"
    assert res.source == "family-map"
