"""tests/test_model_discovery_cost_bands.py — cited-pricing discovery (§B/§C).

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §"Addendum 8 §B + §C".

Phase 11b-2 part 3: when discovery surfaces a model in no rung, it attaches a
PRICING-DERIVED cost band (anchor-relative when the model is priced; family-map
when it's newer than the pricing catalogs) so the model_discovery Proposal
carries cited pricing evidence — never a hand-typed tier. Part 4: identity no
longer comes from RECOMMENDED (covered in test_bot_config_integrity_generator).

A model absent from EVERY pricing source AND with an unknown family falls back
to the family-naming heuristic and is FLAGGED un-priced (cost_band_source =
"heuristic") — it does NOT fabricate a price presented as cited.

All HTTP/pricing is injected — CI makes zero live calls.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_discovery as md  # noqa: E402
import model_pricing as mp  # noqa: E402
from signals import store as signals_store  # noqa: E402
from generators.model_discovery.observe import ModelDiscoveryContext  # noqa: E402

mdgen = importlib.import_module("generators.model_discovery.observe")  # noqa: E402


def _lm(provider, model_id, **kw):
    return md.ListedModel(
        provider=provider, model_id=model_id,
        qualified_id=f"{provider}/{model_id}", **kw,
    )


def _enumerator(listings):
    def _enum(provider, key):  # noqa: ARG001
        return md.ProviderEnumeration(
            provider=provider, ok=True, models=listings.get(provider, []),
        )
    return _enum


# Pod knows the three lower rungs but NOT a frontier line.
_NETWORK = {
    "bots": {},
    "models": {
        "rungs": [
            {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
        ],
    },
}

# Anchor prices ($/token) for the four default-catalog anchors. The pricing
# fetcher returns a LiteLLM-shaped fixture so the REAL ingest path builds the
# cache (no hand-shaped cache).
_PRICES = {
    "claude-haiku-4-5": 0.8e-6,
    "claude-sonnet-4-6": 3.0e-6,
    "claude-opus-4-8": 15.0e-6,
    "claude-fable-5": 30.0e-6,
    # A genuinely-new frontier line, priced ABOVE fable → premium band.
    "claude-mythos-5": 60.0e-6,
}


def _litellm_fixture(prices):
    return {
        mid: {
            "litellm_provider": "anthropic",
            "input_cost_per_token": p,
            "output_cost_per_token": p * 5,
            "max_input_tokens": 200000,
        }
        for mid, p in prices.items()
    }


def _pricing_fetcher(prices):
    """Injectable fetcher: LiteLLM URL → fixture; models.dev → empty."""
    fixture = _litellm_fixture(prices)

    def _get(url):
        if url == mp.LITELLM_PRICING_URL:
            return fixture
        return {}

    return _get


# ── Part 3: discovery finding carries a pricing-derived band + cited evidence ──

def test_discovery_band_is_priced_and_cited():
    """A discovered model that IS in the pricing cache gets an anchor-relative
    band with cited $/token evidence — not a naming heuristic."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        listings = {"anthropic": [
            _lm("anthropic", "claude-haiku-4-5"),
            _lm("anthropic", "claude-sonnet-4-6"),
            _lm("anthropic", "claude-opus-4-8"),
            _lm("anthropic", "claude-mythos-5", display_name="Claude Mythos 5"),
        ]}
        result = md.run_discovery(
            network=_NETWORK, bot_users=[], bot_configs={}, shared_dir=tmp,
            keys={"anthropic": "sk-test"},
            enumerator=_enumerator(listings),
            pricing_fetcher=_pricing_fetcher(_PRICES),
        )
        assert len(result.discoveries) == 1
        d = result.discoveries[0]
        assert d.qualified_id == "anthropic/claude-mythos-5"
        # mythos is priced above the fable anchor → premium, computed from price.
        assert d.suggested_cost_class == "premium"
        assert d.cost_band_source == "pricing"
        assert d.cost_band_evidence["input_cost_per_token"] == 60.0e-6
        assert d.cost_band_evidence["pricing_source"] == "litellm"


