"""Model Performance — pod-wide, model-centric performance rollup from spans.

Phase 13 **v2** (performance) of the model-rungs-and-roles arc
(spec: ``docs/spec-model-economics-page-2026-06-13.md`` §"v2 — performance").

The Model Economics page is **cost-centric** (one row per model, ``$/turn``,
share-of-spend, effective ``$/1k`` — assembled by :mod:`model_economics`). This
module is the **performance transpose** of the SAME model identities: per model,
how *well* did it run — effectiveness (struggle / success), reliability (error
rate), and speed (whole-turn latency).

**Different store, different reliability.** Cost reads the evolve-readable turn
records (every turn). Performance reads the cascade **turn-spans**
(``observability.session_rollup.iter_turn_spans``), which are gateway-owned and
EACCES-prone, so coverage is **partial** (~49% of turns carry a span on the live
pod). That asymmetry is the whole reason this is a separate layer with explicit
coverage reporting:

  - **cost** is over ALL turns;
  - **perf** is over the span-covered SUBSET.

The payload + UI must keep that legible — never conflate a perf figure (subset)
with the all-turns cost figure. Each model identity carries a ``coverage_pct``
(span_count ÷ total turns for that identity) and a ``low_coverage`` flag
(< :data:`LOW_COVERAGE_MIN_SPANS` spans) so the UI can badge "insufficient span
data" rather than hide the model (mirrors the cost ``low_confidence`` pattern).

**Identity is shared with the cost layer (#2889).** A span's model identity is
resolved the SAME way ``model_economics`` resolves a cost row's — IDENTICAL
function (``_identity``) and IDENTICAL ``bare_id → provider`` map — so a perf row
ALWAYS joins its cost row even when the gateway logged the model both
provider-qualified (``anthropic/claude-haiku-4-5``) and bare (``claude-haiku-4-5``).
We import the #2889 helpers rather than reimplementing. A span also carries a clean
top-level ``provider`` field, but it is deliberately NOT used for identity: the cost
layer never sees it (it resolves a bare cost key only via the shared map), so
honoring a span-only provider would orphan the perf data onto an identity the cost
row never lands on. Matching cost's resolution exactly is the contract — see
:func:`_span_identity`.

Metrics (per model identity, all over the span-covered subset):
  - **struggle_avg** — mean ``cascade.struggle.score`` (headline effectiveness;
    100% present on spans; lower = smoother). ``None`` when no span carried a
    score.
  - **success_rate** (+ **success_n**) — ``cascade.success`` true ÷ spans where
    ``cascade.success`` is **present** (~60% coverage). The denominator is the
    present-subset, NEVER all spans — reporting it over all spans would understate
    the rate. ``None`` (with ``success_n=0``) when no span carried the field.
  - **error_rate** — spans with non-null ``error_info`` ÷ span_count.
  - **latency_p50_ms / latency_p95_ms** — turn-duration percentiles (end−start).
    This is **whole-turn wall-clock** (incl. tool calls + waits), NOT pure
    inference time — the caller must label it honestly.

Pure read-and-aggregate — no I/O writes. Takes an iterable of spans so the caller
controls the source (``iter_turn_spans`` on the live path, a list in tests).
"""

from __future__ import annotations

from typing import Any, Iterable

# Identity resolution shared with the cost layer (#2889) so a perf row joins its
# cost row. ``_identity`` returns the canonical ``(provider_lower, bare)`` key
# (via ``_split_model_key`` + the data-derived bare→provider map) — the SAME
# function the cost assembler keys rows on. Importing the shared helper (not
# reimplementing) is the contract — see the module docstring.
from model_economics import _identity


# Below this many spans, a model's perf figures are too thin to trust — the UI
# badges "insufficient span data" instead of presenting them as authoritative
# (mirrors the cost layer's 10k-token ``low_confidence`` min-sample gate). The
# model is NEVER hidden; the fields are still emitted, just flagged.
LOW_COVERAGE_MIN_SPANS = 20


