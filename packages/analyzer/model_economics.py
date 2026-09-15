"""Model Economics — pod-wide, model-centric cost leaderboard assembly.

Phase 13 / Addendum 11 of the model-rungs-and-roles arc
(spec: ``internal/spec-model-economics-page-2026-06-13.md``).

The Usage / Cost page is **bot-centric** ("what is each bot spending"). This
module assembles the **transpose** — a **model-centric, pod-normalized** view:
one row per model used across the whole pod, the comparative surface an operator
needs when tuning model choice ("is the model behind Standard overpriced versus
alternatives in its band?", "where is our spend actually going?").

**This is synthesis, not new instrumentation.** Almost every cut already exists
in ``usage_analytics.compute_summary`` (the exact ``by_model`` /
``by_model_by_audience`` payloads the Cost page consumes). This module REUSES
those, joins each model to its **list price** (``model_pricing.lookup_price``)
and **cost band** (``model_cost_bands.resolve_band``), and adds the small
economics enrichment (``$/turn``, share-of-spend, eff-vs-list delta,
bot-count + recency). It never re-loads or re-derives the per-model cost roll-up.

**v1 is COST ONLY.** Performance (latency / success / struggle per model) is
deferred to v2 behind a data-reliability gate (spec §Performance) — that data
lives only on EACCES-fragile cascade spans.

Invariants (spec §Invariants):
  - **Honest units.** ``$/turn`` is per-turn, NOT per-LLM-call (sub-calls fold
    into the parent turn). Effective ``$/1k`` is incl-cache (the #2797 contract)
    — surfaced as ``usd_per_1k_blended``, never relabeled as a clean per-token
    rate. Callers must label accordingly.
  - **Confidence-gated.** Each row passes through ``low_confidence`` (the 10k
    billed-token min-sample gate, owned by ``usage_analytics``) untouched — the
    UI badges it; low-sample rows are marked, never hidden.
  - **No provider literals in logic.** Provider/model identity is carried as
    strings; colors/labels are a presentation concern (the SPA's ``_aiProvider``
    map), not computed here.
  - **Pod-wide only.** All bots pooled (``load_turns(bot_id=None)``).
"""

from __future__ import annotations

from typing import Any

import model_cost_bands
import model_pricing
import primary_bot


# Internal routing artifacts that the gateway logs as a turn ``model`` but which
# are NOT real models — a delivery mirror leg and the catch-all when no model id
# was captured. They are not comparable economics rows (no provider, no price, no
# band) and pollute the leaderboard, the provider facet, and the rollups, so they
# are dropped pod-wide. Kept as a NAMED data constant (a data-quality skip-list),
# matched on the BARE model id case-insensitively — NOT inline literals in a
# branch, per the "no provider literals in logic" invariant.
_SENTINEL_MODEL_IDS: frozenset[str] = frozenset({"delivery-mirror", "unknown"})


def _is_sentinel_model(model_key: str | None) -> bool:
    """True when a ``by_model`` / turn ``model`` key is an internal sentinel.

    Matches the bare model id (the qualified key's tail, suffix dropped) against
    :data:`_SENTINEL_MODEL_IDS`, case-insensitively — so both ``unknown`` and a
    hypothetical ``provider/delivery-mirror`` are caught."""
    _, bare, _ = _split_model_key(str(model_key or ""))
    return bare.strip().lower() in _SENTINEL_MODEL_IDS


def _split_model_key(model_key: str) -> tuple[str, str, str]:
    """Split a ``by_model`` row's ``model`` key into (provider, bare_id, qualified).

    The key is the gateway-reported qualified id (``provider/model_id``),
    optionally carrying a ``:unexpected_billing`` suffix (a distinct billing
    series for the *same* model). The suffix is stripped for pricing/band joins
    but preserved in the qualified id the caller renders. An unqualified key
    (no ``/``) yields an empty provider.
    """
    base = model_key.split(":", 1)[0]  # drop :unexpected_billing for joins
    provider, sep, bare = base.partition("/")
    if not sep:
        return "", base, model_key
    return provider, bare, model_key


def _resolve_providers_for_bare_keys(
    by_model: list[dict],
    configured: list[dict],
    pricing_cache: dict | None,
    listings_cache: dict | None,
) -> dict[str, str]:
    """Build the ``bare model_id → provider`` resolution map, from DATA only.

    The gateway logs the same model both provider-qualified
    (``anthropic/claude-opus-4-8``) and bare (``claude-opus-4-8``). The bare form
    carries no provider, so its identity ``("", bare)`` never merges with the
    qualified twin's ``("anthropic", bare)`` — the model duplicates across rows,
    and the bare twin also misses pricing/band/configured joins (it falls to the
    observed-cost "premium" band and reads "off-catalog"). This map lets the
    assembler resolve a bare key to its owning provider so the twins collapse
    onto one identity and the merged row prices/bands correctly.

    Resolution is **data-driven** — no provider name appears in a branch (the
    three-homes rule). Four sources, lowest→highest authority (a higher source
    overrides a lower one on conflict):

      1. **pricing catalog** — ``provider`` per priced model (litellm-derived).
         Broadest coverage, but an external catalog: a bare id served by two
         providers there is ambiguous.
      2. **listings cache** — provider per listed model id (the same enumerated
         ``{shared_dir}/model-listings.json`` the add-model picker reads).
      3. **configured catalog** — the operator's declared rung models (only the
         qualified ones carry a provider). Pod-local, trustworthy.
      4. **qualified ``by_model`` keys** — the SAME pod's own data: a bare id
         whose qualified twin ``<provider>/<bare>`` is present in this very
         payload resolves to that provider. Strongest signal for THIS bug.

    Within a single source, a bare id that maps to MORE THAN ONE provider is
    **dropped** from that source (ambiguous → no resolution), so two genuinely
    distinct models that happen to share a bare id are never force-merged; a
    higher-authority source can still resolve what a lower one found ambiguous.
    A bare id absent from every source stays unresolved (provider ``""``)."""
    def _unambiguous(pairs: list[tuple[str, str]]) -> dict[str, str]:
        # bare → the SINGLE non-empty provider it maps to across these pairs;
        # a bare id seen under >1 provider is ambiguous and omitted entirely.
        seen: dict[str, set[str]] = {}
        for bare, provider in pairs:
            if bare and provider:
                seen.setdefault(bare, set()).add(provider)
        return {b: next(iter(ps)) for b, ps in seen.items() if len(ps) == 1}

    pricing_pairs = [
        (str(rec.get("model_id") or ""), str(rec.get("provider") or "").lower())
        for rec in ((pricing_cache or {}).get("models") or [])
        if isinstance(rec, dict)
    ]
    listings_pairs = [
        (str(m.get("model_id") or ""), str(prov or "").lower())
        for prov, models in ((listings_cache or {}).get("providers") or {}).items()
        for m in (models or [])
        if isinstance(m, dict)
    ]
    configured_pairs = [
        (str(c.get("model_id") or ""), str(c.get("provider") or "").lower())
        for c in configured
    ]
    twin_pairs = []
    for m in by_model:
        if not isinstance(m, dict):
            continue
        provider, bare, _ = _split_model_key(str(m.get("model") or ""))
        if provider:
            twin_pairs.append((bare, provider.lower()))

    # Layer low→high: each later source overwrites the earlier on a key it
    # resolves unambiguously, leaving lower-source resolutions otherwise intact.
    resolved: dict[str, str] = {}
    resolved.update(_unambiguous(pricing_pairs))
    resolved.update(_unambiguous(listings_pairs))
    resolved.update(_unambiguous(configured_pairs))
    resolved.update(_unambiguous(twin_pairs))
    return resolved