def test_discovery_band_falls_back_to_family_map_when_unpriced():
    """A discovered model the pricing catalog hasn't listed yet (frontier-lag)
    but whose family IS curated resolves its band via the family map — cited as
    family-map, NOT a fabricated price.

    Uses the 'mythos' family: it is curated in FAMILY_COST_BAND (premium) AND
    is a genuinely-new line (no member in DEFAULT_MODEL_CATALOG), so it surfaces
    as a discovery rather than a within-family staleness finding."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        listings = {"anthropic": [
            _lm("anthropic", "claude-haiku-4-5"),
            _lm("anthropic", "claude-sonnet-4-6"),
            _lm("anthropic", "claude-opus-4-8"),
            # Mythos: family 'claude-mythos' is curated → premium via the family
            # map, but it is NOT in the pricing fixture (frontier-lag).
            _lm("anthropic", "claude-mythos-5", display_name="Claude Mythos 5"),
        ]}
        # Pricing has only the anchors — NOT claude-mythos-5.
        anchors_only = {k: v for k, v in _PRICES.items() if k != "claude-mythos-5"}
        result = md.run_discovery(
            network=_NETWORK, bot_users=[], bot_configs={}, shared_dir=tmp,
            keys={"anthropic": "sk-test"},
            enumerator=_enumerator(listings),
            pricing_fetcher=_pricing_fetcher(anchors_only),
        )
        d = next(x for x in result.discoveries
                 if x.qualified_id == "anthropic/claude-mythos-5")
        assert d.suggested_cost_class == "premium"
        assert d.cost_band_source == "family-map"
        assert d.cost_band_evidence["family"] == "claude-mythos"


def test_discovery_unknown_family_is_heuristic_flagged_not_fabricated():
    """A model with NO price anywhere AND an unknown family falls back to the
    naming heuristic and is FLAGGED 'heuristic' (un-priced) — the band carries
    NO pricing evidence, so the proposal can't present it as cited."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        listings = {"anthropic": [
            _lm("anthropic", "claude-haiku-4-5"),
            _lm("anthropic", "claude-sonnet-4-6"),
            _lm("anthropic", "claude-opus-4-8"),
            # An alien line: not priced, family unknown to FAMILY_COST_BAND.
            _lm("anthropic", "zenith-1", display_name="Zenith 1"),
        ]}
        anchors_only = {k: v for k, v in _PRICES.items() if k != "claude-mythos-5"}
        result = md.run_discovery(
            network=_NETWORK, bot_users=[], bot_configs={}, shared_dir=tmp,
            keys={"anthropic": "sk-test"},
            enumerator=_enumerator(listings),
            pricing_fetcher=_pricing_fetcher(anchors_only),
        )
        d = next(x for x in result.discoveries
                 if x.qualified_id == "anthropic/zenith-1")
        # Still surfaces (it IS a discovery), but the band is heuristic-only.
        assert d.cost_band_source == "heuristic"
        # No $/token in the evidence — nothing was priced.
        assert "input_cost_per_token" not in d.cost_band_evidence


# ── Suggested rung is derived from the cost BAND, not the provider ─────────────
# These pin the spec §Addendum 3.B refactor: the suggested rung/tier comes from
# the model's computed COST BAND mapped through the rung costClass DATA, with NO
# per-provider ``model.provider == "..."`` branches. A band determines the rung
# regardless of which provider the model came from.

def test_band_to_rung_map_inverts_default_catalog():
    """The band → (rung_slug, role) map is derived from DEFAULT_MODEL_CATALOG's
    rung costClass + roles DATA — low→haiku-class/fast … premium→fable-class/max."""
    m = md._band_to_rung_map()
    assert m["low"] == ("haiku-class", "fast")
    assert m["medium"] == ("sonnet-class", "standard")
    assert m["high"] == ("opus-class", "power")
    assert m["premium"] == ("fable-class", "max")


def test_suggest_rung_structured_is_band_derived_not_provider():
    """suggest_rung_structured maps a band → rung slug from the catalog DATA,
    identically for EVERY provider. A low band → haiku-class, a high band →
    opus-class — no matter the provider token."""
    # ANY provider, low band → haiku-class/fast (acceptance: low from ANY
    # provider → fast/haiku-class).
    for prov in ("anthropic", "openai", "google", "xai", "deepseek",
                 "mistral", "some-new-provider"):
        model = _lm(prov, "whatever-model")
        slug, cc = md.suggest_rung_structured(model, "low")
        assert (slug, cc) == ("haiku-class", "low"), prov
        # high band → opus-class/power, same for every provider.
        slug, cc = md.suggest_rung_structured(model, "high")
        assert (slug, cc) == ("opus-class", "high"), prov
        # premium band → fable-class/max.
        slug, cc = md.suggest_rung_structured(model, "premium")
        assert (slug, cc) == ("fable-class", "premium"), prov


def test_suggest_rung_placement_prose_is_band_derived():
    """The prose suggestion names the band-matched rung, with no provider
    conditional — a high band reads 'opus-class (power)' for any provider."""
    rung, rationale = md.suggest_rung_placement(_lm("xai", "grok-frontier"), set(), "high")
    assert "opus-class" in rung
    assert "cost band 'high'" in rationale
    # Same band → same rung for a different provider (provider-independence).
    rung2, _ = md.suggest_rung_placement(_lm("openai", "gpt-frontier"), set(), "high")
    assert rung2 == rung


