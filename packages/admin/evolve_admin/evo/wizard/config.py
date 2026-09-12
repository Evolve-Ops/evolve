"""Per-bot conversational-approval config resolution (slice 5b8 day 2).

Thin wrapper over ``better_engine_config.load(shared_dir)`` that hands
back the keys the rec_pending engine handler and intent classifier need:
``enabled``, ``llm_intent_parse_enabled``, ``confidence_threshold``,
``default_snooze_days``, ``pending_expiry_minutes``,
``push_preamble_enabled``.

Lives here (rather than reading the JSON directly) so the wizard module
has a single import point for config lookups, with a graceful-default
path when the config module isn't importable (the analyzer package
isn't importable in some test contexts) or the file is malformed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Compiled-in fallbacks. Stay in sync with
# ``better_engine_config._COMPILED_DEFAULTS["conversational_approval"]``.
_FALLBACK_ENABLED = True
_FALLBACK_LLM = True
_FALLBACK_CONFIDENCE = 0.80
_FALLBACK_SNOOZE_DAYS = 3
_FALLBACK_EXPIRY_MINUTES = 60
_FALLBACK_PUSH_PREAMBLE = False


@dataclass(frozen=True)
class ConversationalApprovalConfig:
    """Resolved conversational-approval settings for one (bot_id, shared_dir)
    lookup. All fields have safe defaults so callers can use a fallback
    instance when config loading fails."""

    enabled: bool = _FALLBACK_ENABLED
    llm_intent_parse_enabled: bool = _FALLBACK_LLM
    confidence_threshold: float = _FALLBACK_CONFIDENCE
    default_snooze_days: int = _FALLBACK_SNOOZE_DAYS
    pending_expiry_minutes: int = _FALLBACK_EXPIRY_MINUTES
    push_preamble_enabled: bool = _FALLBACK_PUSH_PREAMBLE


def resolve(shared_dir: Path, bot_id: str | None) -> ConversationalApprovalConfig:
    """Read better-engine-config.json and resolve the
    conversational_approval block for ``bot_id`` (with pod-default
    fallback). Returns a fully-populated config; on any error returns
    the compiled defaults so the engine never wedges on a bad config."""
    try:
        from better_engine_config import load as _load  # type: ignore
    except Exception:
        return ConversationalApprovalConfig()

    try:
        cfg = _load(shared_dir)
    except Exception:
        return ConversationalApprovalConfig()

    def _get(key: str, fallback):
        try:
            return cfg.resolve(bot_id, "conversational_approval", key)
        except Exception:
            return fallback

    return ConversationalApprovalConfig(
        enabled=bool(_get("enabled", _FALLBACK_ENABLED)),
        llm_intent_parse_enabled=bool(
            _get("llm_intent_parse_enabled", _FALLBACK_LLM)
        ),
        confidence_threshold=_coerce_float(
            _get("confidence_threshold", _FALLBACK_CONFIDENCE),
            _FALLBACK_CONFIDENCE,
        ),
        default_snooze_days=_coerce_int(
            _get("default_snooze_days", _FALLBACK_SNOOZE_DAYS),
            _FALLBACK_SNOOZE_DAYS,
        ),
        pending_expiry_minutes=_coerce_int(
            _get("pending_expiry_minutes", _FALLBACK_EXPIRY_MINUTES),
            _FALLBACK_EXPIRY_MINUTES,
        ),
        push_preamble_enabled=bool(
            _get("push_preamble_enabled", _FALLBACK_PUSH_PREAMBLE)
        ),
    )


def _coerce_float(v, fallback: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def _coerce_int(v, fallback: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return fallback