def resolve_bare_to_provider(
    summary: dict,
    *,
    network: dict | None = None,
    pricing_cache: dict | None = None,
    listings_cache: dict | None = None,
    configured: list[dict] | None = None,
) -> dict[str, str]:
    """Public entrypoint for the #2889 ``bare_id → provider`` resolution map.

    Shared so a downstream consumer (e.g. the v2 performance layer
    :mod:`model_performance`) resolves a model's identity the SAME way the cost
    rows do — same function, same inputs → byte-identical resolution, so a perf
    row joins its cost row even when the gateway logged the model both qualified
    and bare. :func:`assemble_model_economics` calls this too, so there is one
    resolution path, not two parallel ones.

    Builds the four data sources :func:`_resolve_providers_for_bare_keys` needs:
    ``by_model`` from ``summary``, and ``configured`` from the pod-effective
    catalog (computed from ``network`` unless the caller passes a pre-built list).
    No provider literals — the resolution is entirely data-driven."""
    by_model = summary.get("by_model") or []
    if configured is None:
        configured = _configured_catalog_models(_pod_catalog(network))
    return _resolve_providers_for_bare_keys(
        by_model, configured, pricing_cache, listings_cache,
    )


def _list_per_1k(rec: dict | None) -> dict[str, float | None]:
    """Advertised (list) $/1k input + output, from a pricing-cache record.

    The pricing cache stores per-TOKEN floats (``input_cost_per_token`` /
    ``output_cost_per_token``); ×1000 gives the per-1k figure that lines up with
    ``usage_analytics``'s observed ``usd_per_1k_*``. Returns ``None`` for either
    leg the catalog doesn't price (cache miss on the model, or a partial record).
    """
    def _per_1k(per_token: Any) -> float | None:
        if not isinstance(per_token, (int, float)) or isinstance(per_token, bool):
            return None
        if per_token <= 0:
            return None
        return round(float(per_token) * 1000.0, 4)

    if not rec:
        return {"list_per_1k_input": None, "list_per_1k_output": None}
    return {
        "list_per_1k_input":  _per_1k(rec.get("input_cost_per_token")),
        "list_per_1k_output": _per_1k(rec.get("output_cost_per_token")),
    }


def _eff_vs_list_delta(
    eff_blended: float | None,
    list_in: float | None,
    list_out: float | None,
) -> float | None:
    """Effective-minus-list delta on the blended $/1k axis, or ``None``.

    The effective figure is incl-cache, blended (input+output). The list figure
    has no blend ratio (we don't know this model's I/O mix at list time), so the
    honest comparator is against the **midpoint** of the model's list input/output
    rates — a model that runs cheaper than its list midpoint (cache savings) reads
    NEGATIVE; one running over (cache-write overhead, retries) reads POSITIVE.
    Returns ``None`` when either side is unknown — never a fabricated zero.
    """
    if not isinstance(eff_blended, (int, float)) or isinstance(eff_blended, bool):
        return None
    legs = [v for v in (list_in, list_out) if isinstance(v, (int, float))]
    if not legs:
        return None
    list_mid = sum(legs) / len(legs)
    if list_mid <= 0:
        return None
    return round(float(eff_blended) - list_mid, 4)


def _audience_human_pct(aud_row: dict | None) -> float | None:
    """Share of a model's turns that are human-initiated, 0..100, or ``None``.

    ``aud_row`` is a ``by_model_by_audience`` entry. ``None`` when the model has
    no audience row (shouldn't happen for a used model, but defensive) or zero
    calls (a configured-but-unused model — no traffic to attribute).
    """
    if not aud_row:
        return None
    total = int(aud_row.get("total_calls") or 0)
    if total <= 0:
        return None
    human = int((aud_row.get("human") or {}).get("calls") or 0)
    return round(100.0 * human / total, 1)


# Canonical role display order — ordering DATA (role ids, not provider/model
# literals), so the role rollup renders fast→standard→power→max regardless
# of dict iteration order. Unknown roles (including the retired ``judge`` in
# historical usage records) trail in name order.
_ROLE_ORDER: dict[str, int] = {
    "fast": 0, "standard": 1, "power": 2, "max": 3,
}