def test_unknown_band_size_naming_fallback_is_provider_neutral():
    """When the band is unknown (None), the fallback uses GENERIC size tokens
    (mini/small → low, large/ultra → medium), not provider names. A '*-mini'
    from any provider falls to haiku-class; an unrecognized name stays UNPLACED
    with an EMPTY slug (Addendum 13: never the ``new-rung`` placeholder)."""
    # mini → low → haiku-class, regardless of provider.
    assert md.suggest_rung_structured(_lm("xai", "frontier-mini"), None) == ("haiku-class", "low")
    assert md.suggest_rung_structured(_lm("openai", "alpha-mini"), None) == ("haiku-class", "low")
    # large → medium → sonnet-class.
    assert md.suggest_rung_structured(_lm("mistral", "novus-large"), None) == ("sonnet-class", "medium")
    # No size token, no band → unplaced: EMPTY slug (no rung), never "new-rung".
    assert md.suggest_rung_structured(_lm("anthropic", "zenith-1"), None) == ("", "medium")


# ── Part 3: the generated Proposal carries the cited pricing ───────────────────

def test_generator_proposal_cites_pricing(tmp_path):
    """End-to-end: the model_discovery Proposal body cites the per-token price
    and the pricing source for a priced discovery (cite-or-don't-recommend)."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"),
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", "claude-mythos-5", display_name="Claude Mythos 5"),
    ]}
    ctx = ModelDiscoveryContext(
        bot_id=None, shared_dir=tmp_path, network=_NETWORK,
        bot_users=[], bot_configs={}, keys={"anthropic": "sk-test"},
        enumerator=_enumerator(listings), consult_dismissals=False,
    )
    # Seed the pricing cache the generator's discovery run reads back. (The
    # generator context doesn't thread a pricing_fetcher, so pre-write the
    # cache the same way the discovery sweep would have.)
    doc = mp.build_pricing_cache(
        litellm_raw=_litellm_fixture(_PRICES), modelsdev_raw={},
        refreshed_at="2026-06-11T00:00:00Z",
    )
    mp.write_pricing_cache(tmp_path, doc)

    specs = mdgen.observe_signals(ctx)
    disc = [s for s in specs if s["type"] == "model_discovery"]
    assert len(disc) == 1
    # The Signal details carry the cited band source + evidence.
    assert disc[0]["details"]["cost_band_source"] == "pricing"
    assert disc[0]["details"]["suggested_cost_class"] == "premium"
    for spec in specs:
        signals_store.observe(tmp_path, **spec)

    # observe() is signal-only (spec §Addendum 12) — no proposal queued. The
    # cited-pricing body is still produced by the RETAINED AdoptModel builder,
    # which the AI Optimization adopt path drives; assert it from there.
    assert mdgen.observe(ctx) == []
    sig = next(
        s for s in signals_store.iter_active(
            tmp_path, producer="model_discovery", state="firing")
        if s.type == "model_discovery"
    )
    p = mdgen._make_discovery_proposal(ctx, sig)
    assert p is not None
    # The proposal body cites the price (in $/MTok) and the catalog source.
    assert "/MTok" in p.problem
    assert "litellm" in p.problem.lower()
    assert "premium" in p.problem
    # And the provenance records the cited band source.
    assert p.provenance.signals["cost_band_source"] == "pricing"


def test_generator_proposal_flags_unpriced_band(tmp_path):
    """A discovery with an un-priced, unknown-family model produces a proposal
    that FLAGS the band un-priced rather than fabricating a cited price."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"),
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", "zenith-1", display_name="Zenith 1"),
    ]}
    ctx = ModelDiscoveryContext(
        bot_id=None, shared_dir=tmp_path, network=_NETWORK,
        bot_users=[], bot_configs={}, keys={"anthropic": "sk-test"},
        enumerator=_enumerator(listings), consult_dismissals=False,
    )
    anchors_only = {k: v for k, v in _PRICES.items() if k != "claude-mythos-5"}
    doc = mp.build_pricing_cache(
        litellm_raw=_litellm_fixture(anchors_only), modelsdev_raw={},
        refreshed_at="2026-06-11T00:00:00Z",
    )
    mp.write_pricing_cache(tmp_path, doc)

    specs = mdgen.observe_signals(ctx)
    for spec in specs:
        signals_store.observe(tmp_path, **spec)
    # Signal-only (spec §Addendum 12); the unpriced-flag body comes from the
    # retained AdoptModel builder, not a queued proposal.
    assert mdgen.observe(ctx) == []
    sigs = [
        s for s in signals_store.iter_active(
            tmp_path, producer="model_discovery", state="firing")
        if s.type == "model_discovery"
    ]
    props = [mdgen._make_discovery_proposal(ctx, s) for s in sigs]
    p = next(x for x in props if x and "zenith-1" in x.problem)
    assert "UNPRICED" in p.problem
    assert p.provenance.signals["cost_band_source"] == "heuristic"
