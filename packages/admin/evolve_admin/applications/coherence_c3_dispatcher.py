"""Coherence Pass C3 LLM dispatcher.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.

The pure Pass C3 module (`coherence_pass_c3`) exposes the prompts, rate
limit check, parser, and persistence helper but leaves the LLM call
itself to the caller. This module is that caller — it builds the
prompts, calls Anthropic via the pod's primary-bot key (mirroring the
behavioral judge in `behavioral_runner`), parses the response, and
writes `manifest.coherence.last_capability_check`.

## Why the primary bot's key

C3 is a pod-operator-facing check on the manifest (admin-side state),
not an action on the bot's user data. ``feedback_per_bot_inference``
applies to inference *over user data* — manifests are operator-authored
contracts about what an app does. Using the primary bot's key matches
``behavioral_runner._resolve_api_key`` (the behavioral judge has the
same operator-facing role).

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

import json
import logging
import urllib.error
import urllib.request
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


# Anthropic call cap. C3 is ~5k tokens; 1024 output tokens is plenty for
# the JSON verdict + rationale (capped at ~300 words by the system prompt).
_MAX_OUTPUT_TOKENS = 1024

# Timeout for the messages call. Matches the help endpoint (45s).
_HTTP_TIMEOUT_S = 45.0

# Fallback model when nothing is resolved through the primary-bot's
# tier configuration. Matches behavioral_runner's hardcoded fallback.
_DEFAULT_MODEL = "anthropic/claude-haiku-4-5"


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

    api_key = _resolve_primary_bot_key(network)
    if not api_key:
        return DispatchResult(
            ok=False, skipped=False,
            error="no Anthropic API key resolvable for the primary bot",
        )

    model_id = _resolve_judge_model(network)
    if "/" in model_id and not model_id.startswith("anthropic/"):
        return DispatchResult(
            ok=False, skipped=False,
            error=(
                f"resolved tier model {model_id!r} is non-Anthropic; C3 "
                "currently only supports Anthropic — set ANTHROPIC_API_KEY "
                "or assign an Anthropic model to the primary bot's tier3"
            ),
            model=model_id,
        )

    user_prompt = build_user_prompt(manifest_dict)
    try:
        response_text, tokens = _call_anthropic(
            system_prompt=C3_SYSTEM_PROMPT,
            user_message=user_prompt,
            model=model_id,
            api_key=api_key,
        )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return DispatchResult(
            ok=False, skipped=False,
            error=f"Anthropic HTTP {exc.code}: {body}",
            model=model_id,
        )
    except TimeoutError as exc:
        return DispatchResult(
            ok=False, skipped=False,
            error=f"LLM call timed out after {_HTTP_TIMEOUT_S}s: {exc}",
            model=model_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("dispatch_c3 LLM call failed for %s/%s", bot_id, app_id)
        return DispatchResult(
            ok=False, skipped=False,
            error=f"LLM call failed: {exc}",
            model=model_id,
        )

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


def _resolve_primary_bot_key(network: dict[str, Any]) -> str:
    """Read the Anthropic key the engine background calls use.

    Mirrors behavioral_runner._resolve_api_key: env override first, then
    primary bot's auth-profiles.json.
    """
    try:
        from primary_bot import read_primary_bot_anthropic_key  # type: ignore
    except Exception:  # noqa: BLE001
        return ""
    try:
        return read_primary_bot_anthropic_key(network or {})
    except Exception:  # noqa: BLE001
        return ""


def _resolve_judge_model(network: dict[str, Any]) -> str:
    """Pick a tier3 (cheap, judgy) model for C3.

    C3 is a one-shot judgment call — same shape as the behavioral judge.
    Reuse the same resolution path so a pod that's configured tier3
    around a specific Anthropic Haiku build gets that build.
    """
    try:
        from .behavioral_runner import _resolve_judge_model as _resolver
    except Exception:  # noqa: BLE001
        return _DEFAULT_MODEL
    try:
        return _resolver(network or {})
    except Exception:  # noqa: BLE001
        return _DEFAULT_MODEL


def _strip_provider_prefix(model_id: str) -> str:
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def _call_anthropic(
    *,
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
) -> tuple[str, int]:
    """POST to api.anthropic.com/v1/messages and return (text, total_tokens).

    Synchronous; raises HTTPError / TimeoutError on transport failure so
    the dispatcher can map to a structured failure result.
    """
    payload = json.dumps({
        "model": _strip_provider_prefix(model),
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        data = json.loads(resp.read())
    text = (data.get("content") or [{}])[0].get("text", "")
    usage = data.get("usage") or {}
    tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    return text, tokens


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