def _identity(
    model_key: str | None,
    bare_to_provider: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Identity key ``(provider_lower, bare_id)`` for a model key.

    Collapses every sub-series of the same model — the ``:unexpected_billing``
    suffix variant and any qualified variant — onto ONE key (the suffix is
    stripped by :func:`_split_model_key`). Provider is lower-cased so the
    configured-catalog join and the by_model merge agree on identity; the bare
    model id stays case-sensitive (model ids are identifiers). Different versions
    stay distinct (``opus-4-8`` ≠ ``opus-4-7``).

    The gateway logs the same model both provider-qualified
    (``anthropic/claude-opus-4-8``) and bare (``claude-opus-4-8``); a bare key
    carries no provider, so without resolution its identity ``("", bare)`` differs
    from the qualified twin's ``("anthropic", bare)`` and the model duplicates
    across rows. When ``bare_to_provider`` is supplied (the data-derived
    bare-id → provider map from :func:`_resolve_providers_for_bare_keys`), an
    unqualified key adopts its resolved provider so the twins collapse onto one
    identity. A bare id absent from the map stays provider ``""`` (a genuine
    unknown — never force-merged)."""
    provider, bare, _ = _split_model_key(str(model_key or ""))
    if not provider and bare_to_provider:
        provider = bare_to_provider.get(bare, "")
    return (provider.lower(), bare)


def _pod_catalog(network: dict | None) -> dict:
    """The pod-effective merged model catalog (code defaults < pod overrides).

    One place to fold ``network.json::models`` over ``DEFAULT_MODEL_CATALOG`` so
    the configured-model enumeration and the role rollup read the SAME catalog
    (one merge, not two). No provider literals — the catalog is DATA."""
    pod_models = (network or {}).get("models") if isinstance(network, dict) else None
    return primary_bot.merge_model_catalog(pod_models, None, include_defaults=True)


def _configured_catalog_models(catalog: dict) -> list[dict]:
    """Flatten the pod-effective model catalog into (provider, bare_id, role/band).

    ``catalog`` is the already-merged pod catalog (:func:`_pod_catalog` — code
    defaults < pod overrides) so a pod that adopted a model surfaces it. We
    enumerate every model in every rung; each entry carries the rung id, its
    ``costClass`` band, and the role(s) that point at the rung (so a
    configured-but-unused model can still show "Standard / medium" placement).

    Provider/model identity stays as strings — no provider literals in logic.
    """
    # rung_id → list of roles that resolve to it (reverse of the roles map).
    roles = catalog.get("roles") or {}
    rung_roles: dict[str, list[str]] = {}
    for role, target in roles.items():
        rung_id = None
        if isinstance(target, str):
            rung_id = target
        elif isinstance(target, dict):
            rung_id = target.get("rung")
        if isinstance(rung_id, str) and rung_id:
            rung_roles.setdefault(rung_id, []).append(role)

    out: list[dict] = []
    for rung in catalog.get("rungs") or []:
        if not isinstance(rung, dict):
            continue
        rung_id = rung.get("id") or ""
        band = rung.get("costClass")
        for model in rung.get("models") or []:
            if not isinstance(model, str) or not model:
                continue
            provider, sep, bare = model.partition("/")
            out.append({
                "provider": provider if sep else "",
                "model_id": bare if sep else model,
                "qualified": model,
                "rung_id": rung_id,
                "band": band,
                "roles": list(rung_roles.get(rung_id, [])),
            })
    return out


def _provider_uncredentialed(provider: str, credentialed: set[str] | None) -> bool:
    """True when ``provider`` holds NO api_key anywhere on the pod, so its models
    cannot run (uncredentialed catalog honesty — spec addendum 2026-06-25).

    ``credentialed`` is the pod-wide set of providers any bot holds a key for
    (``model_discovery.discover_credentialed_providers``, lowercased). Contract:

    - **Fail-open** — ``credentialed is None`` (the set could not be derived, e.g.
      a transient auth-profiles read miss) ⇒ NEVER flag. A None set must behave
      exactly as before this addendum; we never false-flag the whole catalog on a
      read hiccup.
    - **Bare provider ("") is treated as credentialed/unknown** — an unqualified
      catalog id carries no provider and is almost always an Anthropic twin, so it
      is never force-flagged.
    - **No provider literals** — the decision is pure set membership over
      data-derived strings; provider names only ever appear in DISPLAY labels.

    ``credentialed`` is expected pre-lowercased by the caller; we lowercase
    ``provider`` here defensively."""
    if credentialed is None:
        return False
    p = (provider or "").strip().lower()
    if not p:
        return False
    return p not in credentialed


def _group_audience_by_identity(
    by_aud: list[dict],
    bare_to_provider: dict[str, str] | None = None,
) -> dict[tuple[str, str], dict]:
    """Sum ``by_model_by_audience`` legs per model identity.

    The audience rows carry the same ``:unexpected_billing`` suffix split as
    ``by_model``; folding them onto one ``{human, non_human, total_calls}`` entry
    per ``(provider_lower, bare)`` identity lets the merged row's human-% and the
    per-audience re-split read summed legs (calls + cost). ``total_calls`` is
    summed straight from the source rows (it equals ``human+non_human`` calls).

    ``bare_to_provider`` is threaded into :func:`_identity` so an unqualified
    audience key resolves to the SAME identity as its qualified twin (consistent
    with the ``by_model`` merge) — otherwise a bare audience row would land on a
    different identity than its merged row and the human-% would read empty."""
    out: dict[tuple[str, str], dict] = {}
    for row in by_aud:
        if not isinstance(row, dict):
            continue
        identity = _identity(row.get("model"), bare_to_provider)
        agg = out.get(identity)
        if agg is None:
            agg = {
                "human": {"calls": 0, "cost": 0.0},
                "non_human": {"calls": 0, "cost": 0.0},
                "total_calls": 0,
            }
            out[identity] = agg
        for aud in ("human", "non_human"):
            leg = row.get(aud) or {}
            agg[aud]["calls"] += int(leg.get("calls") or 0)
            agg[aud]["cost"] += float(leg.get("cost") or 0.0)
        agg["total_calls"] += int(row.get("total_calls") or 0)
    for agg in out.values():
        for aud in ("human", "non_human"):
            agg[aud]["cost"] = round(agg[aud]["cost"], 6)
    return out


def _bot_model_matrix(
    turns: list[dict] | None,
    bare_to_provider: dict[str, str] | None = None,
) -> tuple[list[dict], dict[tuple[str, str], set[str]]]:
    """Build the per-(bot × model-identity) cost/calls matrix from raw turns.

    Powers the per-bot filter (Bite B consumes the matrix; the endpoint also
    supports a server-side ``?bot=`` re-run) and the accurate ``bot_count`` on
    each merged identity — distinct ``instance`` values folded across all
    sub-series (the ``:unexpected_billing`` series shares the same turn-level
    ``model``, so it folds in naturally here). Cost uses the SAME rule as
    ``usage_analytics.compute_summary`` (recorded cost, else token estimate) so
    the per-bot legs reconcile with the pooled ``by_model`` totals.

    ``bare_to_provider`` resolves an unqualified turn ``model`` to its provider
    (the SAME map the ``by_model`` merge uses) so a bare-keyed turn lands on the
    same identity as its merged row — per-bot legs then reconcile with the pooled
    totals even when the gateway logged some turns bare and some qualified.

    Returns ``(matrix, bots_by_identity)``. ``matrix`` is a list of
    ``{bot_id, provider, model_id, model, cost, calls, cost_per_turn}`` (one per
    bot × identity). ``bots_by_identity`` maps each ``(provider_lower, bare)``
    identity → the set of bots that ran it. ``turns=None`` → empty matrix (the
    caller then falls back to the per-series ``bot_count`` from ``by_model``)."""
    if not turns:
        return [], {}
    from turn_cost import (  # single-source cost rule (audit B6)
        load_pricing_catalog,
        turn_cost as _resolve_turn_cost,
    )

    catalog = load_pricing_catalog()
    agg: dict[tuple[str, str, str], dict] = {}
    bots_by_identity: dict[tuple[str, str], set[str]] = {}
    for t in turns:
        if not isinstance(t, dict):
            continue
        provider, bare, _ = _split_model_key(str(t.get("model") or ""))
        if not provider and bare_to_provider:
            provider = bare_to_provider.get(bare, "")
        identity = (provider.lower(), bare)
        bot_id = t.get("instance") or "?"
        # Audit B6: an unpriced turn still COUNTS as a call, but contributes no
        # dollars — the cell carries ``unpriced`` so cost_per_turn is read
        # against the turns that actually had a price, not silently diluted by
        # turns folded in at $0.
        resolved = _resolve_turn_cost(t, catalog=catalog)
        cost = resolved if resolved is not None else 0.0
        cell_key = (bot_id, provider.lower(), bare)
        cell = agg.get(cell_key)
        if cell is None:
            cell = {
                "bot_id": bot_id,
                "provider": provider,
                "model_id": bare,
                "model": f"{provider}/{bare}" if provider else bare,
                "cost": 0.0,
                "calls": 0,
                "unpriced": 0,
            }
            agg[cell_key] = cell
        cell["cost"] += cost
        cell["calls"] += 1
        if resolved is None:
            cell["unpriced"] += 1
        bots_by_identity.setdefault(identity, set()).add(bot_id)

    matrix: list[dict] = []
    for cell in agg.values():
        calls = cell["calls"]
        priced_calls = calls - cell["unpriced"]
        cell["cost"] = round(cell["cost"], 6)
        # Divide by the PRICED calls: an unpriced turn has no cost to average,
        # and folding it in as a $0 sample halves the rate for a pod whose
        # provider the catalog cannot price (audit B6). ``None`` when nothing
        # in the cell could be priced.
        cell["cost_per_turn"] = (
            round(cell["cost"] / priced_calls, 6) if priced_calls > 0 else None
        )
        matrix.append(cell)
    # Stable order: by bot, then spend desc within a bot.
    matrix.sort(key=lambda c: (c["bot_id"], -(c["cost"] or 0.0)))
    return matrix, bots_by_identity


def _merge_rate_fields(series: list[dict]) -> dict:
    """Recompute the effective-cost rate fields for a merged identity.

    The per-1k figures are RATIOS (spend ÷ token volume) and cannot be summed —
    they must be recomputed from the summed legs. We reuse ``usage_analytics``'s
    single-source rate calc (``_observed_per_1k`` — same incl-cache numerator,
    same 10k-token ``low_confidence`` min-sample gate) so a merged row's
    confidence/threshold stays identical to the per-series rows it folds.

    Token detail (``input_tokens``/``output_tokens``) is present on every real
    ``by_model`` row, so the live path always recomputes. When token detail is
    absent (a single legacy/synthetic row carrying only the precomputed rate) we
    PRESERVE that row's figures; a multi-series merge with no token detail (a
    shape that doesn't occur on the live pod) folds the billed totals and drops
    the un-recomputable rate to ``None`` rather than fabricate one."""
    from usage_analytics import _observed_per_1k, MODEL_COST_MIN_TOKENS

    sum_in = sum(int(r.get("input_tokens") or 0) for r in series)
    sum_out = sum(int(r.get("output_tokens") or 0) for r in series)
    sum_cost = sum(float(r.get("cost") or 0.0) for r in series)
    if sum_in + sum_out > 0:
        return _observed_per_1k(sum_cost, sum_in, sum_out)
    if len(series) == 1:
        r = series[0]
        return {
            "usd_per_1k_blended": r.get("usd_per_1k_blended"),
            "usd_per_1k_input": r.get("usd_per_1k_input"),
            "usd_per_1k_output": r.get("usd_per_1k_output"),
            "billed_tokens": int(r.get("billed_tokens") or 0),
            "low_confidence": bool(r.get("low_confidence")),
        }
    sum_billed = sum(int(r.get("billed_tokens") or 0) for r in series)
    return {
        "usd_per_1k_blended": None,
        "usd_per_1k_input": None,
        "usd_per_1k_output": None,
        "billed_tokens": sum_billed,
        "low_confidence": sum_billed < MODEL_COST_MIN_TOKENS,
    }


def _blended(rows: list[dict]) -> tuple[float, int, float | None, int, int]:
    """``(Σspend, Σturns, blended $/turn, member_count, used_count)`` over rows.

    The blended $/turn is ``Σcost ÷ Σturns`` (never an average of per-row
    ratios — that would over-weight low-volume models). ``None`` when no turns.

    **Low-confidence rows are EXCLUDED from the blend** (always-on, independent
    of any client toggle): a blended average that folds a sub-10k-token sample
    (e.g. a 3-turn model that happens to read $1.25/turn) is simply wrong — it
    distorted the HIGH band below MEDIUM on the live pod. The honest blend is
    over the rows whose ``low_confidence`` is falsy. ``member_count`` counts only
    those confident members; a group with ZERO confident members still emits
    (with ``cost_per_turn=None`` / ``member_count=0``) so the band/role stays
    visible as a signal — its low-sample membership is reported elsewhere
    (the leaderboard), not silently dropped here.

    ``used_count`` is the count of ALL members regardless of confidence — the
    fix for the empty "High" rollup card: a band/role whose members are ALL
    low-volume reads ``member_count=0`` (correct — the blend is confident-only)
    but ``used_count>0`` (it DOES have used models), so the card can render
    "N models · insufficient data" instead of the misleading "0 models · 0
    turns". This is a count only; it never folds low-conf samples into the
    blend (``cost_per_turn`` / ``spend`` / ``turns`` stay confident-only)."""
    confident = [r for r in rows if not r.get("low_confidence")]
    spend = sum(float(r.get("total_cost") or 0.0) for r in confident)
    turns = sum(int(r.get("turns") or 0) for r in confident)
    cpt = round(spend / turns, 6) if turns > 0 else None
    return round(spend, 6), turns, cpt, len(confident), len(rows)


def _band_rollup(rows: list[dict]) -> list[dict]:
    """Blended cost per cost-band over the merged rows (market view).

    One entry per band with ≥1 member, in cost order (low→premium), plus a
    trailing ``band=None`` group when a row is unbanded. ``member_count`` =
    distinct CONFIDENT models in the band (low-confidence rows are excluded
    from the blend by :func:`_blended`; see its docstring). ``used_count`` =
    ALL models in the band incl. low-confidence — so a band whose members are
    all low-volume (``member_count=0``) still reports it HAS used models
    (``used_count>0``); the card renders "insufficient data", not "0 models"."""
    groups: dict[Any, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("band"), []).append(r)
    order = {b: i for i, b in enumerate(model_cost_bands.COST_BANDS)}

    def _sort_key(band: Any) -> tuple:
        return (band is None, order.get(band, len(order)), str(band))

    out: list[dict] = []
    for band in sorted(groups, key=_sort_key):
        members = groups[band]
        spend, turns, cpt, member_count, used_count = _blended(members)
        out.append({
            "band": band,
            "cost_per_turn": cpt,
            "spend": spend,
            "turns": turns,
            "member_count": member_count,
            "used_count": used_count,
        })
    return out


def _role_rollup(
    rows: list[dict],
    pod_catalog: dict,
    credentialed: set[str] | None = None,
) -> list[dict]:
    """Blended cost per ROLE slot over the merged rows (what each slot costs).

    role → rung → models from the merged pod catalog; a row contributes to a
    role when its identity is in that role's rung's model cluster. A model can
    back more than one role (some pods point several roles at one rung) and then
    contributes to each — the role view answers "what does THIS slot cost",
    not a spend partition. ``member_count`` = distinct CONFIDENT USED models in
    the role's rung (low-confidence rows are excluded from the blend by
    :func:`_blended`); a role with no confident traffic reports 0 spend / 0 turns
    / ``None`` $/turn (itself a signal). ``used_count`` = ALL used models in the
    rung incl. low-confidence, so a role backed only by low-volume models still
    reports it HAS usage ("insufficient data", not "0 models"). Roles render in
    canonical order (``_ROLE_ORDER``).

    ``credentialed`` (pod-wide providers holding a key, lowercased) flags a slot
    whose rung members are ALL uncredentialed: ``uncredentialed=True`` so the
    role-slot view reads it as unfilled ("no credentials") rather than a populated
    slot that merely has no traffic yet. Fail-open: ``credentialed is None`` ⇒
    every slot reports ``uncredentialed=False`` (unchanged behavior)."""
    rung_models: dict[str, set[tuple[str, str]]] = {}
    for rung in pod_catalog.get("rungs") or []:
        if not isinstance(rung, dict):
            continue
        rid = rung.get("id") or ""
        keys: set[tuple[str, str]] = set()
        for m in rung.get("models") or []:
            if isinstance(m, str) and m:
                p, sep, bare = m.partition("/")
                keys.add((p.lower(), bare) if sep else ("", m))
        rung_models[rid] = keys

    roles = pod_catalog.get("roles") or {}
    out: list[dict] = []
    for role in sorted(roles, key=lambda r: (_ROLE_ORDER.get(r, len(_ROLE_ORDER)), r)):
        target = roles[role]
        if isinstance(target, str):
            rung_id = target
        elif isinstance(target, dict):
            rung_id = target.get("rung")
        else:
            rung_id = None
        member_keys = rung_models.get(rung_id or "", set())
        members = [
            r for r in rows
            if ((r.get("provider") or "").lower(), r.get("model_id")) in member_keys
        ]
        spend, turns, cpt, member_count, used_count = _blended(members)
        # A slot whose ENTIRE rung is uncredentialed can't be filled — every
        # member model lacks an api_key on the pod. Bare-provider members ("")
        # count as credentialed/unknown (never force a slot uncredentialed on a
        # twin). Fail-open: credentialed None ⇒ always False.
        uncredentialed = bool(member_keys) and credentialed is not None and all(
            _provider_uncredentialed(prov, credentialed) for (prov, _bare) in member_keys
        )
        out.append({
            "role": role,
            "rung_id": rung_id,
            "cost_per_turn": cpt,
            "spend": spend,
            "turns": turns,
            "member_count": member_count,
            "used_count": used_count,
            "uncredentialed": uncredentialed,
        })
    return out


def normalize_audience(audience: str | None) -> str | None:
    """Canonicalize an audience filter value to ``"human"`` | ``"non_human"`` | None.

    The facet is ``all / human / auto`` (spec §5); ``auto`` (and its synonyms)
    maps to the ``non_human`` leg. ``all`` / empty / unknown → ``None`` (no
    filter)."""
    if not audience:
        return None
    a = str(audience).strip().lower()
    if a in ("", "all"):
        return None
    if a == "human":
        return "human"
    if a in ("auto", "non_human", "nonhuman", "non-human", "agent", "automated"):
        return "non_human"
    return None


def _apply_audience_view(row: dict, aud: str) -> dict:
    """Re-cast a merged row to a single audience leg (spec §5).

    The four audience-safe metrics ($/turn, spend, share, turns) are recomputed
    from that audience's ``{calls, cost}`` leg; ``share_of_spend`` is left for
    the caller to recompute over the filtered audience total. The effective-cost
    fields (``usd_per_1k_*``, ``eff_vs_list_delta``, ``billed_tokens``) are
    NULLED — there is no per-audience token volume, so Eff. cost/1k is
    unavailable per-audience (the UI greys it). ``list_per_1k_*`` is a property
    of the model, not the audience, so it is preserved."""
    leg = (row.get("audience") or {}).get(aud) or {"calls": 0, "cost": 0.0}
    calls = int(leg.get("calls") or 0)
    cost = float(leg.get("cost") or 0.0)
    r = dict(row)
    r["turns"] = calls
    r["total_cost"] = round(cost, 6)
    r["cost_per_turn"] = round(cost / calls, 6) if calls > 0 else None
    r["usd_per_1k_blended"] = None
    r["usd_per_1k_input"] = None
    r["usd_per_1k_output"] = None
    r["eff_vs_list_delta"] = None
    r["billed_tokens"] = None
    r["audience_view"] = aud
    return r


def filter_economics(
    payload: dict,
    *,
    network: dict | None = None,
    provider: str | None = None,
    band: str | None = None,
    audience: str | None = None,
) -> dict:
    """Apply the provider / band / audience facet filters to an assembled payload.

    Returns a NEW payload (the input is not mutated) with ``rows`` filtered,
    ``share_of_spend`` + ``total_cost`` + ``total_turns`` recomputed over the
    kept set, and the ``rollups`` re-derived so the summary strip matches what
    is shown. The ``bot`` facet is a LOAD-time concern (the route re-runs
    ``load_turns(bot_id=…)``), not handled here.

    - **provider / band** filter on the per-row fields.
    - **audience** re-casts each row to one audience leg (:func:`_apply_audience_view`)
      and drops models with no traffic in that audience; Eff. cost/1k is nulled.

    The ``bot_model_matrix`` is filtered by provider/band (it carries no audience
    split, so an audience filter leaves its all-audience legs as-is — Bite B
    treats the matrix as audience-agnostic)."""
    rows = [dict(r) for r in (payload.get("rows") or [])]

    prov = None if provider in (None, "", "all") else str(provider).lower()
    bnd = None if band in (None, "", "all") else band
    aud = normalize_audience(audience)

    if prov is not None:
        rows = [r for r in rows if (r.get("provider") or "").lower() == prov]
    if bnd is not None:
        rows = [r for r in rows if r.get("band") == bnd]
    if aud is not None:
        rows = [_apply_audience_view(r, aud) for r in rows]
        rows = [r for r in rows if int(r.get("turns") or 0) > 0]

    total_cost = sum(float(r.get("total_cost") or 0.0) for r in rows)
    total_turns = sum(int(r.get("turns") or 0) for r in rows)
    for r in rows:
        c = float(r.get("total_cost") or 0.0)
        r["share_of_spend"] = round(c / total_cost, 6) if total_cost > 0 else None
    rows.sort(
        key=lambda r: (r["cost_per_turn"] is not None, r["cost_per_turn"] or 0.0),
        reverse=True,
    )

    pod_catalog = _pod_catalog(network)
    matrix = list(payload.get("bot_model_matrix") or [])
    if prov is not None:
        matrix = [m for m in matrix if (m.get("provider") or "").lower() == prov]
    if bnd is not None:
        kept = {((r.get("provider") or "").lower(), r.get("model_id")) for r in rows}
        matrix = [
            m for m in matrix
            if ((m.get("provider") or "").lower(), m.get("model_id")) in kept
        ]

    # Re-apply the same credential filter to the re-derived role rollup so the
    # role-slot view stays credential-honest under a facet filter (None ⇒
    # fail-open). The set rides in the payload as a sorted list (or None).
    cp = payload.get("credentialed_providers")
    cred = set(cp) if cp is not None else None

    out = dict(payload)
    out["rows"] = rows
    out["bot_model_matrix"] = matrix
    out["rollups"] = {
        "by_band": _band_rollup(rows),
        "by_role": _role_rollup(rows, pod_catalog, cred),
    }
    out["total_cost"] = round(total_cost, 6)
    out["total_turns"] = total_turns
    return out


def assemble_model_economics(
    summary: dict,
    *,
    pricing_cache: dict | None,
    catalog: dict[str, Any] | None = None,
    network: dict | None = None,
    turns: list[dict] | None = None,
    listings_cache: dict | None = None,
    credentialed_providers: set[str] | None = None,
) -> dict:
    """Assemble the pod-wide model-economics leaderboard from an existing summary.

    ``summary`` is ``usage_analytics.compute_summary(load_turns(bot_id=None))`` —
    the pod-pooled roll-up. We REUSE its ``by_model`` (cost/turns/tokens/$1k +
    ``low_confidence`` + ``bot_count`` / ``last_used_ts``) and
    ``by_model_by_audience`` (human split); we join pricing + band and compute
    the economics enrichment.

    **v1.5 — one row per model identity.** ``by_model`` keys split a single model
    into multiple sub-series (the ``:unexpected_billing`` suffix variant; any
    qualified variant), which v1 surfaced as duplicate rows each re-deriving its
    own band. We collapse to **one row per ``(provider, bare model_id)``**: sum
    cost/calls/tokens across the sub-series, recompute ``$/turn`` and the
    ``usd_per_1k_*`` rates from the summed legs, and derive **one band from
    pricing** (``resolve_band`` on the identity). ``configured`` /
    ``unexpected_billing`` become booleans on the merged row; the configured
    enumeration is deduped against the merged identity set.

    **Provider-normalized identity.** The gateway logs the same model both
    provider-qualified (``anthropic/claude-opus-4-8``) and bare
    (``claude-opus-4-8``). The bare form carries no provider, so it would NOT
    merge with its qualified twin — duplicating the model across rows and
    dropping the bare twin to the observed-cost "premium" band + "off-catalog"
    tag. Before grouping we resolve each unqualified key's provider from a
    data-derived ``bare_id → provider`` map (:func:`_resolve_providers_for_bare_keys`,
    built from the pricing catalog, listings cache, configured catalog, and the
    qualified ``by_model`` twins themselves — no provider literals) and group by
    the RESOLVED identity, so twins collapse onto one correctly-priced row. The
    same map feeds the audience fold and the ``(bot × model)`` matrix so per-bot
    legs stay consistent. (The upstream cause — the gateway logging the model
    with/without a provider prefix inconsistently — would be better normalized at
    the ``TurnObserver``; this read-layer pass is needed regardless because
    historical turns are already bare.)

    ``pricing_cache`` is ``model_pricing.read_pricing_cache(shared_dir)`` (may be
    ``None`` — every list-price/delta then reads ``None``, an honest "—").
    ``catalog`` overrides the band ANCHOR catalog (defaults to the code ladder).
    ``network`` is the loaded ``network.json`` (its ``models`` layer folds into
    the configured-catalog enumeration + the role rollup). ``turns`` is the raw
    pod-pooled turn list — when supplied it powers the ``(bot × model)`` matrix
    and the accurate cross-series ``bot_count``; omit it to fall back to the
    per-series ``bot_count`` from ``by_model``. ``listings_cache`` is
    ``model_discovery.read_listings_cache(shared_dir)`` (may be ``None``) — one of
    the data sources for the bare-key provider resolution above. ``credentialed_providers``
    is the pod-wide set of providers any bot holds an api_key for
    (``model_discovery.discover_credentialed_providers``); when supplied, a
    configured-but-unused model whose provider is absent from it is flagged
    ``credentialed=False`` / ``status="no_credentials"`` (it can't run), and a
    role slot whose rung is entirely uncredentialed reads ``uncredentialed=True``.
    ``None`` is fail-open — nothing is flagged (a transient auth-profiles read
    miss must never false-flag the whole catalog).

    Returns (everything below ``rows``/``unused``/totals/``has_pricing`` is
    ADDITIVE over v1 — the v1 SPA reads the unchanged row fields)::

        {
          "rows": [ <one per USED model IDENTITY, sorted by $/turn desc> ],
          "unused": [ <configured-but-unused models, volume 0> ],
          "rollups": {"by_band": [...], "by_role": [...]},
          "bot_model_matrix": [ <per (bot × model) cost/calls> ],
          "total_cost": <pod Σcost>,
          "total_turns": <pod turn count>,
          "has_pricing": <bool — pricing cache present & non-empty>,
        }

    Each ``rows`` entry carries (honest-unit labels are the caller's job):
      provider, model_id, model (provider/bare, suffix dropped),
      unexpected_billing (bool — any sub-series drifted to API-key billing),
      cost_per_turn, turns, total_cost, share_of_spend (0..1),
      usd_per_1k_{blended,input,output} (effective, incl-cache, merge-recomputed),
      list_per_1k_{input,output}, eff_vs_list_delta,
      billed_tokens, low_confidence, bot_count, last_used_ts,
      band, band_source, human_pct, configured (bool),
      audience {human:{calls,cost}, non_human:{calls,cost}}  (per-audience re-split).
    """
    # Drop internal routing-artifact identities (delivery-mirror, unknown) pod-
    # wide BEFORE any grouping — they are not real models and must never reach
    # the leaderboard rows, the provider/band facets, the rollups, or the
    # (bot × model) matrix. The skip-list is named DATA (``_SENTINEL_MODEL_IDS``),
    # not an inline literal branch. ``total_cost`` / ``total_turns`` stay the
    # pod-wide totals reported in the header (the sentinel legs are a tiny share
    # and the header is "pod spend", not "Σ leaderboard rows").
    by_model = [
        m for m in (summary.get("by_model") or [])
        if isinstance(m, dict) and not _is_sentinel_model(m.get("model"))
    ]
    by_aud = [
        a for a in (summary.get("by_model_by_audience") or [])
        if isinstance(a, dict) and not _is_sentinel_model(a.get("model"))
    ]
    if turns:
        turns = [
            t for t in turns
            if not (isinstance(t, dict) and _is_sentinel_model(t.get("model")))
        ]
    total_cost = float(summary.get("total_cost") or 0.0)
    total_turns = int(summary.get("total_turns") or 0)

    # Pod-effective merged catalog (one merge): drives both the configured-model
    # enumeration and the role rollup.
    pod_catalog = _pod_catalog(network)
    configured = _configured_catalog_models(pod_catalog)
    configured_by_key: dict[tuple[str, str], dict] = {
        (c["provider"].lower(), c["model_id"]): c for c in configured
    }
    seen_configured: set[tuple[str, str]] = set()

    # Pod-wide credentialed-provider set (uncredentialed-catalog honesty, spec
    # addendum 2026-06-25): lowercased once so every membership test reads the
    # same case. ``None`` (could not be derived) is fail-open — nothing is
    # flagged, behavior is exactly as before this addendum.
    cred_providers = (
        {str(p).strip().lower() for p in credentialed_providers}
        if credentialed_providers is not None else None
    )

    # Resolve unqualified by_model keys to their owning provider (data-derived,
    # no literals) so a model the gateway logged both qualified and bare merges
    # onto one identity. Built once and threaded through every identity grouping
    # below (groups, audience fold, bot×model matrix) so they all agree.
    bare_to_provider = resolve_bare_to_provider(
        summary, configured=configured,
        pricing_cache=pricing_cache, listings_cache=listings_cache,
    )

    # Audience legs + (bot × model) matrix, both folded to the SAME resolved
    # model identity as the groups below.
    aud_by_identity = _group_audience_by_identity(by_aud, bare_to_provider)
    matrix, bots_by_identity = _bot_model_matrix(turns, bare_to_provider)

    # Group by_model sub-series by RESOLVED identity (suffix-stripped, provider
    # filled in for bare keys), preserving the first-seen display provider casing.
    groups: dict[tuple[str, str], dict] = {}
    for m in by_model:
        if not isinstance(m, dict):
            continue
        provider, bare, _ = _split_model_key(str(m.get("model") or ""))
        if not provider:
            provider = bare_to_provider.get(bare, "")
        identity = (provider.lower(), bare)
        g = groups.get(identity)
        if g is None:
            g = {"provider": provider, "model_id": bare, "series": []}
            groups[identity] = g
        g["series"].append(m)

    rows: list[dict] = []
    for identity, g in groups.items():
        provider = g["provider"]
        bare = g["model_id"]
        series = g["series"]

        sum_cost = sum(float(r.get("cost") or 0.0) for r in series)
        sum_calls = sum(int(r.get("calls") or 0) for r in series)
        cost_per_turn = round(sum_cost / sum_calls, 6) if sum_calls > 0 else None
        share = round(sum_cost / total_cost, 6) if total_cost > 0 else None
        rate = _merge_rate_fields(series)

        rec = model_pricing.lookup_price(pricing_cache, provider, bare)
        list_legs = _list_per_1k(rec)
        delta = _eff_vs_list_delta(
            rate["usd_per_1k_blended"],
            list_legs["list_per_1k_input"],
            list_legs["list_per_1k_output"],
        )

        # bot_count: distinct instances across the merged sub-series. The matrix
        # (from turns) is the source of truth when present, FLOORED by the max
        # per-series count so a turn whose identity the matrix keys differently
        # from compute_summary (e.g. a model-less row that summary keys "unknown"
        # but the matrix keys "") can only RAISE the count, never erase a
        # known-nonzero one. With no turns, the per-series count is all we have.
        series_bot_count = max((int(r.get("bot_count") or 0) for r in series), default=0)
        if bots_by_identity:
            bot_count = max(len(bots_by_identity.get(identity, set())), series_bot_count)
        else:
            bot_count = series_bot_count
        last_used_ts = max(
            (r.get("last_used_ts") for r in series if r.get("last_used_ts")),
            default=None,
        )

        aud = aud_by_identity.get(identity)
        audience = {
            "human": (aud or {}).get("human", {"calls": 0, "cost": 0.0}),
            "non_human": (aud or {}).get("non_human", {"calls": 0, "cost": 0.0}),
        }

        conf_key = (provider.lower(), bare)
        is_configured = conf_key in configured_by_key
        if is_configured:
            seen_configured.add(conf_key)

        rows.append({
            "provider": provider,
            "model_id": bare,
            "model": f"{provider}/{bare}" if provider else bare,
            "unexpected_billing": any(
                str(r.get("model") or "").endswith(":unexpected_billing")
                for r in series
            ),
            "cost_per_turn": cost_per_turn,
            "turns": sum_calls,
            "total_cost": round(sum_cost, 6),
            "share_of_spend": share,
            "usd_per_1k_blended": rate["usd_per_1k_blended"],
            "usd_per_1k_input": rate["usd_per_1k_input"],
            "usd_per_1k_output": rate["usd_per_1k_output"],
            "list_per_1k_input": list_legs["list_per_1k_input"],
            "list_per_1k_output": list_legs["list_per_1k_output"],
            "eff_vs_list_delta": delta,
            "billed_tokens": rate["billed_tokens"],
            "low_confidence": rate["low_confidence"],
            "bot_count": bot_count,
            "last_used_ts": last_used_ts,
            "human_pct": _audience_human_pct(aud),
            "configured": is_configured,
            "audience": audience,
        })

    # Band: resolve ONCE per identity, anchored on PRICING (the merged row is the
    # identity, not a per-observed-series split). ``observed=rows`` gives the
    # observed-cost fallback the MERGED rate for an un-priced model.
    for row in rows:
        band_res = model_cost_bands.resolve_band(
            row["provider"], row["model_id"],
            cache=pricing_cache, catalog=catalog, observed=rows,
        )
        row["band"] = band_res.band
        row["band_source"] = band_res.source

    # Headline sort: $/turn desc. Rows with no $/turn (zero turns can't occur in
    # the USED set, but defensive) sort last.
    rows.sort(key=lambda r: (r["cost_per_turn"] is not None, r["cost_per_turn"] or 0.0), reverse=True)

    # Configured-but-unused models — the operator sees the full catalog, not just
    # the used subset (a configured model with zero usage is itself a signal,
    # ties into the credential/dormant work). volume 0 / "no usage yet". Deduped
    # against the merged identity set above.
    unused: list[dict] = []
    for c in configured:
        key = (c["provider"].lower(), c["model_id"])
        if key in seen_configured:
            continue
        seen_configured.add(key)  # dedup catalog (same model in two rungs)
        rec = model_pricing.lookup_price(pricing_cache, c["provider"], c["model_id"])
        list_legs = _list_per_1k(rec)
        band_res = model_cost_bands.resolve_band(
            c["provider"], c["model_id"],
            cache=pricing_cache, catalog=catalog, observed=rows,
        )
        # Uncredentialed-catalog honesty: a configured model whose provider holds
        # no api_key on the pod can't run — it is NOT merely idle. Flag it so the
        # UI relabels ("no credentials") and separates it from the credentialed-
        # but-idle ("no usage yet") rows. Fail-open when cred_providers is None.
        uncred = _provider_uncredentialed(c["provider"], cred_providers)
        unused.append({
            "provider": c["provider"],
            "model_id": c["model_id"],
            "model": c["qualified"],
            "rung_id": c["rung_id"],
            "roles": c["roles"],
            "list_per_1k_input": list_legs["list_per_1k_input"],
            "list_per_1k_output": list_legs["list_per_1k_output"],
            # Placement prefers the rung's declared costClass (the operator
            # configured it there); fall back to the resolved pricing band.
            "band": c["band"] or band_res.band,
            "band_source": band_res.source,
            "turns": 0,
            "total_cost": 0.0,
            "configured": True,
            "credentialed": not uncred,
            "status": "no_credentials" if uncred else "no_usage",
        })
    # Stable order for the unused list: band (cost order) then model id.
    _band_rank = {b: i for i, b in enumerate(model_cost_bands.COST_BANDS)}
    unused.sort(key=lambda r: (_band_rank.get(r["band"], len(_band_rank)), r["model_id"]))

    has_pricing = bool(pricing_cache and (pricing_cache.get("models") or []))

    return {
        "rows": rows,
        "unused": unused,
        # Headline split: configured-but-unused that CAN run (no usage yet) vs.
        # those that CANNOT (no credentials). The JS partitions ``unused`` by the
        # ``credentialed`` flag; this count is the same partition, exposed so the
        # "CONFIGURED, UNUSED" stat stops conflating unusable with idle.
        "unused_uncredentialed": sum(
            1 for u in unused if u.get("credentialed") is False
        ),
        # Echo the derived credentialed set (sorted list, or None when unknown) so
        # filter_economics can re-apply the same role-slot credential filter when
        # it re-derives the rollups. None ⇒ fail-open downstream.
        "credentialed_providers": (
            sorted(cred_providers) if cred_providers is not None else None
        ),
        "rollups": {
            "by_band": _band_rollup(rows),
            "by_role": _role_rollup(rows, pod_catalog, cred_providers),
        },
        "bot_model_matrix": matrix,
        "total_cost": round(total_cost, 6),
        "total_turns": total_turns,
        "has_pricing": has_pricing,
    }
