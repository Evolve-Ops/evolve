"""Pod-state read tools — signals.

Currently exposes one tool:

* ``pod_state.signals.firing`` — list signals currently in the firing
  state, with optional filters by bot, severity, and producer. Returns
  a structured record per signal so the model can reason about counts,
  vectors, and titles.

This is the first concrete tool in the evo OC-native tool registry
(Phase 1.0 of ``docs/spec-evo-oc-native-2026-05-19.md``). The implementation
pattern here is the template for every other read tool: thin wrapper
over an existing analyzer module, a JSON-projection function that picks
the fields the model needs (no raw Signal dataclasses cross the wire),
and a closure that binds ``shared_dir`` so the model never has to know
about it.

Memory references: ``project_alerts_signal_store`` (locked
signal-store conventions), ``project_evo_oc_native_architecture``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Cap returned signals. The signal store can legitimately have 70+
# firing signals on a noisy pod; sending all of them in a single tool
# result would consume thousands of tokens and crowd out the model's
# reasoning room. The cap is generous (50 by default) and the model
# can re-call with tighter filters to drill down.
_DEFAULT_MAX_SIGNALS = 50


# Field projection. Picks just what the model needs to reason about a
# signal — title, severity, bot, producer, type, when. Excludes details
# (often verbose), state_history, deliveries, motivated_proposals
# (cross-link cruft), config_hint/remediation (UI-facing).
#
# Keep this projection narrow on purpose: the model can ask for richer
# detail on a specific signal by id via a separate tool when we add one
# (``pod_state.signals.detail(signal_id)`` — Phase 1.1).
def _project_signal(sig_dict: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": sig_dict.get("id"),
        "producer": sig_dict.get("producer"),
        "type": sig_dict.get("type"),
        "severity": sig_dict.get("severity"),
        "scope": sig_dict.get("scope"),
        "bot_id": sig_dict.get("bot_id"),
        "title": sig_dict.get("title"),
        "first_observed_at": sig_dict.get("created_at"),
        "last_observed_at": sig_dict.get("last_observed_at"),
        "observation_count": sig_dict.get("observation_count"),
    }
    # incident_key when present helps the model group related signals
    # (the alerts UI uses this to coalesce; the model can too).
    incident = sig_dict.get("incident_key")
    if incident:
        out["incident_key"] = incident
    return out


def _handler(
    shared_dir: Path,
    bot_id: str | None = None,
    severity: str | None = None,
    producer: str | None = None,
    max_results: int = _DEFAULT_MAX_SIGNALS,
) -> dict[str, Any]:
    """Read firing signals from the store, applying filters.

    The handler is invoked by the proxy with ``shared_dir`` already
    bound (see ``make_handler`` below). The model only sees the three
    filter args + max_results.

    Defensive: if signal-store import fails (rare; happens during
    dev when the analyzer package isn't importable), return an empty
    result with a structured error rather than blowing up the tool
    invocation. The model can see the error and adapt.
    """
    try:
        from signals import store as signals_store
    except ImportError as exc:
        log.warning("pod_state.signals.firing: signals store unavailable: %s", exc)
        return {
            "count": 0,
            "signals": [],
            "truncated": False,
            "error": "signal store unavailable in this runtime",
        }

    # Clamp max_results into [1, 200]. ``None`` (the model omitted the
    # arg entirely) falls back to the default; explicit zero or
    # negative clamps to 1 (always show at least one — operator likely
    # meant "as few as possible," not "show nothing"). Upper bound
    # 200 to keep the result size sane regardless of model input.
    requested = int(max_results) if max_results is not None else _DEFAULT_MAX_SIGNALS
    cap = max(1, min(requested, 200))

    # Note: signals.store.iter_active filters by `state` too, but we
    # pass state="firing" explicitly to be clear that snoozed signals
    # don't surface here. The model can call us with no filters and
    # know it gets firing-only.
    matched: list[dict[str, Any]] = []
    try:
        for sig in signals_store.iter_active(
            shared_dir,
            state="firing",
            bot_id=bot_id if bot_id else None,
            severity=severity if severity else None,
            producer=producer if producer else None,
        ):
            matched.append(_project_signal(sig.to_dict()))
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.signals.firing: signal iteration failed")
        return {
            "count": 0,
            "signals": [],
            "truncated": False,
            "error": f"signal iteration failed: {exc}",
        }

    truncated = len(matched) > cap
    return {
        "count": len(matched),     # total matched, before cap
        "signals": matched[:cap],  # capped list
        "truncated": truncated,    # True iff cap clipped results; the model
                                    # knows to refine filters or ask for more
    }


def make_handler(shared_dir: Path):
    """Bind shared_dir into a tool handler closure.

    The tool registry stores handlers as plain callables. ``shared_dir``
    is runtime context the model shouldn't have to know about, so it's
    closed over at registration time. The admin server passes its own
    ``shared_dir`` when bootstrapping the tool registry; tests pass a
    ``tmp_path``-derived fixture path.
    """
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _handler(shared_dir=shared_dir, **kwargs)
    return handler


# ─── Tool definition ────────────────────────────────────────────────────────
# This is the OC-facing description. The ``input_schema`` here is what
# the LLM sees when deciding whether to invoke the tool and what args
# to send. Keep the field descriptions terse but precise; ambiguity here
# is how tools get mis-invoked.

TOOL = Tool(
    name="pod_state.signals.firing",
    description=(
        "List signals currently in the firing state. Returns one record "
        "per signal with title, severity, bot, producer, type, and "
        "timing. Filter by bot_id, severity, or producer to narrow. "
        "Use this to answer 'what's firing on the pod?', 'what's worst "
        "on personal_bot?', or to ground claims about pod state before "
        "recommending action. Result is capped at 50 signals by "
        "default; if truncated=true, refine filters or raise "
        "max_results (up to 200)."
    ),
    wire_description=(
        "List signals currently firing — one record per signal with "
        "title, severity, bot, producer, type, and timing. Filter by "
        "bot_id, severity, or producer. Use to ground claims about "
        "pod state ('what's firing?', 'what's worst on X?'). Capped "
        "at 50 by default; if truncated=true, refine filters or "
        "raise max_results (up to 200)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": (
                    "Restrict to signals scoped to one bot, by id "
                    "(e.g. 'personal_bot', 'admin_bot', 'evolve'). Omit to "
                    "include pod-scoped signals + every bot."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["info", "warn", "alert"],
                "description": (
                    "Restrict to signals at this severity. 'alert' is "
                    "highest (red), 'warn' is yellow, 'info' is "
                    "informational. Omit to include all."
                ),
            },
            "producer": {
                "type": "string",
                "description": (
                    "Restrict to signals from one producer "
                    "(e.g. 'content_scan', 'permission_monitor', "
                    "'plugin_monitor', 'audit', 'host_health'). "
                    "Useful for filtering noise to one category."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": (
                    "Cap on the number of signals returned. Default "
                    "50. Bump if you need to see more, but be aware "
                    "the result size grows linearly."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_handler,                # placeholder; bound at registry-init time
    risk_tier=RiskTier.READ,         # pure read; no validate
    tags=("pod_state", "signals"),
    # Pod-wide firing signal state is operationally visible — alert
    # banners, breaker chips, and tile health all surface the same
    # information through other channels. Safe for cross-bot members
    # to ask "what's firing on the pod?" without exposing secrets.
    authorization_scope="anyone",
)


# Eager-register at import time. The handler in TOOL is the unbound
# version (no shared_dir); the runtime registration via init_tools()
# rewrites the entry with the bound handler. Until that's wired (the
# admin-server-startup hook + the CLI sync command come in Phase 1.1+),
# tests inject shared_dir explicitly and call the tool by importing
# _handler directly.
register(TOOL)


def init_for_shared_dir(shared_dir: Path) -> None:
    """Rebind the tool's handler with a real shared_dir, replacing the
    unbound placeholder in the registry.

    Called by the admin server at startup (Phase 1.1+ when we wire it
    into ``create_app``). For now this is a hook waiting on the
    integration PR; tests don't need it because they pass shared_dir
    explicitly via _handler.
    """
    bound = make_handler(shared_dir)
    register(Tool(
        name=TOOL.name,
        description=TOOL.description,
        wire_description=TOOL.wire_description,
        input_schema=TOOL.input_schema,
        handler=lambda **kwargs: bound(**kwargs),
        risk_tier=TOOL.risk_tier,
        tags=TOOL.tags,
    ))


# ─── pod_state.signals.history ─────────────────────────────────────────────
# Walks state_history on every signal (active + archived) to return a
# recent timeline of state transitions. Wraps the same logic the
# /api/signals/history endpoint uses in evolve_admin.web.server. Useful
# for "what happened in the last hour?" and "did personal_bot's alert just
# clear?" queries.


def _project_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Pick the fields the model needs from a state-history entry.

    Each entry already has from_state, to_state, at, actor, reason (via
    StateTransition.to_dict). We add identity (signal_id, title, etc.)
    from the parent signal — the original /api/signals/history endpoint
    flattens them the same way for the same reason: the model wants to
    answer 'what changed' without a second lookup.
    """
    return {
        "signal_id": entry.get("signal_id"),
        "title": entry.get("title"),
        "producer": entry.get("producer"),
        "type": entry.get("type"),
        "from_state": entry.get("from_state"),
        "to_state": entry.get("to_state"),
        "at": entry.get("at"),
        "actor": entry.get("actor"),
        "reason": entry.get("reason"),
    }


def _history_handler(
    shared_dir: Path,
    producer: str | None = None,
    bot_id: str | None = None,
    signal_id: str | None = None,
    state: str | None = None,
    max_results: int = 100,
) -> dict[str, Any]:
    """Read recent signal state transitions across all signals.

    Mirrors /api/signals/history's logic — walks state_history on every
    signal in firing/snoozed/archived, flattens to a single timeline,
    sorts newest-first, caps to max_results.

    Filters are optional; all AND together when multiple are passed.
    ``signal_id`` narrows to one specific signal's history — useful as
    the verify_via target after action.signal.{snooze,dismiss,resolve}
    so the model can confirm the state transition landed.
    ``state`` narrows to entries with the named target state (e.g.
    ``state="resolved"`` returns only transitions INTO resolved). When
    combined with ``signal_id`` this answers "did THIS signal reach
    THIS state?" — exactly the verify_via shape the action tools emit.
    """
    try:
        from signals import store as signals_store
    except ImportError as exc:
        log.warning("pod_state.signals.history: signals store unavailable: %s", exc)
        return {
            "count": 0,
            "entries": [],
            "truncated": False,
            "error": "signal store unavailable in this runtime",
        }

    requested = int(max_results) if max_results is not None else 100
    cap = max(1, min(requested, 1000))

    flat: list[dict[str, Any]] = []
    try:
        for sig in signals_store.iter_signals(
            shared_dir, subdirs=("firing", "snoozed", "archived"),
        ):
            if producer and sig.producer != producer:
                continue
            if bot_id and sig.bot_id != bot_id:
                continue
            if signal_id and sig.id != signal_id:
                continue
            for h in sig.state_history:
                entry = _project_history_entry({
                    "signal_id": sig.id,
                    "title": sig.title,
                    "producer": sig.producer,
                    "type": sig.type,
                    **h.to_dict(),
                })
                if state and entry.get("to_state") != state:
                    continue
                flat.append(entry)
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.signals.history: iteration failed")
        return {
            "count": 0,
            "entries": [],
            "truncated": False,
            "error": f"iteration failed: {exc}",
        }

    flat.sort(key=lambda e: e.get("at") or "", reverse=True)
    truncated = len(flat) > cap
    return {
        "count": len(flat),
        "entries": flat[:cap],
        "truncated": truncated,
    }


HISTORY_TOOL = Tool(
    name="pod_state.signals.history",
    description=(
        "List recent signal state transitions (newest first). Each "
        "entry is one transition (created / firing → snoozed / firing "
        "→ resolved / firing → dismissed / etc.) with timestamp, actor, "
        "and reason. Filter by producer or bot_id to narrow. Useful "
        "for 'what changed recently?', 'did that alert just clear?', "
        "and 'how long has personal_bot been firing this?' queries. Capped "
        "at 100 entries by default (max 1000)."
    ),
    wire_description=(
        "List recent signal state transitions, newest first — each "
        "with from/to state, timestamp, actor, and reason. Filter by "
        "producer, bot_id, signal_id, or state. Use for 'what "
        "changed recently?', 'did that alert just clear?', or to "
        "confirm an action.signal.* transition landed. Capped at 100 "
        "entries by default (max 1000)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "producer": {
                "type": "string",
                "description": (
                    "Restrict to transitions on signals from this "
                    "producer (e.g. 'content_scan', 'permission_monitor')."
                ),
            },
            "bot_id": {
                "type": "string",
                "description": (
                    "Restrict to transitions on signals scoped to one "
                    "bot. Omit to include pod-scoped + every bot."
                ),
            },
            "signal_id": {
                "type": "string",
                "description": (
                    "Restrict to one specific signal's transitions. "
                    "Used by verify_via after action.signal.{snooze,"
                    "dismiss,resolve} to confirm the transition landed."
                ),
            },
            "state": {
                "type": "string",
                "description": (
                    "Restrict to transitions whose target state is this "
                    "(e.g. 'snoozed', 'resolved', 'dismissed'). Pairs "
                    "with signal_id to answer 'did this signal reach "
                    "this state?'."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": (
                    "Cap on entries returned. Default 100."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_history_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "signals", "history"),
    # Same rationale as pod_state.signals.firing — recent state
    # transitions are part of the same operationally-visible alert
    # timeline. No PII / no credentials cross the wire.
    authorization_scope="anyone",
)

register(HISTORY_TOOL)
