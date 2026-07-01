"""
install_cost_estimator.py — Pre-install cost projection for forge installs.

Projects the USD cost of a forge install BEFORE it fires, so the operator
sees an expected range when they confirm. The structural fix for the
2026-06-03 team-bot-c incident, where a legitimate $33.64 first-time install of
a substantial app caught the operator by surprise — the cost was correct,
the visibility was missing.

Why this is a sibling of usage_analytics, not a method on it:

  usage_analytics deals with *historical* turns: tokens that already
  happened, prices that already applied. The estimator runs *before*
  any turns exist — its inputs are the build_spec and the bot context,
  and its output is a token-count projection × the same price table.
  Sharing the price table (_MODEL_PRICING) keeps projection and
  reconciliation comparable; keeping the prediction math here keeps
  usage_analytics focused on the past.

Token model (conservative — over-projecting is the right failure mode,
since an under-projection is the entire bug we're fixing):

  build_spec_tokens   ≈ len(build_spec) / 4
  bot_context_tokens  ≈ openclaw.json size / 4   (8K floor, 64K cap)
  tool_calls          = 10                       (typical install)
  input_per_call      = build_spec + bot_context
  base_input          = input_per_call × tool_calls
  output_tokens       = 50_000                   (generated code/tests/manifest)
  iteration_mult      = 1.0 + 0.6 + 0.4 = 2.0    (build + critique + refine)
  cache_write_tokens  ≈ build_spec + bot_context  (call 1 writes once)
  cache_read_tokens   ≈ (build_spec + bot_context) × (tool_calls - 1) × 0.9

  total_input  = base_input  × iteration_mult
  total_output = output_tokens × iteration_mult

Cost is then computed via the same _MODEL_PRICING table that
usage_analytics._estimate_turn_cost uses.

Returned `low_usd` is 0.5× mid (floor), `high_usd` is 2.0× mid (ceiling).
The 2× ceiling is the same threshold the post-install overrun Signal
uses: actual > high → emit forge_install_cost_overrun.

When pricing is unknown (model not in the table and provider missing),
the estimate returns 0.0 with a ``"estimate_unavailable": true`` flag in
``components`` — callers surface this rather than silently claiming $0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Import the same price table usage_analytics uses so projection and
# reconciliation can never drift. We can't ``from usage_analytics import _MODEL_PRICING``
# safely at module import time (usage_analytics is in the same dir but its
# import path depends on caller), so we do it lazily inside estimate_install_cost.


# Defaults — match forge_engine._DEFAULT_BUILDER_MODEL (which is provider-less
# like "claude-sonnet-4-6"; we normalize to anthropic/ when looking up pricing).
_DEFAULT_BUILDER_MODEL = "anthropic/claude-sonnet-4-6"

# Token model constants — these are starting defaults; the SHAPE of the
# projection (linear in spec size, scales with bot context, responds to
# critique/refine config) is the contract. Absolute dollar values will
# need calibration against real forge install cost_events post-launch.
#
# Anchoring intuition:
#   * The 2026-06-03 team-bot-c incident's single dominant turn cost $33.64 —
#     that turn alone had ~20KB build_spec + heavy context + ~35KB output.
#   * Forge installs make many LLM calls (build → multiple sub-calls;
#     critique → review pass; refine → patch pass). 30+ is realistic.
#   * Cache hit is much lower than chat (tool results accumulate, often
#     invalidating prefix blocks); ~50% is a reasonable mid.
#   * Output is generated across calls — typically 5-10K per code-emitting
#     call × ~10 such calls = 50-100K output tokens for a substantial app.
_DEFAULT_TOOL_CALLS = 20            # build phase alone; critique/refine apply iteration_mult
_DEFAULT_OUTPUT_TOKENS = 100_000    # generated code+tests+manifest across calls
_CHARS_PER_TOKEN = 4
_BOT_CONTEXT_FLOOR_BYTES = 32_000   # 8K tokens
_BOT_CONTEXT_CAP_BYTES = 512_000    # 128K tokens (heavyweight bots with MCP descriptions)
_CACHE_HIT_RATIO = 0.5              # forge invalidates cache faster than chat

# Iteration multipliers — base build (1.0) + critique (~0.6) + refine (~0.4).
# Both critique and refine are optional in forge config; we treat the
# config flags as off/on rather than running them N times (forge typically
# does 1 pass of each).
_BUILD_MULT = 1.0
_CRITIQUE_MULT = 0.6
_REFINE_MULT = 0.4

_MTok = 1_000_000.0


@dataclass(frozen=True)
class InstallCostEstimate:
    """A projected cost band for one forge install on one bot."""

    bot_id: str
    model: str                  # canonical "provider/model" form
    low_usd: float              # 0.5 × mid (conservative floor)
    mid_usd: float              # central estimate
    high_usd: float             # 2.0 × mid (overrun ceiling)
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    components: dict = field(default_factory=dict)


def _normalize_model(model: str | None) -> str:
    """Add ``anthropic/`` prefix when an Anthropic-shaped model has no prefix.

    Mirrors the normalisation usage_analytics._estimate_turn_cost does at
    line 138 — keeps the projection lookup path identical to the
    reconciliation path.
    """
    if not model:
        return _DEFAULT_BUILDER_MODEL
    m = model.strip()
    if "/" in m:
        return m
    # Anthropic model shape detection — claude-* gets anthropic/, otherwise
    # leave bare so the provider fallback can fire on lookup.
    if m.startswith("claude-"):
        return f"anthropic/{m}"
    return m


def _resolve_builder_model(network: dict) -> str:
    """Same resolution order as forge_engine._resolve_forge_models step 1.

    Operator override via ``network.json::forge.builder_model`` takes
    precedence; otherwise the canonical Sonnet 4.6 default. The full
    tier2 / per-bot resolution path forge_engine does isn't replicated
    here — the projection is a forecast, not a commitment; if the
    actual run picks a different model the reconciliation pass catches
    the divergence.
    """
    try:
        forge_cfg = (network or {}).get("forge") or {}
        explicit = forge_cfg.get("builder_model")
        if isinstance(explicit, str) and explicit.strip():
            return _normalize_model(explicit.strip())
    except Exception:
        pass
    return _DEFAULT_BUILDER_MODEL


def _bot_context_bytes(bot_id: str) -> int:
    """Approximate bot context size from openclaw.json on disk.

    Reads the bot's ``.openclaw/openclaw.json`` via the same
    ``evolve_config.bot_home`` resolution the analyzer-side tools use
    (apply.py, app_session_correlator.py, etc.). This honours
    network.json's ``bots[bot].user`` override — important because the
    macOS account name can differ from bot_id (e.g. team_bot_b runs on a
    different macOS account). Falling back to ``/Users/{bot_id}`` would
    silently read the wrong file.

    Returns the floor when the file is unreadable — under-projecting
    context is the more conservative failure mode for projection
    (a small context produces a small estimate; a real install with
    a larger context produces a real cost, which then surfaces as
    the overrun Signal).
    """
    try:
        from evolve_config import bot_home as _bot_home  # type: ignore[import]
    except Exception:
        return _BOT_CONTEXT_FLOOR_BYTES
    try:
        path = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
        if not path.exists():
            return _BOT_CONTEXT_FLOOR_BYTES
        size = path.stat().st_size
    except OSError:
        return _BOT_CONTEXT_FLOOR_BYTES
    return max(_BOT_CONTEXT_FLOOR_BYTES, min(_BOT_CONTEXT_CAP_BYTES, size))


def _iteration_multiplier(network: dict) -> float:
    """How many forge passes will this install do?

    Reads ``network.json::forge.critique_iters`` and ``forge.refine_iters``
    as on/off toggles (default both on). The actual forge engine reads
    a critique_rounds config that controls retry behavior, not pass
    count; the projection uses simple "is critique enabled" semantics
    because the retry loop only fires on test failure (rare for a clean
    install).
    """
    forge_cfg = (network or {}).get("forge") or {}
    critique_on = forge_cfg.get("critique_iters", 1)
    refine_on = forge_cfg.get("refine_iters", 1)
    try:
        critique_on = 1 if int(critique_on) > 0 else 0
    except (TypeError, ValueError):
        critique_on = 1
    try:
        refine_on = 1 if int(refine_on) > 0 else 0
    except (TypeError, ValueError):
        refine_on = 1
    mult = _BUILD_MULT
    if critique_on:
        mult += _CRITIQUE_MULT
    if refine_on:
        mult += _REFINE_MULT
    return mult


def _lookup_pricing(model: str) -> tuple[float, float, float, float] | None:
    """Find (input, output, cache_write, cache_read) USD-per-MTok for ``model``.

    Lazily imports the price table from usage_analytics so a single
    source of truth covers projection (this module) and reconciliation
    (usage_analytics._estimate_turn_cost).
    """
    try:
        from usage_analytics import (  # type: ignore[import]
            _MODEL_PRICING,
            _PROVIDER_PRICING_FALLBACK,
        )
    except Exception:
        return None
    pricing = _MODEL_PRICING.get(model)
    if pricing is None and "/" in model:
        pricing = _PROVIDER_PRICING_FALLBACK.get(model.split("/")[0])
    return pricing


def estimate_install_cost(
    bot_id: str,
    build_spec: str,
    *,
    network: dict | None = None,
    shared_dir: Path | None = None,  # noqa: ARG001 — reserved for future use
    app_kind: str = "install",  # noqa: ARG001 — reserved for future "improvement" tuning
    bot_context_bytes_override: int | None = None,
) -> InstallCostEstimate:
    """Project the USD cost band for a forge install of ``build_spec`` on ``bot_id``.

    Pure function — no network calls, no LLM, no disk writes. Reads
    openclaw.json (best-effort, falls back to floor on EACCES). Returns
    an InstallCostEstimate with low/mid/high and the underlying token
    counts so the UI can render a breakdown ("input X tokens, cached Y
    tokens, generated Z tokens").

    Args:
      bot_id:     the target bot (for openclaw.json sizing)
      build_spec: the spec text that will be sent on each tool call
      network:    network.json dict; only ``forge.*`` is consulted
      shared_dir: reserved; not currently used
      app_kind:   reserved; "install" vs "improvement" — improvements
                  re-use cached context heavily and might want lower
                  multipliers. Left as a hook.
    """
    network = network or {}

    model = _resolve_builder_model(network)
    pricing = _lookup_pricing(model)

    build_spec_bytes = len(build_spec.encode("utf-8")) if build_spec else 0
    if bot_context_bytes_override is not None:
        context_bytes = max(0, int(bot_context_bytes_override))
    else:
        context_bytes = _bot_context_bytes(bot_id)
    iteration_mult = _iteration_multiplier(network)

    build_spec_tokens = max(0, build_spec_bytes // _CHARS_PER_TOKEN)
    context_tokens = max(0, context_bytes // _CHARS_PER_TOKEN)
    input_per_call = build_spec_tokens + context_tokens

    base_input_tokens = input_per_call * _DEFAULT_TOOL_CALLS
    base_output_tokens = _DEFAULT_OUTPUT_TOKENS

    total_input_tokens = int(round(base_input_tokens * iteration_mult))
    total_output_tokens = int(round(base_output_tokens * iteration_mult))

    # Cache model: call 1 writes the context+spec, calls 2..N read most
    # of it back. We approximate by treating cache_write as one (context+spec)
    # block and cache_read as (tool_calls - 1) × (context+spec) × hit_ratio.
    cache_write_tokens = input_per_call
    cache_read_tokens = int(round(
        input_per_call * (_DEFAULT_TOOL_CALLS - 1) * _CACHE_HIT_RATIO * iteration_mult
    ))

    components: dict[str, Any] = {
        "build_spec_bytes": build_spec_bytes,
        "bot_context_bytes": context_bytes,
        "tool_calls": _DEFAULT_TOOL_CALLS,
        "iteration_multiplier": iteration_mult,
        "model_resolved": model,
        "pricing_source": None,  # filled in below
    }

    if pricing is None:
        components["estimate_unavailable"] = True
        return InstallCostEstimate(
            bot_id=bot_id,
            model=model,
            low_usd=0.0,
            mid_usd=0.0,
            high_usd=0.0,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            components=components,
        )

    input_p, output_p, cw_p, cr_p = pricing
    # The input we pay for is the part NOT served from cache — cache reads
    # are billed separately at the cache-read rate. So billable input
    # tokens ≈ total_input_tokens - cache_read_tokens (with a floor at 0).
    billable_input = max(0, total_input_tokens - cache_read_tokens)

    mid = (
        billable_input * input_p / _MTok
        + total_output_tokens * output_p / _MTok
        + cache_write_tokens * cw_p / _MTok
        + cache_read_tokens * cr_p / _MTok
    )

    components["pricing_source"] = "model" if "/" in model else "provider"
    components["billable_input_tokens"] = billable_input

    mid_rounded = round(mid, 4)
    return InstallCostEstimate(
        bot_id=bot_id,
        model=model,
        low_usd=round(0.5 * mid_rounded, 4),
        mid_usd=mid_rounded,
        high_usd=round(2.0 * mid_rounded, 4),
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        components=components,
    )


def estimate_to_dict(est: InstallCostEstimate) -> dict:
    """Serialise an InstallCostEstimate to a plain dict (for JSON responses)."""
    return {
        "bot_id": est.bot_id,
        "model": est.model,
        "low_usd": est.low_usd,
        "mid_usd": est.mid_usd,
        "high_usd": est.high_usd,
        "input_tokens": est.input_tokens,
        "output_tokens": est.output_tokens,
        "cache_write_tokens": est.cache_write_tokens,
        "cache_read_tokens": est.cache_read_tokens,
        "components": dict(est.components),
    }
