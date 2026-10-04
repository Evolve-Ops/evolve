"""Tool-gap telemetry tools — Phase 1.5b slice of spec §14.3.

Two tools that expose the tool-gap telemetry layer to evo:

* ``action.evo.log_tool_gap(kind, scope, fingerprint)`` — write_safe.
  Evo invokes when it encounters the §13.4 Q4 escape hatch (no
  matching tool, no matching proposal generator). The record stays
  LOCAL by default; opt-in upload daemon (separate later PR) ships
  anonymized rollups to Evolve's telemetry endpoint.

* ``pod_state.tool_gaps()`` — read. Lets evo see its own gap history
  ("have I asked for this kind of tool before?") and the operator
  inspect what evo has logged via the chat surface.

Risk choice: ``action.evo.log_tool_gap`` is write_safe — small,
reversible, bounded blast radius (writes one line to one local
file). Auto-executes under auto-small / auto authority; renders a
confirm button under ask. The confirm button at ask authority is a
minor friction tradeoff: it keeps evo from spamming the log without
operator awareness during the period when the operator is still
calibrating trust.

The validate gate is loose — just argument-shape checks. The
underlying ``tool_gaps.record_tool_gap`` does the heavy validation
(reject control chars, oversize tokens, etc.) so we don't double
up.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register
from .. import tool_gaps

log = logging.getLogger(__name__)


# ─── action.evo.log_tool_gap ─────────────────────────────────────────────────


def _log_handler(
    shared_dir: Path,
    kind: str,
    scope: str,
    fingerprint: str,
) -> dict[str, Any]:
    """Append a tool-gap record (or increment an existing fingerprint).

    Returns the same shape as ``tool_gaps.RecordResult.__dict__`` so
    evo sees whether this was a new gap or a repeat (with the new
    frequency count).
    """
    result = tool_gaps.record_tool_gap(
        shared_dir,
        kind=kind,
        scope=scope,
        fingerprint=fingerprint,
    )
    if not result.ok:
        return {"ok": False, "error": result.error}
    return {
        "ok": True,
        "kind": kind,
        "scope": scope,
        "fingerprint": result.fingerprint,
        "new": result.new,
        "frequency_local": result.frequency_local,
        # Verify hint: the model can confirm the write by reading
        # pod_state.tool_gaps and finding this fingerprint with the
        # frequency we just reported.
        "verify_via": {
            "tool": "pod_state.tool_gaps",
            "args": {},
            "expect": (
                f"fingerprint {fingerprint!r} appears with "
                f"frequency_local={result.frequency_local}"
            ),
        },
    }


def _log_validate(
    shared_dir: Path,
    kind: str,
    scope: str,
    fingerprint: str,
) -> dict[str, Any]:
    """Dry-run: just argument-shape checks. The handler's record_tool_gap
    does deeper validation (token charset, length); here we surface the
    minimum required for the proxy's button-rendering gate to make a
    sensible decision."""
    if not isinstance(kind, str) or not kind.strip():
        return {"ok": False, "reason": "kind is required"}
    if not isinstance(scope, str) or not scope.strip():
        return {"ok": False, "reason": "scope is required"}
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return {"ok": False, "reason": "fingerprint is required"}
    # Validate against the same regex the writer uses, but surface a
    # friendlier reason so evo can rephrase rather than just retry.
    if not tool_gaps._is_valid_token(kind):
        return {
            "ok": False,
            "reason": (
                f"kind must be a short identifier (alphanumeric + "
                f"_-.:); got {kind!r}"
            ),
        }
    if not tool_gaps._is_valid_token(scope):
        return {
            "ok": False,
            "reason": (
                f"scope must be a short identifier; got {scope!r}. "
                f"Conventional values: single_bot, pod_wide, external, unknown."
            ),
        }
    if not tool_gaps._is_valid_token(fingerprint):
        return {
            "ok": False,
            "reason": (
                f"fingerprint must be a short identifier (no whitespace "
                f"or control chars); got {fingerprint!r}"
            ),
        }
    return {
        "ok": True,
        "context": {
            "kind": kind,
            "scope": scope,
            "fingerprint": fingerprint,
        },
    }


LOG_TOOL = Tool(
    name="action.evo.log_tool_gap",
    description=(
        "Record a tool-gap observation when evo encounters an operator "
        "request that doesn't fit any existing tool (the §13.4 Q4 "
        "escape hatch — no proposal, no direct tool). The record is "
        "the SHAPE of the gap (kind / scope / fingerprint) — never "
        "chat content, bot ids, or proposal bodies. Records stay LOCAL "
        "by default; opt-in upload daemon ships anonymized rollups to "
        "Evolve's telemetry endpoint so the dev team sees aggregate "
        "demand across pods. Repeat observations of the same "
        "fingerprint increment a single record rather than spawning "
        "duplicates. Conventional scopes: single_bot, pod_wide, "
        "external, unknown."
    ),
    wire_description=(
        "Record a tool-gap observation when an operator request fits "
        "NO existing tool or proposal — log the gap instead of "
        "improvising. Records only the gap's SHAPE (kind / scope / "
        "fingerprint), never chat content or bot ids; stays local. "
        "Repeats of the same fingerprint increment one record. "
        "Conventional scopes: single_bot, pod_wide, external, unknown."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": (
                    "Short dotted identifier for the kind of action evo "
                    "wanted to take (eg 'config.bot.set_model', "
                    "'action.bot.relocate'). Plain identifier characters "
                    "only — no whitespace or chat content."
                ),
            },
            "scope": {
                "type": "string",
                "description": (
                    "Blast-radius shape of the missing action. "
                    "Conventional: single_bot, pod_wide, external, "
                    "unknown."
                ),
            },
            "fingerprint": {
                "type": "string",
                "description": (
                    "Stable bucket key — same shape of gap should "
                    "produce the same fingerprint so frequency "
                    "aggregates instead of spawning duplicates. "
                    "Example: 'config-edit:agents.defaults.model'."
                ),
            },
        },
        "required": ["kind", "scope", "fingerprint"],
        "additionalProperties": False,
    },
    handler=_log_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_log_validate,
    tags=("action", "evo", "telemetry"),
    # Logging a tool-gap observation is the caller (evo) recording its
    # OWN missing-tool encounter — fingerprint shape only, no chat
    # content, no operator PII. Cross-bot members reach this only
    # indirectly (evo notices a gap on their behalf and logs it). Safe
    # to open; the underlying record_tool_gap enforces token sanity.
    authorization_scope="anyone",
)

register(LOG_TOOL)


# ─── pod_state.tool_gaps ─────────────────────────────────────────────────────


def _read_handler(
    shared_dir: Path,
    limit: int = 50,
) -> dict[str, Any]:
    """Read aggregated tool-gap records, newest-first.

    Aggregated: each fingerprint appears once with its latest
    frequency_local + last_observed. Useful for both evo
    introspection ("have I logged this gap?") and operator-facing
    chat (evo paraphrases the most-frequent gaps).
    """
    records = tool_gaps.read_tool_gaps(shared_dir, limit=limit)
    return {
        "ok": True,
        "count": len(records),
        "limit": limit,
        "tool_gaps": [r.to_dict() for r in records],
    }


READ_TOOL = Tool(
    name="pod_state.tool_gaps",
    description=(
        "Read recent tool-gap records (newest-first, aggregated by "
        "fingerprint). Each entry has kind, scope, fingerprint, "
        "first_observed, last_observed, frequency_local. Use to "
        "answer 'have I logged this gap before?' or to surface "
        "frequent gaps in chat (eg 'we've hit a config.bot.set_model "
        "gap 4 times this week — worth filing upstream?'). Cap with "
        "limit (default 50, max 1000)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max records returned (default 50, max 1000).",
                "default": 50,
                "minimum": 1,
                "maximum": 1000,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=_read_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "telemetry"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(READ_TOOL)
