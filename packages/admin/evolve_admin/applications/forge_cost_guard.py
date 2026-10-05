"""
forge_cost_guard.py — Pre-dispatch + post-dispatch cost guards for forge
bot dispatches.

The 2026-06-03 incident burned $33.65 in ONE LLM round-trip — a
312-turn runaway agent loop where the final call re-wrote 8.8M cache_write
tokens. The existing ``daily_cap_usd`` breaker (PR #1483) detected this
~3ms after the API returned, but couldn't claw back the cost.

Two complementary guards land here:

* **Guard A — pre-dispatch worst-case projection.** Before spawning the
  ``openclaw agent`` subprocess for a forge dispatch, compute the worst-
  case per-turn and total-dispatch cost from the model's documented
  context window and OC's max-output budget. If either exceeds the
  per-bot cap, refuse the dispatch before any LLM cost is incurred.
  Configurable via
  ``network.json::bots.<bot>.forge.{per_turn_cap_usd, per_dispatch_cap_usd}``.

* **Guard B — hard message-count cap.** OC's ``--max-turns N`` flag,
  wired into every forge dispatch, caps the agent loop at
  ``message_cap`` turns (default 50). When the cap is hit, the agent
  exits without writing an outbox — the dispatch fails with a clear
  signature. Configurable via
  ``network.json::bots.<bot>.forge.message_cap``.

Both guards emit Signals via ``signals.store.observe()`` so the Alerts
page surfaces the refusal / cap hit and the operator can either raise
the cap or investigate the underlying loop. Signal types:

  * ``forge_turn_cost_projected_excessive``  (Guard A)
  * ``forge_session_message_cap_exceeded``   (Guard B)

The module is pure logic and string-shaping — no I/O, no subprocess.
``bot_forge`` calls into it from the dispatch path, and the unit tests
in ``test_forge_cost_guard.py`` exercise it directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional


_log = logging.getLogger(__name__)


PRODUCER = "forge_cost_guard"


# Per-model context window in tokens. Used as the upper-bound for
# worst-case per-turn projection — assume the model could be handed
# its full context window AND re-cache the entire prompt on a single
# turn (the 2026-06-03 incident shape). Source: Anthropic public model docs.
# Update when new variants ship.
MAX_CONTEXT_TOKENS: dict[str, int] = {
    "anthropic/claude-haiku-4-6":        200_000,
    "anthropic/claude-haiku-4-5":        200_000,
    "anthropic/claude-haiku-3-5":        200_000,
    "anthropic/claude-3-haiku":          200_000,
    "anthropic/claude-sonnet-4-6":       200_000,
    "anthropic/claude-sonnet-4-5":       200_000,
    "anthropic/claude-3-5-sonnet":       200_000,
    "anthropic/claude-3-sonnet":         200_000,
    "anthropic/claude-opus-4-6":         200_000,
    "anthropic/claude-opus-4-5":         200_000,
    "anthropic/claude-3-opus":           200_000,
}

DEFAULT_MAX_CONTEXT_TOKENS = 200_000
# OC's agent default — the forge dispatch path doesn't override it.
OPENCLAW_AGENT_MAX_OUTPUT = 8_192

# Defaults applied when ``network.json::bots.<bot>.forge.*`` is unset.
#
# These are conservative ceilings, not targets. Operators set explicit
# per-bot values when they want to allow more expensive forge runs.
#
# Sizing rationale:
#   * message_cap=50 — comfortably above the typical forge dispatch
#     (~10-20 turns for a build + critique + refine on a small app),
#     well below the 2026-06-03 runaway shape (312 turns).
#   * per_turn_cap_usd=5.0 — Sonnet 4.6 worst per-turn under the
#     200K context window is ~$1.47, Opus is ~$7.35. Setting the cap
#     at $5 refuses Opus dispatches by default and forces a deliberate
#     opt-in; Sonnet + Haiku flow through.
#   * per_dispatch_cap_usd=100.0 — Sonnet × 50 turns worst is ~$73.50,
#     which passes; Sonnet × 100 (~$147) does not. Catches gross
#     mis-configurations (a config bump of message_cap to 500 would
#     project ~$735 and refuse). Setting it much lower would refuse
#     legitimate large builds; much higher and the cap loses bite.
DEFAULT_MESSAGE_CAP = 50
DEFAULT_PER_TURN_CAP_USD = 5.0
DEFAULT_PER_DISPATCH_CAP_USD = 100.0

_MTok = 1_000_000.0


# ── Pricing (mirror of usage_analytics._MODEL_PRICING) ────────────────────────
# Duplicated rather than imported because:
#   1. forge_cost_guard runs in the admin daemon; importing analyzer for
#      one constant adds an import-time fragility risk that isn't worth it.
#   2. The two tables drift independently — usage_analytics is descriptive
#      (rollup), forge_cost_guard is prescriptive (refuse a dispatch).
#      A drifted entry here only over- or under-projects; an analyzer
#      drift miscounts spend. Different correctness regimes.
# Format: model_id → (input, output, cache_write, cache_read) USD/MTok.
_PRICING: dict[str, tuple[float, float, float, float]] = {
    "anthropic/claude-haiku-4-6":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-haiku-4-5":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-haiku-3-5":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-3-haiku":            (0.25,   1.25,  0.30, 0.03),
    "anthropic/claude-sonnet-4-6":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-sonnet-4-5":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-3-5-sonnet":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-3-sonnet":           (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-opus-4-6":           (15.00, 75.00, 18.75, 1.50),
    "anthropic/claude-opus-4-5":           (15.00, 75.00, 18.75, 1.50),
    "anthropic/claude-3-opus":             (15.00, 75.00, 18.75, 1.50),
}

# Provider-level fallback when an exact model isn't in the table — use
# Sonnet-class rates so unknown Anthropic ids project as if expensive
# (fail-safe direction: better to over-refuse than to silently allow).
_PROVIDER_FALLBACK: dict[str, tuple[float, float, float, float]] = {
    "anthropic": (3.00, 15.00, 3.75, 0.30),
}


def _pricing_for(model: str | None) -> tuple[float, float, float, float] | None:
    """Resolve (input, output, cache_write, cache_read) USD/MTok for ``model``.

    ``None`` means "bot's agents.defaults.model.primary will be used" — we
    can't see that without reading openclaw.json, so we project against the
    Sonnet-class fallback (the dominant production-bot tier). Returns
    ``None`` only when the model is non-Anthropic — those dispatches go
    through openclaw which already routes via tier configuration, and the
    cost guard is intentionally a no-op there for now.
    """
    if not model:
        return _PROVIDER_FALLBACK["anthropic"]
    exact = _PRICING.get(model)
    if exact is not None:
        return exact
    if "/" in model:
        return _PROVIDER_FALLBACK.get(model.split("/")[0])
    return None


def _max_context_for(model: str | None) -> int:
    if not model:
        return DEFAULT_MAX_CONTEXT_TOKENS
    return MAX_CONTEXT_TOKENS.get(model, DEFAULT_MAX_CONTEXT_TOKENS)


# ── Config loading ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ForgeCaps:
    """Resolved forge cost-guard config for a single bot dispatch.

    Each field's source — explicit ``bots.<bot>.forge.*`` value, pod-wide
    ``forge.defaults.*`` value, or hardcoded default — is reflected by
    the ``*_source`` field so the refusal Signal can name exactly which
    knob the operator should turn to allow the dispatch.
    """
    message_cap: int
    per_turn_cap_usd: float
    per_dispatch_cap_usd: float
    message_cap_source: str
    per_turn_cap_source: str
    per_dispatch_cap_source: str


def _coerce_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def load_caps(bot_id: str, network: dict | None) -> ForgeCaps:
    """Load forge cost-guard caps for ``bot_id`` from ``network.json``.

    Lookup order, first match wins:
      1. ``bots.<bot_id>.forge.<key>``  — explicit per-bot
      2. ``forge.defaults.<key>``       — pod-wide default
      3. hardcoded module default

    Each cap is loaded independently — a bot can override message_cap
    while leaving per_turn_cap_usd at the pod default. Zero / negative
    values are treated as unset (fall through to next source) so an
    operator can't accidentally disable a guard by writing ``0``.
    """
    network = network or {}
    bot_cfg = ((network.get("bots") or {}).get(bot_id) or {}).get("forge") or {}
    defaults_cfg = (network.get("forge") or {}).get("defaults") or {}

    def _resolve_int(key: str, fallback: int) -> tuple[int, str]:
        v = _coerce_int(bot_cfg.get(key))
        if v is not None:
            return v, f"bots.{bot_id}.forge.{key}"
        v = _coerce_int(defaults_cfg.get(key))
        if v is not None:
            return v, f"forge.defaults.{key}"
        return fallback, "default"

    def _resolve_float(key: str, fallback: float) -> tuple[float, str]:
        v = _coerce_float(bot_cfg.get(key))
        if v is not None:
            return v, f"bots.{bot_id}.forge.{key}"
        v = _coerce_float(defaults_cfg.get(key))
        if v is not None:
            return v, f"forge.defaults.{key}"
        return fallback, "default"

    message_cap, mc_src = _resolve_int("message_cap", DEFAULT_MESSAGE_CAP)
    per_turn, pt_src = _resolve_float("per_turn_cap_usd", DEFAULT_PER_TURN_CAP_USD)
    per_dispatch, pd_src = _resolve_float(
        "per_dispatch_cap_usd", DEFAULT_PER_DISPATCH_CAP_USD,
    )
    return ForgeCaps(
        message_cap=message_cap,
        per_turn_cap_usd=per_turn,
        per_dispatch_cap_usd=per_dispatch,
        message_cap_source=mc_src,
        per_turn_cap_source=pt_src,
        per_dispatch_cap_source=pd_src,
    )


# ── Projection ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CostProjection:
    """Worst-case projected cost for a single dispatch.

    ``per_turn_usd`` is the cost ceiling on a single LLM call assuming
    the model receives a fully-loaded context AND re-caches the entire
    prompt on this call (the 2026-06-03 incident shape — turn 312 wrote 8.8M
    cache_write tokens). ``dispatch_total_usd`` extrapolates that to
    ``message_cap`` worst-case turns.
    """
    model: str
    max_context_tokens: int
    max_output_tokens: int
    message_cap: int
    per_turn_usd: float
    dispatch_total_usd: float


def project_worst_case(
    model: str | None,
    *,
    message_cap: int,
    max_output_tokens: int = OPENCLAW_AGENT_MAX_OUTPUT,
) -> Optional[CostProjection]:
    """Worst-case per-turn and total-dispatch cost for ``model``.

    Returns ``None`` when the model isn't priced (non-Anthropic + no
    fallback match) — the caller then skips Guard A for this dispatch.
    The worst-case assumes:

      * input_tokens = model's max context window (entire context sent)
      * cache_write_tokens = same as input (every turn re-caches in full)
      * output_tokens = OC's agent max_tokens (default 8192)

    These are deliberate over-estimates. A cap that survives this
    projection survives any actual turn the model can produce. Operators
    who want a tighter projection (because their prompts never approach
    the model's context window in practice) can simply raise the cap.
    """
    pricing = _pricing_for(model)
    if pricing is None:
        return None
    input_p, output_p, cw_p, _cr_p = pricing
    ctx = _max_context_for(model)
    per_turn = (
        ctx * input_p / _MTok
        + ctx * cw_p / _MTok
        + max_output_tokens * output_p / _MTok
    )
    total = per_turn * message_cap
    return CostProjection(
        model=(model or "anthropic/<unspecified-default>"),
        max_context_tokens=ctx,
        max_output_tokens=max_output_tokens,
        message_cap=message_cap,
        per_turn_usd=per_turn,
        dispatch_total_usd=total,
    )


# ── Pre-dispatch evaluation ───────────────────────────────────────────────────

@dataclass(frozen=True)
class DispatchDecision:
    """Outcome of the pre-dispatch check.

    When ``allowed`` is False, ``reason`` is a one-line human summary
    and ``signal_payload`` is the dict to hand to
    ``signals.store.observe()`` (already shaped with signature, type,
    body, details). When ``allowed`` is True, ``projection`` is still
    populated so callers can log the headroom; the other fields are
    empty/None.
    """
    allowed: bool
    projection: Optional[CostProjection]
    caps: ForgeCaps
    reason: str = ""
    failed_check: str = ""
    signal_payload: Optional[dict] = None


def evaluate_pre_dispatch(
    *,
    bot_id: str,
    model: str | None,
    network: dict | None,
    job_id: str,
    kind: str,
    max_output_tokens: int = OPENCLAW_AGENT_MAX_OUTPUT,
) -> DispatchDecision:
    """Check Guard A against a forge dispatch.

    Returns a DispatchDecision. If ``allowed=False``, the caller MUST
    abort the dispatch before spawning ``openclaw agent`` and emit the
    Signal in ``signal_payload``. If ``allowed=True``, the caller
    proceeds normally — the projection is informational.

    The check is intentionally a wall, not a guideline: a projection
    above ``per_turn_cap_usd`` means a SINGLE API call in this dispatch
    could legally bill more than the cap. Refusing pre-dispatch is the
    only way to make the $33.65-in-one-turn shape impossible by
    construction.
    """
    caps = load_caps(bot_id, network)
    projection = project_worst_case(
        model, message_cap=caps.message_cap, max_output_tokens=max_output_tokens,
    )

    if projection is None:
        return DispatchDecision(allowed=True, projection=None, caps=caps)

    if projection.per_turn_usd > caps.per_turn_cap_usd:
        return _refuse(
            bot_id=bot_id,
            projection=projection,
            caps=caps,
            failed_check="per_turn",
            job_id=job_id,
            kind=kind,
        )
    if projection.dispatch_total_usd > caps.per_dispatch_cap_usd:
        return _refuse(
            bot_id=bot_id,
            projection=projection,
            caps=caps,
            failed_check="per_dispatch",
            job_id=job_id,
            kind=kind,
        )

    return DispatchDecision(allowed=True, projection=projection, caps=caps)


def _refuse(
    *,
    bot_id: str,
    projection: CostProjection,
    caps: ForgeCaps,
    failed_check: str,
    job_id: str,
    kind: str,
) -> DispatchDecision:
    if failed_check == "per_turn":
        cap = caps.per_turn_cap_usd
        cap_src = caps.per_turn_cap_source
        actual = projection.per_turn_usd
        scope_label = "per-turn"
        knob = f"`bots.{bot_id}.forge.per_turn_cap_usd` in network.json"
    else:
        cap = caps.per_dispatch_cap_usd
        cap_src = caps.per_dispatch_cap_source
        actual = projection.dispatch_total_usd
        scope_label = "per-dispatch"
        knob = f"`bots.{bot_id}.forge.per_dispatch_cap_usd` in network.json"

    reason = (
        f"Forge {scope_label} worst-case ${actual:.2f} exceeds cap "
        f"${cap:.2f} ({cap_src}) for {projection.model} "
        f"(context={projection.max_context_tokens:,} tok, "
        f"output={projection.max_output_tokens:,} tok, "
        f"message_cap={projection.message_cap})"
    )
    payload = build_refusal_signal(
        bot_id=bot_id,
        projection=projection,
        caps=caps,
        failed_check=failed_check,
        actual_usd=actual,
        cap_usd=cap,
        cap_source=cap_src,
        knob=knob,
        job_id=job_id,
        kind=kind,
    )
    return DispatchDecision(
        allowed=False,
        projection=projection,
        caps=caps,
        reason=reason,
        failed_check=failed_check,
        signal_payload=payload,
    )


# ── Signal payloads ───────────────────────────────────────────────────────────

def _make_signature(producer: str, sig_type: str, scope: str) -> str:
    """Lazy import of the installed evolve-analyzer package.

    Mirrors the trampoline used elsewhere in evolve_admin (reporter.py,
    health.py, host_health.py). Falls back to a deterministic local
    signature if analyzer isn't importable — the signal still has stable
    dedup keys in that environment.
    """
    try:
        import importlib
        return importlib.import_module("schema.signal").make_signature(
            producer, sig_type, scope,
        )
    except Exception:
        return f"{producer}:{sig_type}:{scope}"


def build_refusal_signal(
    *,
    bot_id: str,
    projection: CostProjection,
    caps: ForgeCaps,
    failed_check: str,
    actual_usd: float,
    cap_usd: float,
    cap_source: str,
    knob: str,
    job_id: str,
    kind: str,
) -> dict:
    """Signal payload for Guard A — pre-dispatch refusal.

    Shape matches signals.store.observe() — caller passes ``**payload``
    once analyzer is on the path. Signature is per-(bot, failed_check)
    so a bot refused for per_turn AND per_dispatch on different days
    raises two distinct signals (different operator response paths),
    while a re-refusal under the same check on consecutive dispatches
    dedups into one firing.
    """
    title = (
        f"{bot_id}: forge {kind} refused — projected ${actual_usd:.2f} "
        f"> ${cap_usd:.2f} cap"
    )
    body = (
        f"Forge dispatch ({kind}, job {job_id}) refused before LLM dispatch:\n"
        f"  model:        {projection.model}\n"
        f"  message_cap:  {projection.message_cap} ({caps.message_cap_source})\n"
        f"  worst per-turn:  ${projection.per_turn_usd:.4f}\n"
        f"  worst dispatch:  ${projection.dispatch_total_usd:.2f}\n"
        f"  cap ({failed_check}): ${cap_usd:.2f} ({cap_source})\n\n"
        f"To allow this dispatch, raise {knob} or pick a cheaper "
        f"model / smaller context."
    )
    fix_steps = (
        f"1. Open Cost → Bot detail for `{bot_id}` to see recent forge "
        f"spend\n"
        f"2. Decide whether the dispatch should be cheaper (switch model, "
        f"reduce context) or whether the cap is too tight\n"
        f"3. If raising the cap: set {knob} to a value above "
        f"${actual_usd:.2f}\n"
        f"4. If tightening the model: set "
        f"`bots.{bot_id}.tier_assignments.forge` or pod-wide tier3 to a "
        f"cheaper model"
    )
    return {
        "signature": _make_signature(
            PRODUCER, "forge_turn_cost_projected_excessive",
            f"{bot_id}/{failed_check}",
        ),
        "producer": PRODUCER,
        "type": "forge_turn_cost_projected_excessive",
        "flavor": "maintenance",
        "severity": "warn",
        "scope": "bot",
        "bot_id": bot_id,
        "title": title,
        "body": body,
        "details": {
            "bot_id": bot_id,
            "job_id": job_id,
            "kind": kind,
            "model": projection.model,
            "max_context_tokens": projection.max_context_tokens,
            "max_output_tokens": projection.max_output_tokens,
            "message_cap": projection.message_cap,
            "projected_per_turn_usd": round(projection.per_turn_usd, 4),
            "projected_dispatch_total_usd": round(projection.dispatch_total_usd, 4),
            "cap_usd": round(cap_usd, 4),
            "cap_source": cap_source,
            "failed_check": failed_check,
            "vector": "cost",
            "magnitude": 2 if failed_check == "per_turn" else 3,
            "severity_active": True,
            "what_it_means": (
                f"The forge dispatch was refused before any LLM tokens were "
                f"billed. The worst-case math for this model + message_cap "
                f"exceeds the {failed_check} cap, so a single runaway turn "
                f"(or the full run-out of the agent loop) could legally "
                f"bill above the operator's threshold. This is preventive, "
                f"not reactive — no money was spent on this dispatch."
            ),
            "fix_steps": fix_steps,
        },
    }


def build_message_cap_signal(
    *,
    bot_id: str,
    job_id: str,
    kind: str,
    message_cap: int,
    cap_source: str,
    model: str | None,
    agent_exit_code: int,
    stderr_tail: str,
    observed_messages: int | None = None,
) -> dict:
    """Signal payload for Guard B — runaway-watcher killed the dispatch.

    Emitted when the dispatcher's sidecar
    :class:`~.forge_runaway_watcher.ForgeRunawayWatcher` observed
    ``message_cap`` (or more) ``model.completed`` events in the
    dispatch's trajectory log and killed the OC process group.

    ``observed_messages`` is the exact count the watcher saw before
    firing the kill. ``None`` is accepted for backward compatibility
    but in practice every dispatcher call site passes the watcher's
    ``observed_count()``.

    Distinct from the legacy "agent rc=1 without outbox" trigger — that
    fires on any failure shape (legit error in the spec, OC crash,
    network failure), so emitting a cap-hit Signal there produced false
    positives. The new trigger fires only when the watcher's verdict is
    ``cap_exceeded``, which by construction means the cap was the
    cause.
    """
    observed_label = (
        f"{observed_messages}" if observed_messages is not None else "?"
    )
    title = (
        f"{bot_id}: forge {kind} hit message cap "
        f"({observed_label}/{message_cap} turns)"
    )
    body = (
        f"Forge dispatch ({kind}, job {job_id}) on bot {bot_id} was "
        f"killed by the runaway watcher after {observed_label} "
        f"``model.completed`` events were observed in its trajectory "
        f"log — at or past the configured message_cap of "
        f"{message_cap} turns (source: {cap_source}). The cap protects "
        f"against runaway agent loops that re-build context every turn "
        f"(the 2026-06-03 incident shape).\n\n"
        f"Model: {model or '(bot default)'}\n"
        f"Agent exit code: {agent_exit_code}\n"
        f"Stderr tail: {stderr_tail[-300:] if stderr_tail else '(empty)'}"
    )
    fix_steps = (
        f"1. Open the job log under "
        f"`{{shared_dir}}/forge/logs/{job_id}.log` for context\n"
        f"2. If the work is genuinely large: raise "
        f"`bots.{bot_id}.forge.message_cap` in network.json above "
        f"{message_cap}\n"
        f"3. If the loop was runaway: investigate the build spec — "
        f"common causes are recursive file reads, unbounded tool retries, "
        f"or a context that grows by >50KB per turn\n"
        f"4. After resolution, the job can be retried via `forge` admin "
        f"UI; the cap re-applies"
    )
    details = {
        "bot_id": bot_id,
        "job_id": job_id,
        "kind": kind,
        "model": model or "",
        "message_cap": message_cap,
        "message_cap_source": cap_source,
        "agent_exit_code": agent_exit_code,
        "stderr_tail": stderr_tail[-1000:] if stderr_tail else "",
        "vector": "cost",
        "magnitude": 2,
        "severity_active": True,
        "what_it_means": (
            f"The runaway watcher killed the agent at "
            f"{observed_label} turns (cap={message_cap}). This is the "
            f"hard cap that bounds runaway loops by construction — "
            f"distinct from the daily_cap_usd breaker which fires after "
            f"spend, not before. A real runaway is now bounded at "
            f"message_cap × per_turn worst case."
        ),
        "fix_steps": fix_steps,
    }
    if observed_messages is not None:
        details["observed_messages"] = int(observed_messages)
    return {
        "signature": _make_signature(
            PRODUCER, "forge_session_message_cap_exceeded", bot_id,
        ),
        "producer": PRODUCER,
        "type": "forge_session_message_cap_exceeded",
        "flavor": "maintenance",
        "severity": "warn",
        "scope": "bot",
        "bot_id": bot_id,
        "title": title,
        "body": body,
        "details": details,
    }


# ── Signal emission helper ────────────────────────────────────────────────────

def emit_signal(shared_dir: Any, payload: dict) -> bool:
    """Best-effort dispatch of ``payload`` to ``signals.store.observe()``.

    Returns True if the signal was written, False otherwise. Failures
    here MUST NOT propagate — the surrounding refusal / cap-hit logic
    is the load-bearing safety, not the alert. Pattern matches the
    other admin-side signal emitters (reporter.py, host_health.py).
    """
    try:
        import importlib
        signals_store = importlib.import_module("signals.store")
    except Exception as exc:
        _log.debug("forge_cost_guard: signal store unavailable (%s)", exc)
        return False
    try:
        signals_store.observe(shared_dir, **payload)
        return True
    except Exception as exc:
        _log.warning("forge_cost_guard: signal write failed: %s", exc)
        return False
