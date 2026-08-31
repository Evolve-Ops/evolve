"""turn_cost — the pod's ONE per-turn cost rule (price it, or say you can't).

Audit finding **B6** (`internal/audit-alpha-journey-2026-08.md` §6): the old
`usage_analytics._estimate_turn_cost` priced from a hardcoded six-provider
table and returned ``0.0`` for everything else. On a pod whose provider is
off-table — OpenRouter or a local Ollama, both of which
`docs/help/installation.md` explicitly supports — a day of real turns summed
to ``$0.00``, the tile reported ``usd_28d: 0.0`` with ``live_today: true``
(*measured, and it is zero*), and, because the spend cap shares the same
expression, **the cap never tripped**. A silent zero is indistinguishable from
"you spent nothing", which is the exact failure `docs/principle-tri-state-status.md`
exists to forbid.

This module is the single home for that rule, so no reader can fork it:

  * :func:`estimate_turn_cost` — token counts → USD, or ``None`` ("can't price").
    Resolution order is **catalog → offline table → can't-price**:
      1. ``{shared_dir}/model-pricing.json`` (``model_pricing.read_pricing_cache``
         + ``lookup_price``) — the full normalized catalog the pod already
         fetches on its discovery sweep, covering the whole LiteLLM +
         models.dev union rather than six hand-kept providers. Note that the
         catalog does NOT hold a row per gateway: LiteLLM deliberately skips
         its re-host surfaces (``openrouter``, ``together_ai``, ``azure``,
         Bedrock, Vertex) so each logical model is priced once, on its native
         vendor row — see :func:`_catalog_candidates` for how a gateway turn
         still resolves.
      2. :data:`OFFLINE_MODEL_PRICING` / :data:`OFFLINE_PROVIDER_PRICING` — the
         hand-kept tables, now demoted to the **offline** fallback for a pod
         whose catalog has not been fetched yet (or whose catalog has no record
         for this model).
      3. ``None``. Never ``0.0``. We do not invent a price, and we do not widen
         the offline table as a substitute for reading the catalog.
  * :func:`turn_cost` — the recorded-else-estimate rule every reader used to
    spell out inline (``recorded if recorded else _estimate_turn_cost(t)``).
    A recorded non-zero cost always wins; otherwise the estimate, which may be
    ``None``.
  * :func:`sum_turn_costs` — the aggregator. Returns a :class:`TurnCostTotal`
    that carries ``unpriced_turns`` **beside** the dollar figure rather than
    folding it in as zero, so every surface can say "can't price 41 turns"
    instead of printing a confident total.

``None`` is deliberately a *loud* carrier: an untaught caller doing arithmetic
on it raises ``TypeError`` rather than silently resuming the B6 behaviour.

A note on cache tokens under catalog pricing: a cache-heavy agent turn is
mostly cache-READ tokens, which most vendors bill at roughly a tenth of the
input rate — so getting the cache rate wrong moves the total by an order of
magnitude, in either direction. Rates are therefore taken, in order, from the
catalog record's own ``cache_read_cost_per_token`` /
``cache_write_cost_per_token`` (added to ``model_pricing.PricedModel`` for
this work), then from the offline table's cache columns for the same model,
and only failing both from the record's input rate — the over-stating
direction, which for a spend cap is the safe one. Every step is a published
rate for that model, never a fabricated one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

# ── Offline pricing tables (USD per million tokens) ───────────────────────────
# Used when ``{shared_dir}/model-pricing.json`` is absent or has no record for
# the model. Prices are approximate public list prices. Format:
# model_key → (input, output, cache_write, cache_read), all USD per 1,000,000
# tokens. Do NOT grow these tables to cover a provider the catalog already
# knows — that is the shortcut B6 warns against; fix the catalog fetch instead.
OFFLINE_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Anthropic — (input, output, cache_write, cache_read) per MTok
    "anthropic/claude-haiku-4-6":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-haiku-4-5":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-haiku-3-5":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-3-haiku":            (0.25,   1.25,  0.30, 0.03),
    "anthropic/claude-sonnet-4-6":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-sonnet-4-5":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-sonnet-4-20250514":  (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-sonnet-4-20250219":  (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-3-5-sonnet":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-3-sonnet":           (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-opus-4-6":           (15.00, 75.00, 18.75, 1.50),
    "anthropic/claude-opus-4-5":           (15.00, 75.00, 18.75, 1.50),
    "anthropic/claude-3-opus":             (15.00, 75.00, 18.75, 1.50),
    # OpenAI — no prompt caching price difference, use 0 for cache fields
    "openai/gpt-4o":                       (2.50,  10.00,  0.00, 1.25),
    "openai/gpt-4o-mini":                  (0.15,   0.60,  0.00, 0.075),
    "openai/gpt-4.1":                      (2.00,   8.00,  0.00, 1.00),
    "openai/gpt-4.1-mini":                 (0.40,   1.60,  0.00, 0.20),
    "openai/gpt-4.1-nano":                 (0.10,   0.40,  0.00, 0.05),
    "openai/o3":                           (10.00,  40.00, 0.00, 2.50),
    "openai/o4-mini":                      (1.10,   4.40,  0.00, 0.275),
    # Google — Gemini
    "google/gemini-3.1-pro-preview":       (1.25,  10.00,  0.00, 0.00),
    "google/gemini-2.5-pro-preview":       (1.25,  10.00,  0.00, 0.00),
    "google/gemini-1.5-pro":               (1.25,   5.00,  0.00, 0.00),
    "google/gemini-2.0-flash":             (0.10,   0.40,  0.00, 0.00),
    "google/gemini-2.0-flash-lite":        (0.075,  0.30,  0.00, 0.00),
    # xAI
    "xai/grok-3":                          (3.00,  15.00,  0.00, 0.00),
    "xai/grok-3-mini":                     (0.30,   0.50,  0.00, 0.00),
    "xai/grok-4-1-fast":                   (5.00,  25.00,  0.00, 0.00),
}

# Provider-level offline pricing when the exact model is not in the table
# above. Mid-range estimates so a known provider's unknown model isn't free.
OFFLINE_PROVIDER_PRICING: dict[str, tuple[float, float, float, float]] = {
    "anthropic": (3.00,  15.00, 3.75, 0.30),
    "openai":    (2.50,  10.00, 0.00, 1.25),
    "google":    (1.25,   5.00, 0.00, 0.00),
    "xai":       (3.00,  15.00, 0.00, 0.00),
    "mistral":   (2.00,   6.00, 0.00, 0.00),
    "groq":      (0.59,   0.79, 0.00, 0.00),
}

_MTok = 1_000_000.0

# Back-compat aliases — the pre-B6 private names, still imported by
# ``install_cost_estimator`` (via ``usage_analytics``) for its projection path.
_MODEL_PRICING = OFFLINE_MODEL_PRICING
_PROVIDER_PRICING_FALLBACK = OFFLINE_PROVIDER_PRICING

_UNKNOWN_PROVIDER = "unknown"


# ── Pricing catalog (the {shared_dir}/model-pricing.json read) ────────────────
# Memoized on (path, mtime) rather than on first read: the spend daemon is
# long-lived, and a catalog refreshed by the discovery sweep underneath it must
# be picked up on the next tick, not at the next restart.
_CATALOG_CACHE: dict[str, tuple[float, dict | None]] = {}


def _default_shared_dir() -> Path:
    """Shared dir to read the pricing catalog from.

    ``EVOLVE_SHARED`` wins (the env override the runners already honor), then
    the platform-keyed canonical dir. Never a ``/Users`` literal.
    """
    env = (os.environ.get("EVOLVE_SHARED") or "").strip()
    if env:
        return Path(env)
    from evolve_config import CANONICAL_SHARED_DIR  # type: ignore[import]
    return Path(CANONICAL_SHARED_DIR)


def reset_pricing_catalog_cache() -> None:
    """Drop the memoized catalog. Tests call this between fixtures."""
    _CATALOG_CACHE.clear()


def load_pricing_catalog(shared_dir: Path | str | None = None) -> dict | None:
    """Read ``{shared_dir}/model-pricing.json``, or ``None`` when absent.

    ``None`` means "no catalog on this pod" — the caller falls back to the
    offline tables. It never means "this model is free".
    """
    try:
        base = Path(shared_dir) if shared_dir is not None else _default_shared_dir()
    except Exception:
        return None
    try:
        import model_pricing  # type: ignore[import]
    except Exception:
        return None
    try:
        path = model_pricing.pricing_cache_path(base)
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    key = str(path)
    hit = _CATALOG_CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        doc = model_pricing.read_pricing_cache(base)
    except Exception:
        doc = None
    _CATALOG_CACHE[key] = (mtime, doc)
    return doc


# ── The rule ──────────────────────────────────────────────────────────────────


def turn_provider(turn: dict) -> str:
    """Derive a turn's provider: explicit field, else the model prefix, else
    ``"unknown"``. Shared with ``usage_analytics._turn_provider``."""
    provider = (turn.get("provider") or "").strip()
    if provider and provider != _UNKNOWN_PROVIDER:
        return provider
    model = (turn.get("model") or "").strip()
    if "/" in model:
        return model.split("/")[0]
    return _UNKNOWN_PROVIDER


def _qualified_model(turn: dict) -> tuple[str, str]:
    """Return ``(qualified_model, provider)`` for a turn.

    Normalises a bare model onto its provider (``gpt-4o`` + ``openai`` →
    ``openai/gpt-4o``) exactly as the pre-B6 estimator did.
    """
    model = (turn.get("model") or "").strip()
    provider = (turn.get("provider") or "").strip()
    if "/" not in model and provider and provider != _UNKNOWN_PROVIDER:
        model = f"{provider}/{model}"
    return model, provider


def _rate_from_record(rec: dict, field: str) -> float | None:
    """A catalog record's $/token rate for ``field``, as $/MTok, or ``None``
    when the catalog does not carry it (an older cache file, or a source that
    publishes no cache pricing)."""
    raw = rec.get(field)
    if raw is None:
        return None
    try:
        return float(raw) * _MTok
    except (TypeError, ValueError):
        return None


def _catalog_candidates(turn: dict) -> list[tuple[str, str]]:
    """``(provider, model_id)`` pairs to try against the catalog, in order.

    Two shapes matter, and the second is what makes the catalog useful for the
    provider B6 is about:

      1. ``(recorded provider, model)`` — the direct hit.
      2. ``(model's vendor prefix, model)`` — an OpenAI-compatible gateway
         records the model under its VENDOR (``anthropic/claude-...``,
         ``qwen/qwen-...``) while ``provider`` is the gateway. LiteLLM
         deliberately skips its re-host surfaces (``openrouter``, ``together``,
         ``azure``, Bedrock, Vertex — ``model_pricing._LITELLM_REHOST_SURFACES``)
         so each logical model is priced ONCE, on its native vendor row. Pricing
         a passthrough turn from that native row is the model's own published
         price, not an invented one — a gateway's margin is not modelled, and
         the figure is therefore a close floor rather than a guess.
    """
    provider = turn_provider(turn)
    raw = (turn.get("model") or "").strip()
    segs = [seg for seg in raw.split("/") if seg]
    if not segs:
        return []
    # A model already carrying its provider as the first segment
    # (``openrouter/anthropic/claude-3``) should not be looked up under a
    # doubled prefix.
    if len(segs) > 1 and segs[0].lower() == provider.lower():
        segs = segs[1:]
    bare = segs[-1]
    joined = "/".join(segs)
    vendor = segs[0] if len(segs) > 1 else ""

    out: list[tuple[str, str]] = []
    for prov in (provider, vendor):
        if not prov or prov == _UNKNOWN_PROVIDER:
            continue
        for model_id in (bare, joined):
            pair = (prov, model_id)
            if pair not in out:
                out.append(pair)
    return out


def _catalog_pricing(
    turn: dict, catalog: dict | None,
) -> tuple[float, float, float, float] | None:
    """Per-MTok pricing for ``turn`` from the normalized catalog, or ``None``.

    Cache rates resolve catalog → offline table → the record's input rate; see
    the module docstring for why the order matters (an order of magnitude).
    """
    if not catalog:
        return None
    try:
        from model_pricing import lookup_price  # type: ignore[import]
    except Exception:
        return None
    rec = None
    for provider, model_id in _catalog_candidates(turn):
        rec = lookup_price(catalog, provider, model_id)
        if rec:
            break
    if not rec:
        return None
    inp = rec.get("input_cost_per_token")
    out = rec.get("output_cost_per_token")
    if inp is None or out is None:
        return None
    try:
        in_mtok = float(inp) * _MTok
        out_mtok = float(out) * _MTok
    except (TypeError, ValueError):
        return None

    # Cache rates, in order: the catalog's own (added for B6), then the offline
    # table's columns for this same model — a real published rate either way.
    # Only when neither exists do we fall back to the input rate, and that
    # over-states a cache-heavy turn rather than under-stating it, which is the
    # safe direction for a spend cap.
    offline = _offline_pricing(turn)
    cw_mtok = _rate_from_record(rec, "cache_write_cost_per_token")
    if cw_mtok is None:
        cw_mtok = offline[2] if offline is not None else in_mtok
    cr_mtok = _rate_from_record(rec, "cache_read_cost_per_token")
    if cr_mtok is None:
        cr_mtok = offline[3] if offline is not None else in_mtok
    return (in_mtok, out_mtok, cw_mtok, cr_mtok)


def _offline_pricing(turn: dict) -> tuple[float, float, float, float] | None:
    """Per-MTok pricing for ``turn`` from the offline tables, or ``None``."""
    model, provider = _qualified_model(turn)
    pricing = OFFLINE_MODEL_PRICING.get(model)
    if pricing is None and "/" in model:
        pricing = OFFLINE_PROVIDER_PRICING.get(model.split("/")[0])
    if pricing is None and provider and provider != _UNKNOWN_PROVIDER:
        pricing = OFFLINE_PROVIDER_PRICING.get(provider)
    return pricing


def estimate_turn_cost(
    turn: dict,
    *,
    catalog: dict | None = None,
    shared_dir: Path | str | None = None,
) -> float | None:
    """Estimate a turn's USD cost from its token counts.

    Resolution order is catalog → offline tables → ``None``. **Returns ``None``
    — never ``0.0`` — when neither can price the model** (B6). A ``0.0`` return
    is reserved for a model that really is free (a local Ollama model priced at
    zero in the catalog) or a turn that burned no tokens.

    ``catalog`` lets a caller that already read the catalog pass it in so a
    per-turn loop does not re-resolve it; when omitted the memoized
    :func:`load_pricing_catalog` read is used.
    """
    cat = catalog if catalog is not None else load_pricing_catalog(shared_dir)
    pricing = _catalog_pricing(turn, cat)
    if pricing is None:
        pricing = _offline_pricing(turn)
    if pricing is None:
        return None

    input_p, output_p, cw_p, cr_p = pricing
    inp = int(turn.get("input_tokens")       or 0)
    out = int(turn.get("output_tokens")      or 0)
    cw  = int(turn.get("cache_write_tokens") or 0)
    cr  = int(turn.get("cache_read_tokens")  or 0)

    return (
        inp * input_p  / _MTok
        + out * output_p / _MTok
        + cw  * cw_p     / _MTok
        + cr  * cr_p     / _MTok
    )


def turn_cost(
    turn: dict,
    *,
    catalog: dict | None = None,
    shared_dir: Path | str | None = None,
) -> float | None:
    """The pod's per-turn cost rule: recorded cost when non-zero, else the
    token-based estimate, else ``None`` ("can't price this turn").

    Every reader — the Usage page, the tiles, the pod rollup, the spend cap,
    the provisioning backstop, the model-economics matrix — goes through this
    one function so their totals can never disagree.
    """
    try:
        recorded = float(turn.get("cost") or 0)
    except (TypeError, ValueError):
        recorded = 0.0
    if recorded:
        return recorded
    return estimate_turn_cost(turn, catalog=catalog, shared_dir=shared_dir)


@dataclass(frozen=True)
class TurnCostTotal:
    """A summed cost that keeps "couldn't price" beside the dollar figure.

    ``usd`` is the total over the turns that COULD be priced. ``unpriced_turns``
    is how many were left out — the number a surface must show next to the
    total so a stranger reads "can't price 41 turns", not a confident number.
    """

    usd: float = 0.0
    priced_turns: int = 0
    unpriced_turns: int = 0
    unpriced_providers: tuple[str, ...] = ()

    @property
    def measurable(self) -> bool:
        """True when every turn in the window could be priced."""
        return self.unpriced_turns == 0

    @property
    def total_turns(self) -> int:
        return self.priced_turns + self.unpriced_turns


def sum_turn_costs(
    turns: Iterable[dict],
    *,
    catalog: dict | None = None,
    shared_dir: Path | str | None = None,
    on_turn: Callable[[dict, float | None], Any] | None = None,
) -> TurnCostTotal:
    """Sum ``turns`` through :func:`turn_cost`, carrying the unpriced count.

    Unpriced turns are counted, never summed as zero (the aggregator half of
    the tri-state contract). ``on_turn(turn, cost)`` is invoked for every turn
    with its resolved cost (``None`` when unpriced) so a caller can accumulate
    its own per-date / per-session buckets in the same pass.
    """
    cat = catalog if catalog is not None else load_pricing_catalog(shared_dir)
    usd = 0.0
    priced = 0
    unpriced = 0
    providers: list[str] = []
    for t in turns:
        cost = turn_cost(t, catalog=cat)
        if on_turn is not None:
            on_turn(t, cost)
        if cost is None:
            unpriced += 1
            p = turn_provider(t)
            if p not in providers:
                providers.append(p)
            continue
        usd += cost
        priced += 1
    return TurnCostTotal(
        usd=usd,
        priced_turns=priced,
        unpriced_turns=unpriced,
        unpriced_providers=tuple(sorted(providers)),
    )


def unpriced_note(unpriced_turns: int, providers: Iterable[str] = ()) -> str:
    """One operator-facing sentence for an unpriced count, or ``""`` for none.

    The single spelling of the phrase, so the tile, the daemon log and the
    Signal body all say the same thing.
    """
    n = int(unpriced_turns or 0)
    if n <= 0:
        return ""
    provs = [p for p in providers if p]
    tail = f" ({', '.join(sorted(set(provs)))})" if provs else ""
    turn_word = "turn" if n == 1 else "turns"
    return f"can't price {n} {turn_word}{tail}"
