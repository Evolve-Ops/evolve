"""engine_llm.py — engine-side LLM enrichment calls that fail LOUDLY.

Umbrella: github issue #3466 (provider-agnostic infra) — this is the
analyzer-side companion to :mod:`infra_llm`.

Three analyzer jobs enrich their reports with one background LLM call:
``weekly_review`` (RSI synthesis), ``expansion`` (application-gap
suggestions) and ``community_intel`` (source summary). Until 2026-08-18
each shelled out to ``openclaw llm complete`` — a subcommand that does
not exist in any shipped OpenClaw (2026.7.1-2 answers ``Unknown command:
openclaw llm``). Every call had been failing for months, and each job
swallowed the non-zero exit into the SAME soft "LLM unavailable" path it
uses when the pod simply has no provider key. A permanently-broken call
site was therefore indistinguishable from a correctly-degrading one, so
nobody noticed: weekly_review quietly shipped a data-only report every
Sunday.

This module exists to keep those two conditions apart. It wraps
``infra_llm`` (the pod's provider-agnostic engine client) and reports a
three-way outcome:

  * :data:`UNCREDENTIALED` — no provider is credentialed on the primary
    bot. ``infra_llm.resolve_infra_llm`` returns ``None`` BY DESIGN and
    the caller's data-only path is the correct, expected behaviour. Quiet:
    no Signal. This is the only outcome that may be silent.
  * :data:`FAILED` — a credentialed call was attempted and did not
    produce usable text (HTTP error, transport error, unparseable
    response, or an unexpected exception). The pod HAS a working provider
    and this enrichment is broken. Emits a firing ``engine_llm_call_failed``
    Signal so it lands on the Alerts page.
  * :data:`OK` — text came back. Resolves any ``engine_llm_call_failed``
    Signal previously raised for this job, so a fixed call site clears
    itself without operator action.

Why ``infra_llm`` and not ``openclaw capability model run``
----------------------------------------------------------
OpenClaw 2026.7.1-2 does ship a working one-shot surface
(``openclaw capability model run --model <id> --prompt <text>``, the
``infer`` alias). Porting to it was rejected:

  * These jobs run as the ``evolve`` user, so the CLI would resolve
    ``/Users/evolve/.openclaw`` — a stale profile carrying a legacy
    ``evolve-tiers.json`` that still pins the retired
    ``anthropic/claude-sonnet-4-6``. Re-pointing at it would have made the
    calls succeed against the wrong model with credentials nobody audits.
  * ``infra_llm`` is the designated single replacement for engine-side
    LLM calls (#3466 PR-3). It resolves the model through the POD's tier
    config and the key through the PRIMARY BOT's credentials
    (``primary_bot.read_primary_bot_llm_keys``) — never the ``evolve``
    account's own profile — and never presumes a provider the pod has no
    key for (docs/principle-llm-provider-agnostic.md).
  * It is an in-process urllib call, so it drops the subprocess/PATH/node
    /uid coupling that let this break invisibly in the first place. The
    ``capability model run`` surface also has no ``--max-tokens``.

These three call sites were simply missed by the #3466 PR-3 sweep.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "OK",
    "UNCREDENTIALED",
    "FAILED",
    "PRODUCER",
    "SIGNAL_TYPE",
    "engine_complete",
    "extract_json_object",
    "report_failure",
]

# ── Outcomes ─────────────────────────────────────────────────────────────────
OK = "ok"
UNCREDENTIALED = "uncredentialed"
FAILED = "failed"

# Signal identity. One producer for all three jobs; the ``job`` argument
# scopes the signature so each call site gets its own Signal (and its own
# independent auto-resolve) rather than three jobs fighting over one.
PRODUCER = "engine_llm"
SIGNAL_TYPE = "engine_llm_call_failed"


def _signature(job: str) -> str:
    """Value-free dedup signature: identity of the call site only.

    Deliberately carries no error text / status code — a signature that
    embeds a measured value mints a fresh Signal on every run whose error
    string differs (see the dedup-fingerprint rule).
    """
    return f"engine_llm:{job}"


def _resolve_target(model_hint: str, role: str) -> Any:
    """Honour the caller's configured model when its provider is
    credentialed, else fall back to pod tier resolution.

    ``credentialed_target`` returns ``None`` for an empty, bare
    (unqualified) or uncredentialed model — in every one of those cases
    ``resolve_infra_llm`` is the right answer, so this never presumes a
    provider.
    """
    from infra_llm import credentialed_target, resolve_infra_llm  # type: ignore

    return credentialed_target(model_hint) or resolve_infra_llm(role)


def _emit_failure(
    shared_dir: Path | str | None, job: str, detail: str, model: str
) -> None:
    """Raise (or re-observe) the firing Signal for a broken call site.

    Signal emission must never take down the job it is reporting on — a
    degraded report is still worth shipping — so every failure here is
    swallowed after being logged.
    """
    if not shared_dir:
        logger.warning("engine_llm[%s]: no shared_dir; Signal not emitted", job)
        return
    try:
        from signals import store  # type: ignore

        store.observe(
            Path(shared_dir),
            signature=_signature(job),
            producer=PRODUCER,
            type=SIGNAL_TYPE,
            scope="pod",
            title=f"Engine LLM call failed — {job}",
            body=(
                f"{job}'s background LLM enrichment call failed against a "
                f"CREDENTIALED provider, so this is breakage, not the "
                f"expected no-key degrade. The job still produced its "
                f"data-only output. Model: {model or 'unresolved'}. "
                f"Detail: {detail}"
            ),
            details={"job": job, "model": model, "error": detail},
        )
    except Exception as e:  # never let alerting break the report
        logger.warning("engine_llm[%s]: Signal emit failed: %s", job, e)


def _resolve_signal(shared_dir: Path | str | None, job: str) -> None:
    """Auto-clear this job's failure Signal once a call succeeds again."""
    if not shared_dir:
        return
    try:
        from signals import store  # type: ignore

        existing = store.find_active_by_signature(Path(shared_dir), _signature(job))
        if existing is not None:
            store.apply_transition(
                existing,
                "resolved",
                Path(shared_dir),
                actor=PRODUCER,
                reason="auto-resolve: engine LLM call succeeded",
            )
    except Exception as e:
        logger.debug("engine_llm[%s]: auto-resolve failed: %s", job, e)