def _span_identity(
    model: str | None,
    bare_to_provider: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Canonical ``(provider_lower, bare)`` identity for a span — IDENTICAL to the
    cost-row identity, so a perf row ALWAYS joins its cost row.

    Resolution is delegated wholesale to the shared #2889 :func:`_identity`
    (``_split_model_key`` + the data-derived ``bare_id → provider`` map) — the SAME
    function and the SAME map the cost layer uses. A qualified key
    (``provider/bare``) keeps its provider; a bare key adopts the map's resolution,
    or stays ``("", bare)`` when the map can't resolve it.

    A span ALSO carries a clean top-level ``provider`` field, but it is deliberately
    **NOT** used for identity. The cost layer never sees that field — it resolves a
    bare cost key *only* via the shared map — so honoring a span-only provider would
    key the perf row onto an identity the cost row never lands on, **orphaning** the
    perf data (the cost row would show ``perf=None`` while the figures sat unreachable
    under a different key). Matching cost's resolution exactly is the binding
    contract; the clean provider is informational only (on the live pod every bare
    twin has a qualified sibling that already seeds the map to the same provider, so
    there is nothing to gain and a join to lose)."""
    return _identity(model, bare_to_provider)


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile (numpy "linear"/type-7) over a SORTED list.

    ``pct`` is 0..100. Returns ``None`` for an empty list. For a single value it is
    that value. The rank is ``(pct/100)·(n−1)``; we interpolate between the two
    bracketing ranks so p50 of an even-length list is the midpoint of the two
    central values (a true median), not a biased pick. Caller pre-sorts."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return float(sorted_vals[lo]) + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * frac


def _is_real_bool(v: Any) -> bool:
    """True iff ``v`` is a genuine bool (not an int, not a truthy string).

    ``cascade.success`` is recorded as a JSON boolean; ``isinstance(x, bool)``
    distinguishes a present field (``True``/``False``) from an absent one
    (``None``) so the success_rate denominator counts only spans that actually
    carried the field. ``isinstance(1, int)`` is True but ``isinstance(1, bool)``
    is False — so a stray non-bool never sneaks into the present-subset."""
    return isinstance(v, bool)


def assemble_model_performance(
    spans: Iterable[Any],
    total_turns_by_identity: dict[tuple[str, str], int],
    *,
    total_turns: int | None = None,
    bare_to_provider: dict[str, str] | None = None,
    min_spans: int = LOW_COVERAGE_MIN_SPANS,
) -> dict:
    """Roll cascade turn-spans up into per-model-identity performance figures.

    ``spans`` is an iterable of :class:`observability.opik_client.OpikSpan` (the
    ``producer == "cascade_telemetry"`` turn-spans, e.g. from
    ``observability.session_rollup.iter_turn_spans``). Each span is bucketed onto
    its model identity (:func:`_span_identity`) and contributes its latency,
    struggle, success, and error to that bucket.

    ``total_turns_by_identity`` maps each ``(provider_lower, bare)`` identity → the
    total turns the cost layer counted for that model (from ``usage_analytics``'s
    ``by_model``, grouped by the SAME identity). It is the denominator for the
    per-model ``coverage_pct``; an identity absent from the map (spans but no
    cost-turns — e.g. a model the turn records missed) reports ``coverage_pct =
    None`` (can't divide), never a fabricated value.

    ``total_turns`` (optional) is the authoritative pod-wide turn count for the
    window (``summary["total_turns"]``). When omitted, the pod denominator falls
    back to ``sum(total_turns_by_identity.values())``. Used only for the pod-level
    ``perf_coverage_pct`` header figure.

    ``bare_to_provider`` is the shared #2889 ``bare_id → provider`` map (so a bare
    span with no clean provider field resolves to the same identity as its cost
    row). ``min_spans`` is the ``low_coverage`` threshold.

    Returns::

        {
          "by_identity": {
            "<provider>/<bare>" | "<bare>": {        # key mirrors the cost row's `model`
              "provider": str, "model_id": str, "model": str,
              "span_count": int,
              "latency_p50_ms": float | None,
              "latency_p95_ms": float | None,
              "struggle_avg": float | None,
              "success_rate": float | None,          # over the present subset
              "success_n": int,                      # size of that subset
              "error_rate": float | None,
              "coverage_pct": float | None,          # span_count ÷ total_turns(identity)
              "low_coverage": bool,                  # span_count < min_spans
            }, ...
          },
          "perf_coverage_pct": float | None,         # total_spans ÷ pod total_turns
          "total_spans": int,
          "total_turns": int,
        }

    Only identities with ≥1 span appear in ``by_identity``; a cost model with no
    spans simply has no perf entry (the route sets its row ``perf`` to ``None``).
    Empty ``spans`` → empty ``by_identity`` and ``perf_coverage_pct`` computed
    against whatever pod total is known (0 spans over N turns = 0.0; 0 turns →
    ``None``)."""
    # Accumulate raw per-identity observations in one pass.
    acc: dict[tuple[str, str], dict] = {}
    total_spans = 0
    for sp in spans:
        total_spans += 1
        attrs = getattr(sp, "attributes", None)
        attrs = attrs if isinstance(attrs, dict) else {}
        identity = _span_identity(getattr(sp, "model", None), bare_to_provider)
        bucket = acc.get(identity)
        if bucket is None:
            bucket = {
                "durations_ms": [],
                "struggles": [],
                "success_present": 0,
                "success_true": 0,
                "error_count": 0,
                "span_count": 0,
            }
            acc[identity] = bucket

        bucket["span_count"] += 1

        # Latency — whole-turn wall-clock (end−start), clamped ≥0 by the span,
        # in milliseconds. OpikSpan.duration_seconds() is a pure datetime subtract
        # (can't raise); guard only that the attribute exists, so a non-span stub
        # is skipped rather than swallowed.
        dur = getattr(sp, "duration_seconds", None)
        if callable(dur):
            seconds = dur()
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                bucket["durations_ms"].append(float(seconds) * 1000.0)

        # Struggle — headline effectiveness; 100% present on real spans.
        score = attrs.get("cascade.struggle.score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            bucket["struggles"].append(float(score))

        # Success — denominator is the PRESENT subset, not all spans.
        success = attrs.get("cascade.success")
        if _is_real_bool(success):
            bucket["success_present"] += 1
            if success:
                bucket["success_true"] += 1

        # Error — non-null error_info on the span.
        if _span_is_error(sp):
            bucket["error_count"] += 1

    by_identity: dict[str, dict] = {}
    for identity, bucket in acc.items():
        provider, bare = identity
        durations = sorted(bucket["durations_ms"])
        struggles = bucket["struggles"]
        span_count = bucket["span_count"]
        success_n = bucket["success_present"]

        total_for_identity = int(total_turns_by_identity.get(identity, 0) or 0)
        coverage_pct = (
            round(span_count / total_for_identity, 4) if total_for_identity > 0 else None
        )

        model_key = f"{provider}/{bare}" if provider else bare
        by_identity[model_key] = {
            "provider": provider,
            "model_id": bare,
            "model": model_key,
            "span_count": span_count,
            "latency_p50_ms": _round_or_none(_percentile(durations, 50.0), 1),
            "latency_p95_ms": _round_or_none(_percentile(durations, 95.0), 1),
            "struggle_avg": (
                round(sum(struggles) / len(struggles), 4) if struggles else None
            ),
            "success_rate": (
                round(bucket["success_true"] / success_n, 4) if success_n > 0 else None
            ),
            "success_n": success_n,
            "error_rate": (
                round(bucket["error_count"] / span_count, 4) if span_count > 0 else None
            ),
            "coverage_pct": coverage_pct,
            "low_coverage": span_count < min_spans,
        }

    pod_total_turns = (
        int(total_turns) if total_turns is not None
        else sum(int(v or 0) for v in total_turns_by_identity.values())
    )
    perf_coverage_pct = (
        round(total_spans / pod_total_turns, 4) if pod_total_turns > 0 else None
    )

    return {
        "by_identity": by_identity,
        "perf_coverage_pct": perf_coverage_pct,
        "total_spans": total_spans,
        "total_turns": pod_total_turns,
    }


def _span_is_error(sp: Any) -> bool:
    """True iff the span recorded an error. Prefers ``is_error()`` (``OpikSpan``'s
    is a pure ``bool(error_info)`` — can't raise); falls back to a non-empty
    ``error_info`` so a plain object (test stub) without the method still works."""
    is_error = getattr(sp, "is_error", None)
    if callable(is_error):
        return bool(is_error())
    return bool(getattr(sp, "error_info", None))


def _round_or_none(v: float | None, ndigits: int) -> float | None:
    """``round(v, ndigits)`` but pass ``None`` straight through."""
    return None if v is None else round(v, ndigits)
