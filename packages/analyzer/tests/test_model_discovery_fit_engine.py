"""tests/test_model_discovery_fit_engine.py — capability-aware fit engine (Addendum 13).

Covers the FIVE placement verdicts (fits_existing / new_tier / mode_variant /
specialist / cannot_place), the deterministic layer (incl. the mode-variant
family-stem match and the workload-specialist hint), the optional
LLM-sharpening layer with EVERY fail-open path, the kill of the ``new-rung``
literal (unplaceable → ``cannot_place`` with an empty slug, plus the applier
guard that refuses to mint a placeholder rung), and the fit_llm wiring (no
hardcoded model id, fail-open build).

All LLM is stubbed — CI makes zero live calls.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_discovery as md  # noqa: E402
from arbiter.appliers import adopt_model  # noqa: E402
from arbiter.appliers.adopt_model import AdoptModelApplier  # noqa: E402
from schema.proposal import AdoptModel  # noqa: E402
from generators.model_discovery import fit_llm  # noqa: E402
from generators.model_discovery.observe import ModelDiscoveryContext  # noqa: E402

# Use import_module so ``mdgen`` is the observe SUBMODULE, not the ``observe``
# function the package __init__ re-exports (which shadows attribute access).
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


# Pod knows the three lower rungs but NOT a frontier line (mirrors the
# cost-bands fixture so the band→role mapping resolves from the catalog).
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

_PRICES = {
    "claude-haiku-4-5": 0.8e-6,
    "claude-sonnet-4-6": 3.0e-6,
    "claude-opus-4-8": 15.0e-6,
    "claude-fable-5": 30.0e-6,
    "claude-mythos-5": 60.0e-6,  # priced ABOVE fable → premium band
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
    import model_pricing as mp

    fixture = _litellm_fixture(prices)

    def _get(url):
        return fixture if url == mp.LITELLM_PRICING_URL else {}

    return _get


def _ctx(tmp_path, listings, *, fit_classifier=None, prices=None):
    return ModelDiscoveryContext(
        bot_id=None, shared_dir=tmp_path, network=_NETWORK,
        bot_users=[], bot_configs={}, keys={"anthropic": "sk-test"},
        enumerator=_enumerator(listings),
        pricing_fetcher=_pricing_fetcher(prices or {}),
        consult_dismissals=False, fit_classifier=fit_classifier,
    )


def _finding(provider, model_id, verdict: md.PlacementVerdict) -> md.DiscoveryFinding:
    return md.DiscoveryFinding(
        provider=provider, model_id=model_id,
        qualified_id=f"{provider}/{model_id}", evidence={},
        suggested_rung="", suggested_rationale="",
        suggested_rung_slug=(verdict.recommended_rung_slug or ""),
        placement_verdict=verdict.placement_verdict,
        recommended_role=verdict.recommended_role,
        recommended_rung_slug=verdict.recommended_rung_slug,
        fit_reason=verdict.fit_reason, fit_confidence=verdict.fit_confidence,
        fit_evidence=verdict.fit_evidence, fit_source=verdict.fit_source,
    )


# ── Deterministic verdict matrix ──────────────────────────────────────────────

def test_verdict_priced_is_authoritative_fits_existing_any_provider():
    """A priced model bands → fits_existing with the band's role/rung, at high
    confidence, for EVERY provider (band→role is catalog DATA, not provider)."""
    for prov in ("anthropic", "openai", "xai", "deepseek", "some-new-provider"):
        v = md.compute_placement_verdict(
            _lm(prov, "anything"), "low", "pricing",
            {"input_cost_per_token": 0.5e-6, "pricing_source": "litellm"},
        )
        assert v.placement_verdict == md.PLACEMENT_FITS_EXISTING, prov
        assert v.recommended_role == "fast", prov
        assert v.recommended_rung_slug == "haiku-class", prov
        assert v.fit_source == "deterministic"
        assert v.fit_confidence >= 0.85
        assert v.fit_evidence["cost_band"] == "low"
        assert v.fit_evidence["cost_band_source"] == "pricing"
    # high band → power/opus-class, premium → max/fable-class.
    v_hi = md.compute_placement_verdict(_lm("xai", "x"), "high", "pricing", {})
    assert (v_hi.recommended_role, v_hi.recommended_rung_slug) == ("power", "opus-class")
    v_pr = md.compute_placement_verdict(_lm("xai", "x"), "premium", "pricing", {})
    assert (v_pr.recommended_role, v_pr.recommended_rung_slug) == ("max", "fable-class")


def test_verdict_family_map_fits_existing_lower_confidence():
    v = md.compute_placement_verdict(
        _lm("xai", "grok-3"), "medium", "family-map", {"family": "grok"},
    )
    assert v.placement_verdict == md.PLACEMENT_FITS_EXISTING
    assert v.recommended_role == "standard"
    assert v.recommended_rung_slug == "sonnet-class"
    assert 0.7 <= v.fit_confidence < 0.85  # family-map < priced


def test_verdict_size_naming_hint_is_soft_fits_existing():
    """No band, but a generic size token → fits_existing at LOW confidence."""
    v = md.compute_placement_verdict(
        _lm("openai", "alpha-mini"), None, "heuristic", {},
    )
    assert v.placement_verdict == md.PLACEMENT_FITS_EXISTING
    assert v.recommended_role == "fast"
    assert v.recommended_rung_slug == "haiku-class"
    assert v.fit_confidence <= 0.5  # soft, name-based


def test_verdict_cannot_place_no_band_no_hint():
    """No price, unknown family, no size token → cannot_place, NO rung."""
    v = md.compute_placement_verdict(
        _lm("anthropic", "zenith-1"), None, "heuristic", {"family": "zenith"},
    )
    assert v.placement_verdict == md.PLACEMENT_CANNOT_PLACE
    assert v.recommended_role is None
    assert v.recommended_rung_slug is None
    assert v.fit_confidence <= 0.3


def test_verdict_new_tier_when_band_has_no_role_bearing_tier(monkeypatch):
    """A band the pod has no role-bearing tier for → new_tier with a band-derived
    proposed slug (never the ``new-rung`` placeholder)."""
    real = md._band_to_rung_map()
    trimmed = {k: val for k, val in real.items() if k != "premium"}
    monkeypatch.setattr(md, "_band_to_rung_map", lambda: trimmed)
    v = md.compute_placement_verdict(
        _lm("anthropic", "frontier-x"), "premium", "pricing",
        {"input_cost_per_token": 99e-6},
    )
    assert v.placement_verdict == md.PLACEMENT_NEW_TIER
    assert v.recommended_role is None
    assert v.recommended_rung_slug == "premium-class"
    assert not md.is_placeholder_rung_slug(v.recommended_rung_slug)


def test_verdict_reason_is_plain_language_no_jargon():
    """fit_reason avoids internal vocabulary (rung/band/dormant/new-rung)."""
    for v in (
        md.compute_placement_verdict(_lm("x", "m"), "low", "pricing", {}),
        md.compute_placement_verdict(_lm("x", "zenith-1"), None, "heuristic", {}),
        md.compute_placement_verdict(_lm("x", "m"), "premium", "family-map", {}),
    ):
        low = v.fit_reason.lower()
        for bad in ("rung", "band", "dormant", "new-rung", "cost class"):
            assert bad not in low, (bad, v.fit_reason)


def test_capability_name_tokens_are_soft_evidence():
    v = md.compute_placement_verdict(
        _lm("xai", "grok-3-reasoning"), "medium", "family-map", {},
    )
    assert "reasoning" in v.fit_evidence["capability_name_tokens"]


# ── Mode variant + specialist — the off-ladder kinds (deterministic) ──────────

def test_verdict_mode_variant_when_base_family_is_placed():
    """A reasoning MODE of an already-run model → mode_variant: no role, no
    tier. The variant's OWN family stem differs from the base (so it isn't
    pre-filtered as within-family), but the mode-stripped base IS placed."""
    v = md.compute_placement_verdict(
        _lm("xai", "grok-4-reasoning"), "medium", "family-map", {"family": "grok"},
        known_stems={"grok"},
    )
    assert v.placement_verdict == md.PLACEMENT_MODE_VARIANT
    assert v.recommended_role is None
    assert v.recommended_rung_slug is None
    assert v.fit_evidence["mode_base_family"] == "grok"
    assert "reasoning" in v.fit_evidence["mode_name_tokens"]


def test_verdict_mode_token_without_placed_base_is_not_mode_variant():
    """A mode token but NO matching placed base family → falls through to the
    band logic (a general reasoning model that fits a tier), NOT mode_variant."""
    v = md.compute_placement_verdict(
        _lm("xai", "grok-4-reasoning"), "medium", "family-map", {"family": "grok"},
        known_stems={"gpt-4o"},  # a different family is placed
    )
    assert v.placement_verdict == md.PLACEMENT_FITS_EXISTING
    assert v.recommended_role == "standard"


def test_verdict_mode_variant_no_known_stems_falls_through():
    """No known_stems passed (a bare unit call) → the mode-variant check is
    inert; a band-placeable reasoning model is still fits_existing."""
    v = md.compute_placement_verdict(
        _lm("xai", "grok-4-reasoning"), "medium", "family-map", {},
    )
    assert v.placement_verdict == md.PLACEMENT_FITS_EXISTING


def test_verdict_non_reasoning_joined_token_strips_to_base():
    v = md.compute_placement_verdict(
        _lm("xai", "grok-4-non-reasoning"), "medium", "family-map", {},
        known_stems={"grok"},
    )
    assert v.placement_verdict == md.PLACEMENT_MODE_VARIANT
    assert "non-reasoning" in v.fit_evidence["mode_name_tokens"]
    assert "reasoning" not in v.fit_evidence["mode_name_tokens"]  # subsumed


def test_verdict_non_reasoner_joined_token_strips_cleanly():
    """``non-reasoner`` is recognized as one token (no stray ``non`` left in the
    stripped base) and subsumes the bare ``reasoner``."""
    assert md._strip_mode_tokens("grok-4-non-reasoner") == "grok-4"
    v = md.compute_placement_verdict(
        _lm("xai", "grok-4-non-reasoner"), "medium", "family-map", {},
        known_stems={"grok"},
    )
    assert v.placement_verdict == md.PLACEMENT_MODE_VARIANT
    assert "non-reasoner" in v.fit_evidence["mode_name_tokens"]
    assert "reasoner" not in v.fit_evidence["mode_name_tokens"]  # subsumed


def test_verdict_specialist_from_workload_token_beats_price():
    """A workload token (coding) → specialist: off the general ladder, no role,
    no tier — even when a real price would otherwise band-place it."""
    v = md.compute_placement_verdict(
        _lm("openai", "gpt-5-codex-coder"), "high", "pricing",
        {"input_cost_per_token": 12e-6},
    )
    assert v.placement_verdict == md.PLACEMENT_SPECIALIST
    assert v.recommended_role is None
    assert v.recommended_rung_slug is None
    assert "coder" in v.fit_evidence["workload_name_tokens"]


def test_verdict_specialist_multi_agent_joined():
    v = md.compute_placement_verdict(
        _lm("xai", "grok-4-multi-agent"), None, "heuristic", {},
    )
    assert v.placement_verdict == md.PLACEMENT_SPECIALIST
    assert "multi-agent" in v.fit_evidence["workload_name_tokens"]


def test_verdict_specialist_reason_names_the_work_no_jargon():
    v = md.compute_placement_verdict(_lm("x", "deepseek-math"), None, "heuristic", {})
    assert v.placement_verdict == md.PLACEMENT_SPECIALIST
    low = v.fit_reason.lower()
    assert "math" in low  # names the kind of work in plain language
    for bad in ("rung", "band", "dormant", "new-rung", "cost class"):
        assert bad not in low


def test_verdict_mode_variant_reason_no_jargon():
    v = md.compute_placement_verdict(
        _lm("xai", "grok-4-thinking"), "medium", "family-map", {}, known_stems={"grok"},
    )
    assert v.placement_verdict == md.PLACEMENT_MODE_VARIANT
    low = v.fit_reason.lower()
    for bad in ("rung", "band", "dormant", "new-rung", "cost class"):
        assert bad not in low


def test_mode_variant_surfaces_through_observe_signals(tmp_path):
    """End-to-end: the pod runs claude-sonnet-4-6; its reasoning variant
    surfaces as a mode_variant Signal (its own stem differs, so it isn't
    pre-filtered, but the mode-stripped base family is placed)."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-6"),            # known
        _lm("anthropic", "claude-sonnet-4-6-reasoning"),  # its reasoning mode
    ]}
    specs = mdgen.observe_signals(_ctx(tmp_path, listings))
    rows = [s["details"] for s in specs if s["type"] == "model_discovery"]
    var = [d for d in rows if d["model_id"] == "claude-sonnet-4-6-reasoning"]
    assert var, [d["model_id"] for d in rows]
    d = var[0]
    assert d["placement_verdict"] == "mode_variant"
    assert d["recommended_role"] is None
    assert d["recommended_rung_slug"] is None
    assert d["suggested_rung_slug"] == ""           # NOT a placeholder slug
    assert d["fit_evidence"]["mode_base_family"] == "claude-sonnet"


