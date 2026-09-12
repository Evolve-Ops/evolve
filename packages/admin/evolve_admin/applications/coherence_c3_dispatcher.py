"""Coherence Pass C3 LLM dispatcher.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.

The pure Pass C3 module (`coherence_pass_c3`) exposes the prompts, rate
limit check, parser, and persistence helper but leaves the LLM call
itself to the caller. This module is that caller — it builds the
prompts, dispatches via the provider-agnostic ``infra_llm`` client on
the pod's primary-bot credentials (#3466), parses the response, and
writes `manifest.coherence.last_capability_check`.

## Why the primary bot's key

C3 is a pod-operator-facing check on the manifest (admin-side state),
not an action on the bot's user data. ``feedback_per_bot_inference``
applies to inference *over user data* — manifests are operator-authored
contracts about what an app does, so the operator's own credential is
the right one.

## Trigger semantics

The dispatcher does *not* re-decide whether C3 should run — that's
`coherence_pass_c3.should_run_c3`'s job. Callers pass a trigger string
and the dispatcher invokes `should_run_c3`. If it says no, the
dispatcher returns a skipped result with the reason. This keeps the
gating logic in one place (the pure module) and lets the dispatcher
focus on the LLM call shape.

## Concurrency / cost notes

- ~5k tokens per run (per the spec)
- Synchronous 45s timeout (matches the help endpoint's cap)
- Rate-limited to 1 run per app per day via `is_rate_limited`
- Mutates the manifest in-place and persists via `save_manifest`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .coherence_pass_c3 import (
    C3_SYSTEM_PROMPT,
    CapabilityCheck,
    build_user_prompt,
    is_rate_limited,
    parse_llm_response,
    should_run_c3,
    write_capability_check,
)
from .manifest import (
    ApplicationManifest,
    load_manifest,
    save_manifest,
)

log = logging.getLogger(__name__)


# LLM output cap. C3 is ~5k tokens; 1024 output tokens is plenty for
# the JSON verdict + rationale (capped at ~300 words by the system prompt).
_MAX_OUTPUT_TOKENS = 1024

# Timeout for the completion call. Matches the help endpoint (45s).
_HTTP_TIMEOUT_S = 45


VALID_TRIGGERS = frozenset({
    "charter_change",
    "forge_approval",
    "on_demand",
})


@dataclass
class DispatchResult:
    """Outcome of a C3 dispatch attempt.

    Three terminal states:
      * ``ok=True`` — LLM ran, verdict cached. ``check`` holds it.
      * ``ok=False, skipped=True`` — should_run_c3 / rate-limit returned
        no-go. ``reason`` explains. No LLM call, no manifest write.
      * ``ok=False, skipped=False`` — LLM dispatch failed. ``error``
        holds the failure summary; ``raw_response`` carries the LLM body
        when the failure was a parse error (so operators can see what
        came back).
    """
    ok: bool = False
    skipped: bool = False
    reason: str = ""
    check: CapabilityCheck | None = None
    error: str = ""
    raw_response: str = ""
    model: str = ""
    cost_estimate_usd: float = 0.0

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "ok":      self.ok,
            "skipped": self.skipped,
            "reason":  self.reason,
            "error":   self.error,
            "model":   self.model,
            "cost_estimate_usd": round(self.cost_estimate_usd, 4),
        }
        if self.check is not None:
            out["check"] = self.check.to_dict()
        if self.raw_response:
            out["raw_response"] = self.raw_response[:1000]
        return out


def dispatch_c3(
    *,
    bot_id: str,
    app_id: str,
    trigger: str,
    shared_dir: Path,
    network: dict[str, Any] | None = None,
    before_manifest: dict | None = None,
    now: datetime | None = None,
) -> DispatchResult:
    """Run Pass C3 against the named app's manifest and persist the verdict.

    Loads the manifest, asks ``should_run_c3`` whether to fire, calls the
    LLM if so, parses the response, and writes
    ``manifest.coherence.last_capability_check`` via ``save_manifest``.

    Args:
        bot_id:           bot owning the app
        app_id:           manifest id
        trigger:          one of VALID_TRIGGERS
        shared_dir:       pod's shared directory
        network:          loaded network.json dict (optional — read if None)
        before_manifest:  prior manifest dict for charter-change detection
                          (required only when trigger == "charter_change";
                          the gating logic surfaces a clear reason otherwise)
        now:              clock for rate-limit check (testing hook)

    Returns:
        DispatchResult — see class docstring for the three terminal shapes.
    """
    if trigger not in VALID_TRIGGERS:
        return DispatchResult(
            ok=False, skipped=True,
            reason=f"unknown trigger {trigger!r}",
        )

    manifest = load_manifest(app_id, bot_id, shared_dir)
    if manifest is None:
        return DispatchResult(
            ok=False, skipped=True,
            reason=f"manifest not found: {bot_id}/{app_id}",
        )

    manifest_dict = (
        manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
    )

    decision, decision_reason = should_run_c3(
        before=before_manifest,
        after=manifest_dict,
        trigger=trigger,
        now=now,
    )
    if not decision:
        return DispatchResult(
            ok=False, skipped=True, reason=decision_reason,
        )

    # Belt-and-braces: should_run_c3 already checks is_rate_limited, but
    # we re-check here to keep the LLM call guarded even if a future
    # should_run_c3 change loosens the rate-limit semantics.
    if is_rate_limited(manifest_dict, now=now):
        return DispatchResult(
            ok=False, skipped=True,
            reason="rate-limited (already ran within 24h)",
        )

    if network is None:
        network = _load_network_safely()

    target = _resolve_llm_target(network)
    if target is None:
        return DispatchResult(
            ok=False, skipped=False,
            error="no LLM provider credentialed for the pod's primary bot",
        )
    model_id = target.model

    user_prompt = build_user_prompt(manifest_dict)
    try:
        from infra_llm import complete  # type: ignore

        response_text = complete(
            target,
            prompt=user_prompt,
            system=C3_SYSTEM_PROMPT,
            max_tokens=_MAX_OUTPUT_TOKENS,
            timeout=_HTTP_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("dispatch_c3 LLM call failed for %s/%s", bot_id, app_id)
        return DispatchResult(
            ok=False, skipped=False,
            error=f"LLM call failed: {exc}",
            model=model_id,
        )

    # infra_llm returns text only — estimate tokens (~4 chars/token) for
    # the operator-facing cost figure, same "rough, conservative" spirit
    # as _estimate_cost_usd itself.
    tokens = (len(C3_SYSTEM_PROMPT) + len(user_prompt) + len(response_text)) // 4

    check = parse_llm_response(response_text)
    if check is None:
        return DispatchResult(
            ok=False, skipped=False,
            error="C3 LLM response did not parse to a valid verdict",
            raw_response=response_text,
            model=model_id,
        )

    check.checked_at = (now or datetime.now(timezone.utc)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    check.triggered_by = trigger

    write_capability_check(manifest_dict, check)
    _persist_capability_check(manifest, manifest_dict, shared_dir)

    return DispatchResult(
        ok=True, skipped=False,
        check=check,
        model=model_id,
        cost_estimate_usd=_estimate_cost_usd(model_id, tokens),
    )


# ── Helpers ─────────────────────────────────────────────────────────


def _persist_capability_check(
    manifest: ApplicationManifest,
    manifest_dict: dict,
    shared_dir: Path,
) -> None:
    """Write the mutated capability check back to the manifest file.

    ``write_capability_check`` mutates ``manifest_dict`` (a snapshot).
    The dataclass instance ``manifest`` still has its old coherence
    block, so we update its field before save_manifest. Direct dataclass
    mutation keeps the v7-arc / hydration layer happy: save_manifest
    serialises via ``manifest.to_dict()``.
    """
    coherence = manifest_dict.get("coherence")
    if isinstance(coherence, dict):
        setattr(manifest, "coherence", coherence)
    save_manifest(manifest, shared_dir)


def _load_network_safely() -> dict[str, Any]:
    """Load network.json. Returns {} if unreadable so the caller can
    still try environment-variable fallbacks."""
    try:
        from ..config import load_network
        return load_network()
    except Exception:  # noqa: BLE001
        return {}


def _resolve_llm_target(network: dict[str, Any]):
    """Resolve the (provider, model, key) target for the C3 judgment.

    C3 is a one-shot cheap judgment call, so ``infra_llm`` walks the
    pod's credentialed providers at the fast tier. Returns ``None`` when
    no LLM provider is credentialed — the caller then runs without an
    LLM rather than raising.

    Until 2026-08-21 this first consulted a model pin read from
    ``behavioral_runner._resolve_judge_model``. That module was deleted
    2026-06-08, so the import raised ``ModuleNotFoundError`` on every
    call and the swallowing ``except`` sent every resolution down the
    fast-tier path regardless. The pin's source is gone, so there is
    nothing left to honor and the lookup is removed rather than
    repaired.
    """
    try:
        from infra_llm import resolve_infra_llm  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        return resolve_infra_llm("fast", network=network or None)
    except Exception:  # noqa: BLE001
        return None


def _strip_provider_prefix(model_id: str) -> str:
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


# Per-MTok approximations for the cost estimate — keeping the same
# constants the help endpoint and cost-attribution paths use. C3's
# ~5k-token spec target lands around $0.005 on Haiku, so a couple
# decimals of precision is plenty.
_PER_MTOK_USD = {
    "claude-haiku-4-5":  0.80,
    "claude-sonnet-4-6": 3.00,
    "claude-opus-4-7":  15.00,
}


def _estimate_cost_usd(model_id: str, total_tokens: int) -> float:
    """Rough cost estimate for the operator. Conservative — uses the
    flat per-MTok rate without splitting input/output, which over-
    estimates the cheaper input half by ~4×. That's the side we want to
    err on when surfacing cost to the operator before they take the
    action."""
    bare = _strip_provider_prefix(model_id)
    rate = _PER_MTOK_USD.get(bare, _PER_MTOK_USD["claude-haiku-4-5"])
    return (total_tokens / 1_000_000.0) * rate
