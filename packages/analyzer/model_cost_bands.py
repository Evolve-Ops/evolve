"""model_cost_bands — compute a model's cost band from pricing (Phase 11b-2).

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §"Addendum 8 §B + §C".

Bite 11b-1 built the *data layer* (``model_pricing``): it mirrors LiteLLM +
models.dev into ``{shared_dir}/model-pricing.json`` and reads a model's price
back. This module makes that price *useful* — it maps a price to a ``costClass``
band (``low | medium | high | premium``, the four the catalog rungs already use)
so the catalog's tier can be **computed**, never hand-typed (the rot that minted
a fictional model id in Phase 10d).

The cardinal design choice is **anchor-relative thresholds, not magic dollars**.
A model's band is decided by where its input price sits relative to the *anchor
models* — the primaries of the canonical rungs (haiku→low, sonnet→medium,
opus→high, fable→premium). The anchors and their bands are DATA, derived from
``primary_bot.DEFAULT_MODEL_CATALOG`` (the single source of truth for "which
model anchors which band"); the band boundaries are the geometric midpoints
between consecutive anchor prices, read live from the pricing cache. So as the
market re-prices the ladder, the bands track it — there is no hardcoded "$X is
premium" to rot.

Resolution order for any (provider, model_id), per spec §B:

  1. **exact pricing-cache hit** → the anchor-relative computed band.
  2. **no external price, but the pod has TRUSTWORTHY observed cost** → band
     the model from its own ``cost_event`` telemetry (``usage_analytics``'s
     per-model ``usd_per_1k_blended``), mapped onto the *same* anchor-relative
     scale. This covers the frontier-lag window with real measured spend
     instead of a curated guess — so it outranks the family map below. Gated on
     the min-sample flag (``low_confidence == False``): a noisy few-call figure
     never drives a band.
  3. **still nothing, family known** → the hand-curated ``FAMILY_COST_BAND``
     fallback, keyed on the family parsed from the (correctly-spelled) listing
     id via discovery's ``_family_of`` — the curated cover for a brand-new
     model that is in the provider listing, has no observed traffic yet, and is
     not yet in any pricing catalog.
  4. **all miss** → ``None`` (the caller emits a ``model_discovery`` Signal).
     We do **not** invent a band — that is exactly the rot this effort removes.

This module computes bands and resolves them; it holds NO provider/model
literals in logic (anchors come from the catalog DATA, families from the
``FAMILY_COST_BAND`` DATA map), makes NO network calls (the cache reader is
11b-1's), and writes nothing.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import model_pricing

_log = logging.getLogger(__name__)


def _family_of(model_id: str) -> str:
    """Family stem for ``model_id`` (e.g. ``claude-opus``), via discovery's
    canonical parser. Imported lazily inside the function to avoid a circular
    import: ``model_discovery`` imports this module (to derive cost bands during
    the diff), so a top-level ``from model_discovery import _family_of`` here
    would form a cycle. The family rule lives in ONE place (``model_discovery``);
    this is a thin forwarder, not a reimplementation."""
    from model_discovery import _family_of as _fam
    return _fam(model_id)


# ── Band vocabulary (ordering DATA — the four costClass values) ───────────────
# Cheapest first. Mirrors ``_COST_CLASS_ORDER`` in primary_bot / models.py;
# defined locally to keep this module's import surface small. Ordering data,
# never a provider/model literal.
COST_BANDS: tuple[str, ...] = ("low", "medium", "high", "premium")


# ── Family → cost-band fallback (curated DATA — the frontier-lag cover) ────────
# Spec §B "Fallback for the freshness gap": every pricing catalog is
# community-PR-maintained, so a brand-new frontier model can appear in the
# provider's own listing BEFORE any pricing catalog prices it. This tiny
# hand-curated map (~one row per family × ~6 providers) covers that window,
# keyed on the family ``_family_of`` parses from the correctly-spelled listing
# id. When the catalog PR lands, the exact pricing row supersedes this row
# automatically (resolution tries the cache first).
#
# This is DATA in the same category as ``DEFAULT_MODEL_CATALOG`` / the
# provider-alias table: a new family is a one-row edit, never a code change in
# the resolution logic. The families here are the stable line names (versions
# churn; the family + its band do not). Curated, not computed — by construction
# (a model with NO price anywhere can't be computed).
#
# MODEL LAUNCH: add a row when a provider ships a new family before the pricing
# catalogs catch up; drop/repoint a row if a family's market band shifts.
FAMILY_COST_BAND: dict[str, str] = {
    # Anthropic lines (Haiku=fast, Sonnet=standard, Opus=power, Fable/Mythos=frontier).
    "claude-haiku": "low",
    "claude-sonnet": "medium",
    "claude-opus": "high",
    "claude-fable": "premium",
    "claude-mythos": "premium",
    # OpenAI lines (mini/nano = cheap; the flagship gpt line = mid).
    "gpt-mini": "low",
    "gpt-nano": "low",
    "gpt": "medium",
    # Google Gemini lines (flash = cheap; pro/ultra = mid/high).
    "gemini-flash": "low",
    "gemini-pro": "medium",
    "gemini-ultra": "high",
    # xAI Grok lines (mini = cheap; flagship grok = mid).
    "grok-mini": "low",
    "grok": "medium",
    # DeepSeek (chat = cheap workhorse; reasoner = mid).
    "deepseek-chat": "low",
    "deepseek-reasoner": "medium",
    # Mistral lines (small = cheap; large = flagship mid; codestral =
    # code-specialist workhorse, mid). Family stems are what `_family_of` parses
    # from the listing id (mistral-large-latest -> mistral-large, etc.).
    "mistral-large": "medium",
    "mistral-small": "low",
    "codestral": "medium",
}


# ── Anchor scale (computed live from the pricing cache) ────────────────────────

@dataclass(frozen=True)
class _Anchor:
    """One band's anchor model and its looked-up input price ($/token).

    The anchor is the *primary* (first) model of the canonical rung that carries
    this band in ``DEFAULT_MODEL_CATALOG``. ``input_cost_per_token`` is filled
    from the pricing cache; ``None`` when the anchor itself isn't priced (its
    boundary is then skipped — the scale degrades to the anchors that ARE
    priced rather than fabricating a number)."""

    band: str
    provider: str
    model_id: str
    input_cost_per_token: float | None


def _default_catalog() -> dict[str, Any]:
    """The code-shipped blessed ladder (deep copy). Lazy import keeps the
    module light and avoids a hard primary_bot dependency at import time."""
    try:
        from primary_bot import default_model_catalog
        return default_model_catalog()
    except Exception:  # pragma: no cover - defensive
        return {}


def anchor_models(catalog: dict[str, Any] | None = None) -> dict[str, tuple[str, str]]:
    """Map each cost band → its anchor ``(provider, model_id)`` from the catalog.

    The anchor for a band is the *primary* model of the canonical rung whose
    ``costClass`` equals that band — i.e. ``DEFAULT_MODEL_CATALOG``'s
    haiku-class→low, sonnet-class→medium, opus-class→high, fable-class→premium,
    read straight from the catalog DATA so this function names no provider or
    model. When several rungs carry the same costClass, the first in array order
    wins (matches the cost-order tiebreak elsewhere). The id is split into
    ``(provider, bare_id)`` on the leading ``/``; an unqualified id yields an
    empty provider (it simply won't match a priced row).
    """
    cat = catalog if catalog is not None else _default_catalog()
    out: dict[str, tuple[str, str]] = {}
    for rung in (cat.get("rungs") or []):
        if not isinstance(rung, dict):
            continue
        band = rung.get("costClass")
        if band not in COST_BANDS or band in out:
            continue
        models = rung.get("models") or []
        primary = next((m for m in models if isinstance(m, str) and m), None)
        if not primary:
            continue
        provider, _, bare = primary.partition("/")
        out[band] = (provider, bare) if bare else ("", provider)
    return out


def _priced_anchors(
    cache: dict | None, catalog: dict[str, Any] | None = None,
) -> list[_Anchor]:
    """Resolve each band's anchor and look up its input price from the cache.

    Returns one ``_Anchor`` per band the catalog defines, sorted by **price**
    (cheapest first) among the priced ones, with unpriced anchors appended.
    The ladder is supposed to be price-monotonic (haiku < sonnet < opus <
    fable); sorting by the *looked-up* price rather than trusting the band
    order makes the boundaries robust to a temporarily mis-ordered catalog and
    is the literal "place boundaries between consecutive anchors" the spec asks
    for.
    """
    anchors_by_band = anchor_models(catalog)
    anchors: list[_Anchor] = []
    for band, (provider, model_id) in anchors_by_band.items():
        rec = model_pricing.lookup_price(cache, provider, model_id) if provider else None
        price = None
        if rec is not None:
            price = rec.get("input_cost_per_token")
            price = float(price) if isinstance(price, (int, float)) else None
        anchors.append(_Anchor(band, provider, model_id, price))
    # Priced anchors sorted by price (cheapest first); unpriced trail.
    priced = sorted(
        (a for a in anchors if a.input_cost_per_token is not None),
        key=lambda a: a.input_cost_per_token,  # type: ignore[arg-type, return-value]
    )
    unpriced = [a for a in anchors if a.input_cost_per_token is None]
    return priced + unpriced


def _boundaries(priced: list[_Anchor]) -> list[tuple[float, str]]:
    """Build the [(upper_bound, band)] classification scale from priced anchors.

    Each priced anchor owns a band; a boundary between two consecutive anchors
    sits at their **geometric midpoint** (``sqrt(p_i * p_{i+1})``) — the right
    midpoint for prices that span orders of magnitude (a model 3× a haiku price
    but 1/3 a sonnet price should read as the cheaper band proportionally, not
    by an arithmetic midpoint that the larger number dominates). Returns
    ascending ``(upper_bound, band)`` pairs; the last band has an open top
    (represented by ``inf``). A single priced anchor yields one open band (that
    anchor's). Empty input → empty scale (caller falls through to the family
    map).
    """
    priced = [a for a in priced if a.input_cost_per_token is not None]
    if not priced:
        return []
    scale: list[tuple[float, str]] = []
    for i, a in enumerate(priced):
        if i + 1 < len(priced):
            nxt = priced[i + 1]
            lo = a.input_cost_per_token or 0.0
            hi = nxt.input_cost_per_token or 0.0
            bound = math.sqrt(lo * hi) if lo > 0 and hi > 0 else (lo + hi) / 2.0
            scale.append((bound, a.band))
        else:
            scale.append((math.inf, a.band))
    return scale


def compute_band(
    input_cost_per_token: float | None,
    *,
    cache: dict | None,
    catalog: dict[str, Any] | None = None,
) -> str | None:
    """Classify an input price ($/token) into a band, anchor-relative.

    Reads the band anchors' prices from ``cache`` (via ``model_pricing``), places
    the geometric-midpoint boundaries between consecutive anchors, and returns
    the band whose interval contains ``input_cost_per_token``. Returns ``None``
    when the price is missing/non-positive OR the cache prices no anchors at all
    (no scale can be built) — the caller then tries the family map. Never
    fabricates a band from a magic dollar number; the only inputs are the live
    anchor prices.
    """
    if not isinstance(input_cost_per_token, (int, float)):
        return None
    if input_cost_per_token <= 0:
        return None
    scale = _boundaries(_priced_anchors(cache, catalog))
    if not scale:
        return None
    for upper, band in scale:
        if input_cost_per_token <= upper:
            return band
    # math.inf top guarantees a match, but be defensive: return the top band.
    return scale[-1][1]


def family_band(model_id: str) -> str | None:
    """Fallback band from the curated ``FAMILY_COST_BAND`` map, keyed on the
    family ``_family_of`` parses from the (correctly-spelled) listing id.

    Used when the pricing cache has no row for a model (the frontier-lag
    window). Returns ``None`` when the family is unknown to the map — the caller
    must then emit a ``model_discovery`` Signal rather than invent a band.
    """
    if not model_id:
        return None
    return FAMILY_COST_BAND.get(_family_of(model_id))


# ── Observed-cost fallback (the pod's own telemetry as a band source) ──────────
# Spec §C, observed-cost addendum. The external catalogs lag the frontier: a
# brand-new model can run on the pod for days before LiteLLM/models.dev price
# it. But the pod is *already paying* for those calls, and ``usage_analytics``
# already rolls that spend up per model (``compute_summary``'s ``by_model``
# rows: ``usd_per_1k_blended`` + the ``low_confidence`` min-sample flag). That
# measured figure is a band source in its own right — and a better one than the
# curated family map, because it is real money, not a name match. We map it onto
# the *same* anchor-relative scale the external pricing uses, never a separate
# magic-dollar threshold.

def observed_band(
    usd_per_1k_blended: float | None,
    *,
    low_confidence: bool | None,
    cache: dict | None,
    catalog: dict[str, Any] | None = None,
) -> str | None:
    """Classify a model's OBSERVED blended $/1k-token cost into a band.

    ``usd_per_1k_blended`` is ``usage_analytics``'s measured per-model rate
    (total spend ÷ ((input+output) tokens / 1000)); ``low_confidence`` is its
    companion min-sample flag (``billed_tokens < MODEL_COST_MIN_TOKENS`` — the
    single source of truth for "enough traffic to trust the ratio", owned by
    ``usage_analytics``, never re-derived here).

    Gating — never band off a noisy figure:
      - ``low_confidence`` MUST be ``False``. A ``True`` flag (under the
        min-sample threshold) **or** a missing/``None`` flag (provenance
        unknown) → no band. This is the min-sample gate the spec requires.
      - the blended figure must be a positive number.

    Mapping: the figure is a per-**1k** rate, so ÷1000 gives the per-token
    figure ``compute_band`` expects, then it rides the exact anchor scale
    (geometric-midpoint boundaries between the catalog anchors' prices).

    Unit note: that anchor scale is built from each anchor's
    ``input_cost_per_token``, whereas the observed figure is a BLENDED
    (input+output) rate. Blended ≥ input-only, so an observed model bands
    slightly toward the *more expensive* side of where its input-only price
    would land — the safe direction for a cost guard (over-, not under-,
    flagging spend). Returns ``None`` when gated out, or when no anchor scale
    can be built (the caller then falls through to the family map / Signal).
    """
    if low_confidence is not False:
        return None
    if not isinstance(usd_per_1k_blended, (int, float)) or isinstance(
        usd_per_1k_blended, bool
    ):
        return None
    if usd_per_1k_blended <= 0:
        return None
    blended_cost_per_token = float(usd_per_1k_blended) / 1000.0
    return compute_band(blended_cost_per_token, cache=cache, catalog=catalog)


def observed_row_for(
    observed: list[dict] | None, provider: str, model_id: str,
) -> dict | None:
    """Find the observed ``by_model`` row for ``(provider, model_id)``, or None.

    ``observed`` is ``usage_analytics.compute_summary``'s ``by_model`` list;
    each row's ``model`` is the qualified id (``provider/model_id``), optionally
    carrying a ``:unexpected_billing`` suffix (a distinct billing series for the
    *same* model — stripped before matching). Match is on the canonical
    (provider, bare-id) key, case-insensitive on the provider only (model ids
    are case-sensitive identifiers). Returns the row dict (carrying
    ``usd_per_1k_blended`` + ``low_confidence``) or ``None`` when the pod has no
    traffic for that model.
    """
    if not observed:
        return None
    prov = (provider or "").strip().lower()
    bare = model_id.split("/", 1)[1] if "/" in model_id else model_id
    for row in observed:
        if not isinstance(row, dict):
            continue
        key = str(row.get("model") or "").split(":", 1)[0]  # drop billing suffix
        kp, sep, kb = key.partition("/")
        if sep:
            if kp.strip().lower() == prov and kb == bare:
                return row
        elif key == bare:  # bare row (no provider prefix)
            return row
    return None


@dataclass(frozen=True)
class BandResolution:
    """A model's resolved cost band plus the evidence behind it.

    ``band`` is ``None`` when neither the cache, the pod's observed cost, nor
    the family map could place the model (the caller emits a ``model_discovery``
    Signal). ``source`` is one of
    ``"pricing" | "observed" | "family-map" | "unknown"``. When
    ``source == "pricing"``, ``evidence`` cites the price + catalog source
    (cite-or-don't-recommend); when ``"observed"`` it cites the measured
    ``usd_per_1k_blended`` + ``billed_tokens``; when ``"family-map"`` it cites
    the matched family; when ``"unknown"`` it is empty and ``band is None``.
    """

    band: str | None
    source: str
    evidence: dict[str, Any]


def resolve_band(
    provider: str,
    model_id: str,
    *,
    cache: dict | None,
    catalog: dict[str, Any] | None = None,
    observed: list[dict] | None = None,
) -> BandResolution:
    """Resolve a model's cost band by the spec §B order: exact pricing row →
    observed cost → family map → unknown.

    1. **exact pricing-cache hit** → ``compute_band`` of its input price
       (anchor-relative). Evidence cites the $/token and the catalog ``source``.
    2. **no external price, but TRUSTWORTHY observed cost** → ``observed_band``
       of the pod's measured ``usd_per_1k_blended`` for this model (looked up in
       ``observed`` — ``usage_analytics``'s ``by_model`` list — and gated on the
       ``low_confidence`` min-sample flag). Real measured spend, so it outranks
       the curated family map. Evidence cites the blended $/1k + billed tokens.
    3. **still nothing, family known** → ``family_band`` of the listing id.
       Evidence cites the matched family.
    4. **all miss** → ``band=None, source="unknown"`` — the caller emits a
       ``model_discovery`` Signal; this function never fabricates a band.

    ``observed`` defaults to ``None``: a caller that passes no telemetry gets
    the original pricing→family→unknown order unchanged (no regression).
    External pricing always wins — observed is consulted ONLY when the model has
    no external price (or is priced but the anchors aren't, so no scale built).
    """
    rec = model_pricing.lookup_price(cache, provider, model_id)
    if rec is not None:
        in_cost = rec.get("input_cost_per_token")
        band = compute_band(in_cost, cache=cache, catalog=catalog)
        if band is not None:
            return BandResolution(
                band=band,
                source="pricing",
                evidence={
                    "input_cost_per_token": in_cost,
                    "output_cost_per_token": rec.get("output_cost_per_token"),
                    "pricing_source": rec.get("source") or "",
                    "provider": rec.get("provider") or provider,
                    "model_id": rec.get("model_id") or model_id,
                },
            )
        # Priced but no anchor scale (cache prices the model but not the
        # anchors) → fall through to observed / the family map rather than None.

    # Observed-cost fallback: the pod's own telemetry bands an un-priced model
    # from real measured spend, gated on the min-sample flag so a noisy figure
    # never drives a band. Preferred over the family map (real data > guess).
    obs_row = observed_row_for(observed, provider, model_id)
    if obs_row is not None:
        obs_band = observed_band(
            obs_row.get("usd_per_1k_blended"),
            low_confidence=obs_row.get("low_confidence"),
            cache=cache,
            catalog=catalog,
        )
        if obs_band is not None:
            return BandResolution(
                band=obs_band,
                source="observed",
                evidence={
                    "usd_per_1k_blended": obs_row.get("usd_per_1k_blended"),
                    "billed_tokens": obs_row.get("billed_tokens"),
                    "observed_source": "pod-telemetry",
                },
            )

    fam_band = family_band(model_id)
    if fam_band is not None:
        return BandResolution(
            band=fam_band,
            source="family-map",
            evidence={"family": _family_of(model_id), "band": fam_band},
        )

    return BandResolution(band=None, source="unknown", evidence={})