def test_specialist_surfaces_through_observe_signals(tmp_path):
    listings = {"anthropic": [_lm("anthropic", "claude-builder-coder")]}
    specs = mdgen.observe_signals(_ctx(tmp_path, listings))
    d = next(s["details"] for s in specs if s["type"] == "model_discovery")
    assert d["placement_verdict"] == "specialist"
    assert d["recommended_role"] is None
    assert d["recommended_rung_slug"] is None
    assert d["suggested_rung_slug"] == ""
    assert d["fit_evidence"]["workload_name_tokens"] == ["coder"]
    # vocabulary clean in the operator-facing body too.
    for bad in ("rung", "band", "dormant", "new-rung"):
        assert bad not in next(
            s["body"] for s in specs if s["type"] == "model_discovery").lower()


# ── LLM sharpening (stubbed) ──────────────────────────────────────────────────

def test_llm_sharpens_cannot_place_to_fits_existing(tmp_path):
    listings = {"anthropic": [_lm("anthropic", "zenith-1")]}

    def stub(finding):
        return {
            "verdict": "fits_existing", "recommended_role": "standard",
            "confidence": 0.82,
            "reason": "It's a general workhorse model, a fit for your standard role.",
        }

    specs = mdgen.observe_signals(_ctx(tmp_path, listings, fit_classifier=stub))
    d = next(s["details"] for s in specs if s["type"] == "model_discovery")
    assert d["placement_verdict"] == "fits_existing"
    assert d["recommended_role"] == "standard"
    assert d["recommended_rung_slug"] == "sonnet-class"  # role→rung, computed
    assert d["suggested_rung_slug"] == "sonnet-class"
    assert d["fit_source"] == "llm"
    assert "standard role" in d["fit_reason"]


