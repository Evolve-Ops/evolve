"""generators.model_discovery.value_line — pod-grounded value line (Bite 3).

Design: internal/design-recommendation-legibility-2026-06-12.md §"decision-grounding".

The third rung of the recommendation-legibility contract
(cardinality → register → **decision-grounding** → ranking). Bite 1 made the
``model_discovery`` card *digestible* (coalesce + terse title); this bite makes
it *decidable* — it joins the discovered model × this pod's observed tier usage
× model pricing into ONE terse, cited line so the operator can DECIDE whether to
adopt, not just read that a model exists.

Strict **cite-or-don't** (contract property 3, the load-bearing invariant):
a price/savings NUMBER is emitted ONLY when BOTH a real cited price for the
model AND real pod usage to compare it against are present. An UNPRICED model
(e.g. xAI / grok today) honestly says "can't price yet" — NEVER a fabricated
savings %. A fabricated value number is worse than none.

Deterministic, **no LLM**. Data sources:
  - tier usage: ``{shared_dir}/cost/tier-usage/<bot>/<YYYY-MM-DD>.jsonl`` —
    counts-only records ``{ts, tier(role), model, context, bot_id}`` written by
    ``models.record_tier_usage``. Carries *which role ran which model how often*,
    but no tokens/$, so the $ dimension comes from pricing, not from here.
  - pricing / bands: :mod:`model_pricing` + :mod:`model_cost_bands` (REUSED,
    never reimplemented). ``lookup_price`` gives the cited catalog $/token;
    ``resolve_band`` gives the cohort band for the usage comparison.

The value line is a heuristic *decision aid*, not a binding role mapping: the
operator still chooses the role on the adopt card. The cohort it compares
against is the same cost **band** as the discovered model (a high-band model is
compared against what currently serves the pod's high band), with the dominant
role named for legibility.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import model_cost_bands
import model_pricing


# |Δprice| within this fraction of the incumbent reads as "about the same".
_COMPARABLE_FRACTION = 0.10
# Default observation window — "last week".
_DEFAULT_WINDOW_DAYS = 7


@dataclass(frozen=True)
class ValueLine:
    """A computed value line.

    ``terse`` is the card-face one-liner; ``detail`` is the richer drill-down
    markdown (woven into the proposal body). ``priced`` is True iff a real,
    cited price NUMBER is shown — the cite-or-don't witness: when False the line
    is usage-grounded but carries no fabricated price.
    """

    terse: str
    detail: str
    priced: bool


@dataclass(frozen=True)
class _Incumbent:
    """The dominant same-band model the pod actually ran in the window."""

    role: str
    qualified_id: str
    provider: str
    model_id: str
    calls: int


# ── tier-usage reader ─────────────────────────────────────────────────────────

def _split_qualified(qualified: str) -> tuple[str, str]:
    """``"anthropic/claude-opus-4-8"`` -> ``("anthropic", "claude-opus-4-8")``.
    A bare id (no ``/``) yields ``("", id)`` — it won't match a priced row but
    can still band via the family map."""
    if "/" in qualified:
        provider, _, model_id = qualified.partition("/")
        return provider, model_id
    return "", qualified


def _parse_ts(raw: object) -> datetime | None:
    """Parse a tier-usage ``ts`` (``%Y-%m-%dT%H:%M:%SZ``, UTC) → aware datetime,
    or None when missing/unparseable (the record is then skipped — fail-open)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def read_tier_usage(
    shared_dir: Path, *, now: datetime, window_days: int = _DEFAULT_WINDOW_DAYS,
) -> Counter:
    """Aggregate ``(role, qualified_model) -> call count`` over the last
    ``window_days`` across EVERY bot's tier-usage JSONL.

    Iterates all ``cost/tier-usage/<bot>/`` dirs (like the analytics route does)
    so it needs no bot list and is robust to bot-id/user spelling. Files are
    named by the *local* date but each record's ``ts`` is UTC, so we read a
    one-day-buffered set of date files and filter on the record ``ts`` — precise
    at the window boundary, not skewed by the local/UTC filename mismatch.

    Fail-open at every level: a missing dir, unreadable file, bad JSON line, or
    unparseable ``ts`` is skipped, never raised. A partial usage read yields a
    weaker (but still honest) value line; a hard failure would yield none.
    """
    root = Path(shared_dir) / "cost" / "tier-usage"
    counts: Counter = Counter()
    if not root.is_dir():
        return counts

    cutoff = now - timedelta(days=window_days)
    # Candidate filenames: one day of slack on each side of the window so a
    # file named in local time still gets opened; the ts filter below is what
    # actually bounds the records.
    candidate_dates = {
        (now - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(-1, window_days + 1)
    }

    for bot_dir in root.iterdir():
        if not bot_dir.is_dir():
            continue
        for date in candidate_dates:
            f = bot_dir / f"{date}.jsonl"
            if not f.is_file():
                continue
            try:
                lines = f.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                role = str(rec.get("tier") or "").strip()
                model = str(rec.get("model") or "").strip()
                if role and model:
                    counts[(role, model)] += 1
    return counts


# ── pricing helpers (cite-or-don't gate) ──────────────────────────────────────

def _catalog_input_price(
    cache: dict | None, provider: str, model_id: str,
) -> float | None:
    """The model's **cited** input price ($/token) from the pricing catalog, or
    None when the catalog doesn't price it. This — not the cost band — is the
    cite-or-don't gate for a price number: a family-map/heuristic band is NOT a
    price and never drives a savings %."""
    rec = model_pricing.lookup_price(cache, provider, model_id)
    if not rec:
        return None
    price = rec.get("input_cost_per_token")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return float(price) if price > 0 else None


def _dominant_incumbent(
    usage: Counter,
    disc_band: str | None,
    *,
    cache: dict | None,
    catalog: dict | None,
    exclude_qualified: str,
) -> _Incumbent | None:
    """The most-used ``(role, model)`` the pod ran in the SAME band as the
    discovered model. None when the band is unknown or nothing in that band ran.

    Bands are resolved once per distinct model (memoized) via the shared
    ``model_cost_bands.resolve_band`` — the discovered model and the incumbents
    ride the identical anchor-relative scale, so "same band" means the same
    thing on both sides."""
    if disc_band is None:
        return None
    band_of: dict[str, str | None] = {}
    agg: Counter = Counter()
    for (role, model), calls in usage.items():
        if model == exclude_qualified:
            continue
        if model not in band_of:
            provider, model_id = _split_qualified(model)
            if provider or model_id:
                res = model_cost_bands.resolve_band(
                    provider, model_id, cache=cache, catalog=catalog,
                )
                band_of[model] = res.band
            else:
                band_of[model] = None
        if band_of[model] == disc_band:
            agg[(role, model)] += calls
    if not agg:
        return None
    (role, model), calls = agg.most_common(1)[0]
    provider, model_id = _split_qualified(model)
    return _Incumbent(
        role=role, qualified_id=model, provider=provider,
        model_id=model_id, calls=calls,
    )


def _fmt_mtok(price_per_token: float) -> str:
    """Format a $/token price as a $/MTok string (the unit operators read)."""
    per_m = price_per_token * 1e6
    return f"${per_m:.2f}/MTok"


def _comparison(disc_price: float, inc_price: float) -> str:
    """Cheaper / pricier / same-price phrase for a discovered-vs-incumbent
    input-price delta, with a ±10% "about the same" dead-band."""
    delta = (disc_price - inc_price) / inc_price
    if delta <= -_COMPARABLE_FRACTION:
        return f"~{abs(delta) * 100:.0f}% cheaper"
    if delta >= _COMPARABLE_FRACTION:
        return f"~{delta * 100:.0f}% pricier"
    return "about the same price"


# ── public entrypoint ─────────────────────────────────────────────────────────

def compute_value_line(
    provider: str,
    model_id: str,
    *,
    shared_dir: Path,
    pricing_cache: dict | None = None,
    catalog: dict | None = None,
    provider_display: str | None = None,
    now: datetime | None = None,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> ValueLine | None:
    """Compute the pod-grounded value line for a discovered model, or None.

    Returns None when there is nothing to ground honestly (no pod usage AND no
    way to cite a price) — the caller omits the line entirely rather than show a
    bare, ungrounded claim.

    Decision tree (cite-or-don't throughout):

      1. priced model + a same-band incumbent the pod ran + that incumbent is
         also priced → the full **cited price delta** (``priced=True``).
      2. a same-band incumbent ran, but the model (or the incumbent) isn't in a
         pricing catalog → **usage-grounded, "can't price yet"** (``priced=False``).
      3. the model is priced but the pod runs nothing in its band (yet has other
         usage) → **cite the price**, note there's no current spend to compare
         (``priced=True``).
      4. otherwise → None.
    """
    now = now or datetime.now(timezone.utc)
    disp = provider_display or provider
    cache = (
        pricing_cache if pricing_cache is not None
        else model_pricing.read_pricing_cache(Path(shared_dir))
    )

    usage = read_tier_usage(Path(shared_dir), now=now, window_days=window_days)
    total_calls = sum(usage.values())

    disc_qualified = f"{provider}/{model_id}"
    # Band for the comparison cohort (may come from the family map for an
    # unpriced frontier model — that's fine, the band only picks the cohort);
    # the price for the cite-or-don't number must be a real catalog hit.
    band_res = model_cost_bands.resolve_band(
        provider, model_id, cache=cache, catalog=catalog,
    )
    disc_band = band_res.band
    disc_price = _catalog_input_price(cache, provider, model_id)

    incumbent = _dominant_incumbent(
        usage, disc_band, cache=cache, catalog=catalog,
        exclude_qualified=disc_qualified,
    )

    # ── Case 1: full cited price delta ────────────────────────────────────────
    if disc_price is not None and incumbent is not None:
        inc_price = _catalog_input_price(
            cache, incumbent.provider, incumbent.model_id,
        )
        if inc_price is not None:
            cmp = _comparison(disc_price, inc_price)
            terse = (
                f"Your `{incumbent.role}` role ran {incumbent.calls} "
                f"call{'s' if incumbent.calls != 1 else ''} in the last "
                f"{window_days}d on `{incumbent.model_id}`; this model is "
                f"{cmp} ({_fmt_mtok(disc_price)} vs {_fmt_mtok(inc_price)} "
                f"input)."
            )
            detail = (
                f"**Pod-grounded value** — over the last {window_days} days, "
                f"this pod's `{incumbent.role}` role made {incumbent.calls} "
                f"call{'s' if incumbent.calls != 1 else ''} on "
                f"`{incumbent.qualified_id}` (its current "
                f"`{disc_band}`-band model). At {_fmt_mtok(disc_price)} input, "
                f"`{disc_qualified}` is {cmp} than that incumbent "
                f"({_fmt_mtok(inc_price)} input) — both prices from the cited "
                f"pricing catalog. This is a price comparison on real usage, "
                f"not a forecast; actual savings depend on token volume."
            )
            return ValueLine(terse=terse, detail=detail, priced=True)
        # Incumbent ran but isn't priced — fall through to the honest
        # "can't price yet" line below rather than invent a delta.

    # ── Case 2: usage-grounded but can't put a price on it ────────────────────
    if incumbent is not None:
        if disc_price is None:
            why = f"{disp} doesn't publish pricing we can cite yet"
        else:
            why = (
                f"your current `{incumbent.role}` model "
                f"(`{incumbent.model_id}`) isn't in a pricing catalog we "
                f"can cite"
            )
        terse = (
            f"Your `{incumbent.role}` role ran {incumbent.calls} "
            f"call{'s' if incumbent.calls != 1 else ''} in the last "
            f"{window_days}d on `{incumbent.model_id}` — but {why}, so we "
            f"can't price this against your usage yet."
        )
        detail = (
            f"**Pod-grounded value** — this pod's `{incumbent.role}` role made "
            f"{incumbent.calls} call{'s' if incumbent.calls != 1 else ''} on "
            f"`{incumbent.qualified_id}` (a `{disc_band}`-band model) over the "
            f"last {window_days} days, so a model in this band is genuinely "
            f"used here. But {why} — per cite-or-don't, no savings number is "
            f"shown rather than a fabricated one. Re-check after the pricing "
            f"catalogs list it."
        )
        return ValueLine(terse=terse, detail=detail, priced=False)

    # ── Case 3: priced, but nothing in this band runs here yet ────────────────
    if disc_price is not None and disc_band is not None and total_calls > 0:
        terse = (
            f"Priced at {_fmt_mtok(disc_price)} input, but no bot here ran a "
            f"`{disc_band}`-band model in the last {window_days}d — no current "
            f"spend to compare against."
        )
        detail = (
            f"**Pod-grounded value** — `{disc_qualified}` is priced "
            f"({_fmt_mtok(disc_price)} input, from the cited pricing catalog), "
            f"but across {total_calls} model call{'s' if total_calls != 1 else ''} "
            f"in the last {window_days} days no bot ran a `{disc_band}`-band "
            f"model, so there's no current spend in this band to compare it "
            f"against. Adopt it to open that band, or skip if this pod has no "
            f"`{disc_band}`-band need."
        )
        return ValueLine(terse=terse, detail=detail, priced=True)

    # ── Case 4: no honest grounding to show ───────────────────────────────────
    return None
