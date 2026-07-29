"""model_pricing — normalized model-pricing catalog (Phase 11b, Addendum 8 §B).

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §"Addendum 8 §B".

Provider ``/v1/models`` listings carry **identity** (the correctly-spelled
current id) but NO pricing — verified. The tier system needs per-token pricing
to *compute* a model's cost band instead of hand-assigning it (the rot that
produced a fictional model id in Phase 10d). This module is the **data layer**
for that: it fetches, normalizes, caches, and reads back a model-pricing
catalog. It computes NO bands and changes NO existing behavior — a later bite
joins this catalog to the listings and derives the band.

Two community catalogs (both MIT, both mirrorable static JSON) are the source
of truth, because no provider exposes structured pricing:

  - **PRIMARY: LiteLLM** ``model_prices_and_context_window.json`` — a flat dict
    keyed by model id; pricing already in **$/token**
    (``input_cost_per_token`` / ``output_cost_per_token``). Broadest coverage.
    The same logical model appears once per *serving surface*
    (``litellm_provider`` = ``anthropic`` AND ``bedrock`` AND ``vertex_ai`` …);
    we keep the **native** provider row and skip the re-hosts so the catalog
    holds one price per (provider, model).

  - **CROSS-CHECK: models.dev** ``api.json`` — keyed by provider, each provider
    carrying a ``models`` map; pricing in **$/M tokens** (divide by 1e6 to
    normalize). Uniquely carries ``family`` + ``release_date``, which we use to
    **enrich** a LiteLLM record that lacks a family.

Every normalized record is a common shape::

    {provider, model_id, input_cost_per_token, output_cost_per_token,
     context_window | None, family | None, source}

The source URLs and the provider-key normalization table are **config/adapter
DATA**, not literals in routing logic (the no-provider-literals-in-logic rule):
they live in the allow-region below, treated exactly like the per-provider
listing endpoints in ``model_discovery``. Logic downstream consumes the
data-derived records, never a provider name baked into a branch.

Network access happens ONLY on the pod's discovery sweep, via
:func:`fetch_pricing_catalog`, mirrored to ``{shared_dir}/model-pricing.json``
by :func:`write_pricing_cache`. Library logic and tests never hit the network —
normalization runs over already-fetched dicts (fixtures in tests).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)


# ── Source adapters (config DATA — endpoints + key normalization) ─────────────
# provider-literal-allow-begin: pricing-source ADAPTERS (three-homes rule, home
# #2). These are NOT routing/availability logic — they are the wiring that says
# "here is where the LiteLLM/models.dev catalogs live and how their provider
# keys spell the providers we already know." Logic downstream keys off the
# data-derived records, never a provider name in a branch.

_HTTP_TIMEOUT_SECONDS = 15

# Primary catalog: LiteLLM static JSON (flat dict keyed by model id, $/token).
LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
# LiteLLM ships a documentation-template row keyed ``sample_spec`` (the field
# legend, not a real model). Skip it on ingest — adapter data, not logic.
_LITELLM_SENTINEL_KEYS = frozenset({"sample_spec"})
# Cross-check catalog: models.dev (keyed by provider, $/M, carries family).
MODELSDEV_API_URL = "https://models.dev/api.json"

# LiteLLM's ``litellm_provider`` spells a *serving surface*. We keep the native
# provider row and skip the re-hosts (Bedrock / Vertex / Azure / OpenRouter,
# etc.) so each logical model is priced once. This set names the surfaces to
# SKIP — adapter data, not logic. A surface not listed here is treated as native.
_LITELLM_REHOST_SURFACES = frozenset({
    "bedrock",
    "bedrock_converse",
    "vertex_ai",
    "vertex_ai-anthropic_models",
    "vertex_ai-mistral_models",
    "vertex_ai-llama_models",
    "vertex_ai-ai21_models",
    "vertex_ai-chat-models",
    "vertex_ai-language-models",
    "azure",
    "azure_ai",
    "azure_text",
    "openrouter",
    "fireworks_ai",
    "together_ai",
    "deepinfra",
    "anyscale",
    "sagemaker",
    "cloudflare",
})

# Normalize each catalog's provider spelling to the provider names the rest of
# the pod uses (the same bare tokens the listings/auth-profiles use). A
# catalog provider absent here passes through unchanged — the table only
# renames the few that differ. Adapter data, keyed structurally on the catalog,
# never consulted as a branch condition in logic.
_PROVIDER_ALIASES = {
    "vertex_ai-anthropic_models": "anthropic",
    "vertex_ai-language-models": "google",
    "vertex_ai-chat-models": "google",
    "gemini": "google",
    "google-vertex": "google",
    "googleai": "google",
    "google_ai": "google",
    "vertexai": "google",
    "azure": "openai",
    "azure_ai": "openai",
    "x-ai": "xai",
    "google-ai-studio": "google",
}
# provider-literal-allow-end


# ── Normalized record ─────────────────────────────────────────────────────────

@dataclass
class PricedModel:
    """One model's normalized pricing, in $/token, from one catalog.

    The common shape every source collapses to. ``input_cost_per_token`` /
    ``output_cost_per_token`` are always per-token floats (models.dev's $/M is
    divided by 1e6 on ingest). ``family`` is the catalog's family when it
    carries one (models.dev does, LiteLLM does not); ``source`` records which
    catalog the row came from for provenance.
    """

    provider: str
    model_id: str
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    context_window: int | None = None
    family: str | None = None
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


PRICING_CACHE_NAME = "model-pricing.json"
_SOURCE_LITELLM = "litellm"
_SOURCE_MODELSDEV = "models.dev"


def pricing_cache_path(shared_dir: Path) -> Path:
    """Path of the persisted pricing cache. Top-level under ``{shared_dir}``
    (evolve-owned — atomic temp+rename write, no sudo), mirroring
    ``model-listings.json``."""
    return Path(shared_dir) / PRICING_CACHE_NAME


def _canonical_provider(raw_provider: str | None) -> str:
    """Map a catalog's provider spelling to the pod's canonical provider name.

    Pure table lookup over adapter DATA (``_PROVIDER_ALIASES``) — an unaliased
    provider passes through lowercased/trimmed. No provider literal in a branch.
    """
    p = (raw_provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(p, p)


def _as_float(value: Any) -> float | None:
    """Coerce a price field to float, tolerating numeric strings (the raw
    APIs sometimes emit $/token as strings). Non-numeric / missing → None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_litellm(raw: dict) -> list[PricedModel]:
    """Normalize the LiteLLM ``model_prices_and_context_window.json`` dict.

    LiteLLM is a FLAT dict keyed by model id; each value carries
    ``litellm_provider`` and pricing already in **$/token**. The same logical
    model appears once per serving surface — dedup by skipping the re-host
    surfaces (``_LITELLM_REHOST_SURFACES``) and keeping the native provider's
    row. A sentinel key (``sample_spec``) and non-dict / unpriced rows are
    skipped. Returns one ``PricedModel`` per (provider, model) kept.
    """
    out: list[PricedModel] = []
    seen: set[tuple[str, str]] = set()
    for model_id, spec in (raw or {}).items():
        if not isinstance(spec, dict) or model_id in _LITELLM_SENTINEL_KEYS:
            continue
        surface = str(spec.get("litellm_provider") or "").strip().lower()
        if not surface or surface in _LITELLM_REHOST_SURFACES:
            continue
        in_cost = _as_float(spec.get("input_cost_per_token"))
        out_cost = _as_float(spec.get("output_cost_per_token"))
        if in_cost is None and out_cost is None:
            # No usable pricing — nothing to contribute to the cost layer.
            continue
        provider = _canonical_provider(surface)
        # The id in LiteLLM may be qualified ("openai/gpt-...") or bare; keep
        # the bare tail so it matches the listing's bare model_id.
        bare_id = model_id.split("/", 1)[1] if "/" in model_id else model_id
        key = (provider, bare_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(PricedModel(
            provider=provider,
            model_id=bare_id,
            input_cost_per_token=in_cost,
            output_cost_per_token=out_cost,
            context_window=_as_int(
                spec.get("max_input_tokens") or spec.get("max_tokens"),
            ),
            family=None,  # LiteLLM carries no family; enriched from models.dev.
            source=_SOURCE_LITELLM,
        ))
    return out


def normalize_modelsdev(raw: dict) -> list[PricedModel]:
    """Normalize the models.dev ``api.json`` dict.

    models.dev is keyed by PROVIDER, each provider carrying a ``models`` map
    keyed by model id. Pricing is in **$/M tokens** (``cost.input`` /
    ``cost.output``) → divide by 1e6 for $/token. It uniquely carries
    ``family`` (and ``release_date``) per model, which the cross-check uses to
    enrich LiteLLM rows. Returns one ``PricedModel`` per (provider, model).
    """
    out: list[PricedModel] = []
    for raw_provider, pdata in (raw or {}).items():
        if not isinstance(pdata, dict):
            continue
        provider = _canonical_provider(raw_provider)
        models = pdata.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, spec in models.items():
            if not isinstance(spec, dict):
                continue
            raw_cost = spec.get("cost")
            cost: dict = raw_cost if isinstance(raw_cost, dict) else {}
            in_per_m = _as_float(cost.get("input"))
            out_per_m = _as_float(cost.get("output"))
            in_cost = in_per_m / 1e6 if in_per_m is not None else None
            out_cost = out_per_m / 1e6 if out_per_m is not None else None
            raw_limit = spec.get("limit")
            limit: dict = raw_limit if isinstance(raw_limit, dict) else {}
            family = spec.get("family")
            out.append(PricedModel(
                provider=provider,
                model_id=str(model_id),
                input_cost_per_token=in_cost,
                output_cost_per_token=out_cost,
                context_window=_as_int(limit.get("context")),
                family=str(family) if family else None,
                source=_SOURCE_MODELSDEV,
            ))
    return out


def _record_key(provider: str, model_id: str) -> str:
    """Index key for a normalized record — the canonical qualified id."""
    return f"{provider}/{model_id}"


def build_pricing_cache(
    *,
    litellm_raw: dict | None,
    modelsdev_raw: dict | None,
    refreshed_at: str,
    degraded: list[dict[str, str]] | None = None,
) -> dict:
    """Shape the pricing cache document from the two raw catalogs.

    LiteLLM is primary (broadest coverage); models.dev is the cross-check that
    (a) adds models LiteLLM lacks and (b) **enriches** a LiteLLM row's missing
    ``family``. The merge is keyed on (provider, model_id): a LiteLLM row wins
    on pricing, but borrows ``family`` from the models.dev row for the same key
    when LiteLLM has none. A models.dev-only model is added as-is.

    ``degraded`` records sources whose fetch failed — recorded with a reason so
    a stale-but-served cache is never silently "fresh" (the silent-monitor
    lesson, mirroring the listings cache).
    """
    litellm_records = normalize_litellm(litellm_raw or {})
    modelsdev_records = normalize_modelsdev(modelsdev_raw or {})

    md_by_key = {
        _record_key(r.provider, r.model_id): r for r in modelsdev_records
    }

    merged: dict[str, PricedModel] = {}
    for r in litellm_records:
        key = _record_key(r.provider, r.model_id)
        if r.family is None:
            cross = md_by_key.get(key)
            if cross is not None and cross.family:
                r.family = cross.family
        merged[key] = r
    # models.dev-only models (not priced by LiteLLM) are added so the catalog is
    # the union — coverage is the point.
    for key, r in md_by_key.items():
        merged.setdefault(key, r)

    models = [merged[k].to_dict() for k in sorted(merged)]
    return {
        "refreshed_at": refreshed_at,
        "sources": {
            "primary": _SOURCE_LITELLM,
            "cross_check": _SOURCE_MODELSDEV,
        },
        "models": models,
        "degraded": list(degraded or []),
    }


def write_pricing_cache(shared_dir: Path, doc: dict) -> Path:
    """Atomically persist the pricing cache document.

    Temp-file + rename in ``{shared_dir}`` (same filesystem → atomic rename),
    owned by the evolve user — no /tmp staging or sudo (the dir carries the
    evolve ACL). Mirrors ``write_listings_cache``. Returns the cache path.
    """
    path = pricing_cache_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
    os.replace(tmp, path)
    return path


# ── Network fetch (pod sweep ONLY — never in library logic or tests) ──────────

def _http_get_json(url: str) -> dict:
    """Plain stdlib GET → parsed JSON. Raises on HTTP/network/parse error so the
    caller records a degraded reason. No retries (the daily sweep stays cheap;
    a transient failure degrades this run and recovers next run)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def fetch_pricing_catalog(
    *,
    refreshed_at: str,
    fetcher: Callable[[str], dict] | None = None,
) -> dict:
    """Fetch both pricing catalogs over HTTP and build the cache document.

    Runs ONLY on the pod's discovery sweep. Each source is fetched
    independently and degrades gracefully: if one source's fetch raises, it is
    recorded in ``degraded`` (with a reason) and the other source still
    contributes — never a hard failure, never a silent "fresh" on a partial
    fetch. ``fetcher`` is injectable so tests exercise the degrade path with a
    raising stub and CI makes zero live calls.
    """
    get = fetcher or _http_get_json
    degraded: list[dict[str, str]] = []

    litellm_raw: dict | None = None
    try:
        litellm_raw = get(LITELLM_PRICING_URL)
    except Exception as exc:  # noqa: BLE001 — any fetch/parse error degrades.
        degraded.append({"source": _SOURCE_LITELLM, "reason": str(exc)})
        _log.warning("model_pricing: LiteLLM fetch failed: %s", exc)

    modelsdev_raw: dict | None = None
    try:
        modelsdev_raw = get(MODELSDEV_API_URL)
    except Exception as exc:  # noqa: BLE001 — any fetch/parse error degrades.
        degraded.append({"source": _SOURCE_MODELSDEV, "reason": str(exc)})
        _log.warning("model_pricing: models.dev fetch failed: %s", exc)

    return build_pricing_cache(
        litellm_raw=litellm_raw,
        modelsdev_raw=modelsdev_raw,
        refreshed_at=refreshed_at,
        degraded=degraded,
    )


def refresh_pricing_cache(
    shared_dir: Path,
    *,
    refreshed_at: str,
    fetcher: Callable[[str], dict] | None = None,
) -> Path | None:
    """Fetch + cache the pricing catalog, degrading gracefully.

    Called from the discovery sweep alongside ``write_listings_cache``. If BOTH
    sources fail (the new document would hold no models), the existing stale
    cache is KEPT — a degraded fetch must never blow away good data (mirrors the
    listings ``degraded`` handling). A write failure logs and returns ``None``,
    never breaking the sweep. Returns the cache path on a successful write.
    """
    doc = fetch_pricing_catalog(refreshed_at=refreshed_at, fetcher=fetcher)
    both_failed = len(doc.get("degraded") or []) >= 2 and not doc.get("models")
    if both_failed and pricing_cache_path(shared_dir).exists():
        _log.warning(
            "model_pricing: both sources degraded and no models fetched; "
            "keeping the stale cache at %s",
            pricing_cache_path(shared_dir),
        )
        return None
    try:
        return write_pricing_cache(Path(shared_dir), doc)
    except OSError as exc:
        _log.warning(
            "model_pricing: failed to write pricing cache at %s: %s",
            pricing_cache_path(Path(shared_dir)), exc,
        )
        return None


# ── Reader ────────────────────────────────────────────────────────────────────

def read_pricing_cache(shared_dir: Path) -> dict | None:
    """Read the persisted pricing cache, or ``None`` if absent/unreadable.

    A corrupt or missing cache returns ``None`` so the caller can render an
    honest "no pricing yet" state rather than crashing (mirrors
    ``read_listings_cache``).
    """
    path = pricing_cache_path(shared_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def lookup_price(
    cache: dict | None, provider: str, model_id: str,
) -> dict | None:
    """Look up a model's normalized pricing record by (provider, model_id).

    Matches on the canonical (provider, bare-model-id) key. ``model_id`` may be
    passed bare (``claude-opus-4-8``) or qualified (``anthropic/claude-...``);
    the provider prefix is stripped before matching. Returns the cache's record
    dict (the normalized $/token shape) or ``None`` when the catalog has no
    price for that model.
    """
    if not cache:
        return None
    bare = model_id.split("/", 1)[1] if "/" in model_id else model_id
    want = _record_key((provider or "").strip().lower(), bare)
    for rec in cache.get("models") or []:
        if not isinstance(rec, dict):
            continue
        if _record_key(
            str(rec.get("provider") or ""), str(rec.get("model_id") or ""),
        ) == want:
            return rec
    return None