def test_llm_new_tier_uses_deterministic_band_slug():
    v = md.compute_placement_verdict(
        _lm("xai", "grok-x"), "medium", "family-map", {"family": "grok"},
    )
    finding = _finding("xai", "grok-x", v)

    def stub(_f):
        return {"verdict": "new_tier", "recommended_role": None,
                "confidence": 0.8, "reason": "It would add a brand-new tier."}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert out.placement_verdict == "new_tier"
    assert out.recommended_role is None
    assert out.recommended_rung_slug == "medium-class"  # band-derived, NOT LLM text
    assert out.fit_source == "llm"


def test_llm_cannot_place_clears_placement():
    v = md.compute_placement_verdict(_lm("x", "alpha-mini"), None, "heuristic", {})
    assert v.placement_verdict == "fits_existing"  # det size-hint
    finding = _finding("x", "alpha-mini", v)

    def stub(_f):
        return {"verdict": "cannot_place", "recommended_role": None,
                "confidence": 0.9, "reason": "Not enough information to place it."}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert out.placement_verdict == "cannot_place"
    assert out.recommended_role is None
    assert out.recommended_rung_slug is None
    assert out.suggested_rung_slug == ""
    assert out.fit_source == "llm"


def test_llm_may_not_override_priced_authoritative(tmp_path):
    """A real-price-derived fit is ground truth — a confident LLM cannot_place
    must NOT override it."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"),
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", "claude-fable-5"),
        _lm("anthropic", "claude-mythos-5"),
    ]}

    def stub(_f):
        return {"verdict": "cannot_place", "recommended_role": None,
                "confidence": 0.99, "reason": "I am unsure."}

    specs = mdgen.observe_signals(
        _ctx(tmp_path, listings, fit_classifier=stub, prices=_PRICES))
    myth = [s for s in specs if s["type"] == "model_discovery"
            and s["details"]["model_id"] == "claude-mythos-5"]
    assert myth, "mythos surfaced as a discovery"
    d = myth[0]["details"]
    assert d["placement_verdict"] == "fits_existing"
    assert d["recommended_role"] == "max"
    assert d["fit_source"] == "deterministic"  # LLM did not win


def test_llm_reason_with_internal_jargon_falls_back_to_clean():
    """If the LLM slips internal jargon (e.g. 'rung'), the emitted reason falls
    back to the always-clean deterministic reason (vocabulary rule)."""
    v = md.compute_placement_verdict(_lm("xai", "grok-3"), "medium", "family-map", {})
    finding = _finding("xai", "grok-3", v)
    det_reason = finding.fit_reason

    def stub(_f):
        return {"verdict": "fits_existing", "recommended_role": "standard",
                "confidence": 0.8,
                "reason": "It belongs in your sonnet-class rung as a dormant entry."}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert out.fit_source == "llm"  # the verdict was still adopted
    assert "rung" not in out.fit_reason.lower()
    assert "dormant" not in out.fit_reason.lower()
    assert out.fit_reason == det_reason  # fell back to the clean deterministic line


def test_llm_can_change_role_on_non_authoritative_fit():
    """A family-map (non-authoritative) fit CAN be re-roled by a confident LLM."""
    v = md.compute_placement_verdict(_lm("xai", "grok-3"), "medium", "family-map", {})
    finding = _finding("xai", "grok-3", v)

    def stub(_f):
        return {"verdict": "fits_existing", "recommended_role": "power",
                "confidence": 0.8, "reason": "Heavier reasoning model."}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert out.recommended_role == "power"
    assert out.recommended_rung_slug == "opus-class"
    assert out.fit_source == "llm"


def test_llm_sharpens_to_mode_variant():
    """A non-authoritative fit the LLM recognizes as a mode of a placed model →
    mode_variant: no role, no tier, empty slug."""
    v = md.compute_placement_verdict(_lm("xai", "grok-3"), "medium", "family-map", {})
    finding = _finding("xai", "grok-3", v)

    def stub(_f):
        return {"verdict": "mode_variant", "recommended_role": None,
                "confidence": 0.85, "reason": "The reasoning mode of a model you run."}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert out.placement_verdict == "mode_variant"
    assert out.recommended_role is None
    assert out.recommended_rung_slug is None
    assert out.suggested_rung_slug == ""
    assert out.fit_source == "llm"


def test_llm_sharpens_to_specialist():
    v = md.compute_placement_verdict(_lm("xai", "grok-3"), "medium", "family-map", {})
    finding = _finding("xai", "grok-3", v)

    def stub(_f):
        return {"verdict": "specialist", "recommended_role": None,
                "confidence": 0.8, "reason": "A coding specialist, off your usual roles."}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert out.placement_verdict == "specialist"
    assert out.recommended_role is None
    assert out.recommended_rung_slug is None
    assert out.suggested_rung_slug == ""
    assert out.fit_source == "llm"


def test_llm_specialist_may_not_override_priced_authoritative(tmp_path):
    """Even the off-ladder verdicts cannot override a real-price fits_existing —
    a confident LLM 'specialist' on a priced model is ignored (cost is truth)."""
    v = md.compute_placement_verdict(
        _lm("openai", "gp-9"), "low", "pricing",
        {"input_cost_per_token": 0.4e-6, "pricing_source": "litellm"},
    )
    assert v.placement_verdict == "fits_existing"  # priced, authoritative
    finding = _finding("openai", "gp-9", v)

    def stub(_f):
        return {"verdict": "specialist", "recommended_role": None,
                "confidence": 0.99, "reason": "looks specialized"}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert out.placement_verdict == "fits_existing"
    assert out.fit_source == "deterministic"  # LLM did not win


# ── Fail-open paths (every one keeps the deterministic verdict) ───────────────

def _cannot_place_finding():
    v = md.compute_placement_verdict(_lm("x", "zenith-1"), None, "heuristic", {})
    return _finding("x", "zenith-1", v)


def test_failopen_classifier_returns_non_dict():
    """A misbehaving classifier returning a truthy non-dict must not raise."""
    f = _cannot_place_finding()
    out = mdgen._apply_fit_classifier(
        SimpleNamespace(fit_classifier=lambda _f: ["not", "a", "dict"]), f)
    assert out is f
    assert out.placement_verdict == "cannot_place"


def test_llm_reason_with_band_jargon_falls_back():
    v = md.compute_placement_verdict(_lm("xai", "grok-3"), "medium", "family-map", {})
    finding = _finding("xai", "grok-3", v)

    def stub(_f):
        return {"verdict": "fits_existing", "recommended_role": "standard",
                "confidence": 0.8, "reason": "Its cost band matches your standard work."}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), finding)
    assert "band" not in out.fit_reason.lower()
    assert out.fit_reason == finding.fit_reason  # clean deterministic fallback


def test_failopen_no_classifier_keeps_deterministic():
    f = _cannot_place_finding()
    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=None), f)
    assert out is f  # untouched


def test_failopen_classifier_raises():
    f = _cannot_place_finding()

    def boom(_f):
        raise RuntimeError("model exploded")

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=boom), f)
    assert out is f
    assert out.placement_verdict == "cannot_place"
    assert out.fit_source == "deterministic"


def test_failopen_classifier_returns_none():
    f = _cannot_place_finding()
    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=lambda _f: None), f)
    assert out is f


def test_failopen_low_confidence_kept_deterministic():
    f = _cannot_place_finding()

    def stub(_f):
        return {"verdict": "fits_existing", "recommended_role": "max",
                "confidence": 0.3, "reason": "maybe"}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), f)
    assert out.placement_verdict == "cannot_place"
    assert out.fit_source == "deterministic"


def test_failopen_invalid_role_kept_deterministic():
    f = _cannot_place_finding()

    def stub(_f):
        return {"verdict": "fits_existing", "recommended_role": "overlord",
                "confidence": 0.95, "reason": "x"}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), f)
    assert out.placement_verdict == "cannot_place"
    assert out.fit_source == "deterministic"


def test_failopen_invalid_verdict_kept_deterministic():
    f = _cannot_place_finding()

    def stub(_f):
        return {"verdict": "obliterate", "recommended_role": None,
                "confidence": 0.95, "reason": "x"}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), f)
    assert out.placement_verdict == "cannot_place"
    assert out.fit_source == "deterministic"


def test_failopen_fits_existing_role_maps_no_rung_kept_deterministic(monkeypatch):
    """A valid role that resolves to no rung (broken catalog) → keep det."""
    f = _cannot_place_finding()
    monkeypatch.setattr(md, "_role_to_rung_map", lambda: {})

    def stub(_f):
        return {"verdict": "fits_existing", "recommended_role": "fast",
                "confidence": 0.9, "reason": "x"}

    out = mdgen._apply_fit_classifier(SimpleNamespace(fit_classifier=stub), f)
    assert out.placement_verdict == "cannot_place"
    assert out.fit_source == "deterministic"


# ── new-rung literal is GONE (regression) + applier guard ─────────────────────

def test_unplaceable_signal_carries_empty_slug_not_new_rung(tmp_path):
    listings = {"anthropic": [_lm("anthropic", "zenith-1")]}
    specs = mdgen.observe_signals(_ctx(tmp_path, listings))
    disc = [s for s in specs if s["type"] == "model_discovery"]
    assert len(disc) == 1
    d = disc[0]["details"]
    assert d["placement_verdict"] == "cannot_place"
    assert d["recommended_rung_slug"] is None
    assert d["suggested_rung_slug"] == ""           # NOT "new-rung"
    assert d["fit_source"] == "deterministic"
    assert d["fit_reason"]
    # vocabulary clean in both the reason and the Signal body.
    for bad in ("rung", "band", "dormant", "new-rung"):
        assert bad not in d["fit_reason"].lower()
        assert bad not in disc[0]["body"].lower()


def test_applier_refuses_placeholder_slug_and_mints_nothing():
    state = {"net": {"bots": {}, "models": {"rungs": []}}}
    adopt_model.set_network_io(
        lambda: state["net"], lambda n: state.__setitem__("net", n))
    try:
        for bad in ("new-rung", "new_rung", "", "  ", "none", "unplaced"):
            r = AdoptModelApplier().apply(
                AdoptModel(provider="anthropic", model_id="frontier-7",
                           rung_slug=bad, position=0, cost_class="medium"),
                "<pod>",
            )
            assert not r.ok, f"{bad!r} should be rejected"
            assert state["net"]["models"]["rungs"] == [], f"{bad!r} minted a rung"
    finally:
        adopt_model.set_network_io(None, None)


def test_applier_still_adopts_a_real_slug():
    """The guard rejects placeholders but a real slug still adopts (idempotent
    end-to-end check that the guard didn't break the normal path)."""
    state = {"net": {"bots": {}, "models": {"rungs": []}}}
    adopt_model.set_network_io(
        lambda: state["net"], lambda n: state.__setitem__("net", n))
    try:
        r = AdoptModelApplier().apply(
            AdoptModel(provider="anthropic", model_id="claude-mythos-5",
                       rung_slug="fable-class", position=3, cost_class="premium"),
            "<pod>",
        )
        assert r.ok, r.message
        rungs = state["net"]["models"]["rungs"]
        assert any(rg["id"] == "fable-class"
                   and "anthropic/claude-mythos-5" in rg["models"] for rg in rungs)
    finally:
        adopt_model.set_network_io(None, None)


# ── fit_llm: no hardcoded model id, fail-open build, parse/sanitize ───────────

def test_resolve_fit_target_comes_from_infra_llm_not_literal(monkeypatch):
    """The fit target is resolved via infra_llm's standard role (#3466) —
    provider-qualified, never a hardcoded literal, never uncredentialed."""
    import infra_llm as _infra

    sentinel = _infra.InfraLLMTarget(
        "openai", "openai/gpt-4o", "sk-test-not-a-real-key")
    captured = {}

    def fake_resolve(role, *, network=None, model_override=""):
        captured["role"] = role
        return sentinel

    monkeypatch.setattr(_infra, "resolve_infra_llm", fake_resolve)
    assert fit_llm.resolve_fit_target(_NETWORK) is sentinel
    assert captured["role"] == "standard"


def test_build_user_message_includes_families_line_only_when_passed():
    """The trusted families line lets the LLM perform the mode_variant family
    tie — it appears when (and only when) families are passed; the model id
    stays clearly labelled untrusted either way (injection-safety unchanged)."""
    ev = {"cost_band": "medium", "mode_name_tokens": ["thinking"]}
    _LINE = "Model families this pod already runs (trusted):"

    with_fams = fit_llm._build_user_message(
        "prov/workhorse-4-thinking", ev,
        known_families=["some-provider/workhorse", "some-provider/quick"],
    )
    assert _LINE in with_fams
    assert "some-provider/workhorse" in with_fams
    assert "MODEL ID (untrusted data): prov/workhorse-4-thinking" in with_fams

    # Omitted with no families arg, and with an explicit empty list.
    assert _LINE not in fit_llm._build_user_message("prov/x", ev)
    assert _LINE not in fit_llm._build_user_message("prov/x", ev, known_families=[])


def test_resolve_known_families_returns_stems_and_fails_open():
    """The pod's adopted family stems come from the SAME source the
    deterministic diff uses (known_model_set → known_family_stems); any
    malformed input fails open to a list rather than raising."""
    fams = fit_llm.resolve_known_families(_NETWORK)
    assert isinstance(fams, list)
    # The fixture pod runs sonnet + opus; their family stems must surface.
    assert "claude-sonnet" in fams, fams
    assert "claude-opus" in fams, fams
    # Fail-open: a malformed network must not raise; it yields [].
    assert fit_llm.resolve_known_families({"bots": "not-a-dict"}) == []
    # None is tolerated and still returns a list (the code-default families).
    assert isinstance(fit_llm.resolve_known_families(None), list)


def test_build_default_classifier_failopen_no_provider():
    assert fit_llm.build_default_classifier(
        {}, target_resolver=lambda _n: None) is None


def _openai_target():
    from infra_llm import InfraLLMTarget

    return InfraLLMTarget("openai", "openai/gpt-4o", "sk-test-not-a-real-key")


def test_build_default_classifier_wires_and_calls():
    """Non-Anthropic flow-through (#3466): an openai target dispatches to the
    openai-compatible endpoint with the resolved (never hardcoded) model."""
    calls = []

    def transport(url, headers, body):
        calls.append((url, dict(headers), body))
        return 200, {"choices": [{"message": {"content": json.dumps({
            "verdict": "fits_existing", "recommended_role": "fast",
            "confidence": 0.9, "reason": "cheap and quick",
        })}}]}

    classify = fit_llm.build_default_classifier(
        {}, target_resolver=lambda _n: _openai_target(), transport=transport,
    )
    assert classify is not None
    finding = SimpleNamespace(
        model_id="prov/new-model", placement_verdict="cannot_place",
        fit_evidence={"cost_band": None, "capability_name_tokens": ["reasoning"]},
    )
    out = classify(finding)
    assert out == {
        "verdict": "fits_existing", "recommended_role": "fast",
        "confidence": 0.9, "reason": "cheap and quick",
    }
    url, headers, body = calls[0]
    # model id came from the resolved target (no hardcoded literal).
    assert url == "https://api.openai.com/v1/chat/completions"
    assert body["model"] == "gpt-4o"
    # untrusted model id labelled, system prompt rode in the system slot.
    assert body["messages"][0]["role"] == "system"
    assert "MODEL ID (untrusted data): prov/new-model" in \
        body["messages"][1]["content"]


def test_fit_classifier_failopen_on_api_error():
    def boom(url, headers, body):
        raise RuntimeError("503")

    classify = fit_llm.make_fit_classifier(_openai_target(), transport=boom)
    out = classify(SimpleNamespace(model_id="x", placement_verdict="cannot_place",
                                   fit_evidence={}))
    assert out is None


def test_parse_response_valid_fenced_and_garbage():
    ok = fit_llm._parse_response(
        '```json\n{"verdict":"new_tier","recommended_role":null,'
        '"confidence":0.7,"reason":"hi"}\n```')
    assert ok["verdict"] == "new_tier" and ok["recommended_role"] is None
    assert fit_llm._parse_response("not json at all") is None
    assert fit_llm._parse_response('{"verdict":"bogus","confidence":0.9}') is None
    assert fit_llm._parse_response('{"verdict":"new_tier","confidence":"NaNly"}') is None
    # invalid role coerces to None, not a rejection.
    coerced = fit_llm._parse_response(
        '{"verdict":"fits_existing","recommended_role":"overlord","confidence":0.8}')
    assert coerced["recommended_role"] is None


def test_parse_response_accepts_off_ladder_verdicts():
    """The five-value enum: mode_variant + specialist parse (both role-null)."""
    mv = fit_llm._parse_response(
        '{"verdict":"mode_variant","recommended_role":null,"confidence":0.8,"reason":"x"}')
    assert mv["verdict"] == "mode_variant" and mv["recommended_role"] is None
    sp = fit_llm._parse_response(
        '{"verdict":"specialist","recommended_role":null,"confidence":0.7,"reason":"y"}')
    assert sp["verdict"] == "specialist"
    assert fit_llm._VALID_VERDICTS == {
        "fits_existing", "new_tier", "mode_variant", "specialist", "cannot_place"}


def test_sanitize_reason_strips_control_and_caps_length():
    assert fit_llm.sanitize_reason("a\nb\tc\x00d") == "a b c d"
    long = fit_llm.sanitize_reason("x" * 400)
    assert len(long) <= 281 and long.endswith("…")
    assert fit_llm.sanitize_reason(None) == ""


def test_signal_details_field_shape(tmp_path):
    """Lock the Signal-detail field shape Bite 2 depends on."""
    listings = {"anthropic": [_lm("anthropic", "zenith-1")]}
    specs = mdgen.observe_signals(_ctx(tmp_path, listings))
    d = next(s["details"] for s in specs if s["type"] == "model_discovery")
    for key in (
        "placement_verdict", "recommended_role", "recommended_rung_slug",
        "fit_reason", "fit_confidence", "fit_evidence", "fit_source",
        "cost_band_source", "cost_band_evidence",
    ):
        assert key in d, key
    assert d["placement_verdict"] in (
        "fits_existing", "new_tier", "mode_variant", "specialist", "cannot_place")
    assert isinstance(d["fit_evidence"], dict)
    assert isinstance(d["fit_confidence"], float)
    # The off-ladder name tokens the LLM + Bite 2 read are always present.
    assert "mode_name_tokens" in d["fit_evidence"]
    assert "workload_name_tokens" in d["fit_evidence"]