def report_failure(
    shared_dir: Path | str | None, job: str, detail: str, model: str = ""
) -> None:
    """Raise this job's failure Signal for a fault the caller detected itself.

    Public counterpart to the internal emit: used when the provider DID
    answer but the answer was unusable (see :func:`extract_json_object`).
    That is broken enrichment, not an absent provider, so it gets the same
    loudness as a transport failure.
    """
    print(f"[{job}] engine LLM FAILED — {detail}", file=sys.stderr)
    _emit_failure(shared_dir, job, detail, model)


def extract_json_object(
    output: str, *, job: str, shared_dir: Path | str | None
) -> "dict[str, Any] | None":
    """Pull the single JSON object out of an LLM response.

    Providers commonly wrap JSON in a markdown fence even when told not to
    (the live 2026-08-18 probe returned ```json\n{...}\n```), so this scans
    for the outermost ``{``/``}`` pair rather than parsing the whole string.

    Returns the parsed dict, or ``None`` after raising a failure Signal —
    text that arrived but cannot be used is breakage, not a degrade.
    """
    start = output.find("{")
    end = output.rfind("}") + 1
    if start == -1 or end == 0:
        report_failure(shared_dir, job, "no JSON object in LLM output")
        return None
    import json

    try:
        parsed = json.loads(output[start:end])
    except json.JSONDecodeError as e:
        report_failure(shared_dir, job, f"malformed JSON: {e}")
        return None
    if not isinstance(parsed, dict):
        report_failure(shared_dir, job, f"expected a JSON object, got {type(parsed).__name__}")
        return None
    return parsed


def engine_complete(
    prompt: str,
    *,
    job: str,
    shared_dir: Path | str | None,
    model_hint: str = "",
    role: str = "fast",
    max_tokens: int = 1024,
    system: str = "",
    timeout: int = 60,
) -> tuple[str | None, str]:
    """Run one engine LLM completion. Returns ``(text_or_None, outcome)``.

    ``job``        — call-site id; scopes the Signal signature.
    ``shared_dir`` — pod shared dir (Signal store root). ``None`` disables
                     Signal emission (the failure is still logged and still
                     reported through the returned outcome).
    ``model_hint`` — the caller's own configured model (a tier pin, a
                     ``network.json`` override). Used only when its provider
                     is credentialed; never presumed.

    Callers keep their existing data-only path for a ``None`` text, but must
    NOT collapse the outcome: :data:`UNCREDENTIALED` is expected, while
    :data:`FAILED` means something is broken and has already been raised as
    a Signal.
    """
    try:
        target = _resolve_target(model_hint, role)
    except Exception as e:
        # Resolution itself blowing up is breakage, not a missing key.
        detail = f"model resolution raised {type(e).__name__}: {e}"
        print(f"[{job}] engine LLM FAILED — {detail}", file=sys.stderr)
        _emit_failure(shared_dir, job, detail, model_hint)
        return None, FAILED

    if target is None:
        # Expected, quiet degrade: the pod has no credentialed provider.
        print(
            f"[{job}] no credentialed LLM provider — data-only output "
            f"(expected; not a failure)",
            file=sys.stderr,
        )
        return None, UNCREDENTIALED

    try:
        from infra_llm import complete  # type: ignore

        text = complete(
            target,
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        print(
            f"[{job}] engine LLM FAILED against credentialed provider "
            f"{target.provider} ({target.model}) — {detail}",
            file=sys.stderr,
        )
        _emit_failure(shared_dir, job, detail, target.model)
        return None, FAILED

    if not (text or "").strip():
        detail = "provider returned empty text"
        print(
            f"[{job}] engine LLM FAILED — {detail} ({target.model})",
            file=sys.stderr,
        )
        _emit_failure(shared_dir, job, detail, target.model)
        return None, FAILED

    _resolve_signal(shared_dir, job)
    return text, OK
