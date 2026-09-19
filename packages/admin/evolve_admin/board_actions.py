"""board_actions.py — D-BI5 action vocabulary, cost estimate, and gating.

Design: ``internal/design-pa-board-interface-v2-2026-09-04.md`` D-BI5 (cards
carry ``actions[]``; a tap is an instruction) and D-BI6 (email-derived cards
never carry a send/book/pay action).

Deliberately separate from ``board_store``: the vocabulary lookup
(:func:`default_actions_for`) is pure data and is what ``board_store.add_card``
calls, but the live parts — ``est_cost`` and whether an action is currently
enabled — need pod state (the bot's fast-rung model, the pricing catalog,
whether Google is configured) that a card-store WRITE must never depend on.
``board_store`` stays a pure state machine; the read path (``routes_board``)
calls :func:`bot_action_context` once per request and :func:`annotate_action`
per action, so gating is always evaluated against CURRENT pod state, never a
snapshot frozen at add time — an integration granted after a card was stocked
must make its actions usable without touching the card.

THE RUNG STAND-IN. The addendum asks for "the integration's current autonomy
rung" (D-BI5/§5) — but the numeric autonomy ladder itself is a separate,
not-yet-built design track (no code anywhere defines a live per-integration
rung). Modelling ``requires_rung`` as a plain int now, compared against a
binary granted/not-granted "current rung" (0 or 1) per integration, is the
literal mechanism the addendum describes without inventing the finer ladder;
when that ladder lands, only :func:`_current_rung` needs to change to read it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: At most three actions per card (D-BI5).
MAX_ACTIONS_PER_CARD = 3

#: requires_rung values. 0 = no integration needed, always usable. 1 = needs
#: an integration the bot may or may not have granted (Google, today).
#: 2 = needs a capability that does not exist on ANY pod yet (placing a
#: phone call) — always disabled, and honestly so: this is not a per-bot
#: grant gap, it is a pod capability gap.
RUNG_NONE = 0
RUNG_INTEGRATION = 1
RUNG_UNBUILT = 2

_INTEGRATION_NONE = None
_INTEGRATION_GOOGLE = "google"
_INTEGRATION_CALLING = "calling"

#: {integration -> requires_rung}. Purely a lookup so the vocabulary table
#: below can name an integration once and get both numbers.
_RUNG_FOR_INTEGRATION = {
    _INTEGRATION_NONE: RUNG_NONE,
    _INTEGRATION_GOOGLE: RUNG_INTEGRATION,
    _INTEGRATION_CALLING: RUNG_UNBUILT,
}

#: D-BI5 starter vocabulary. Keyed by card ``source`` first (a calendar- or
#: email-sourced card wants calendar-/email-shaped actions regardless of
#: cluster), falling back to ``cluster`` (D-PA's vocabulary) for everything
#: else. Widened from evidence over time (D-BI5) — this is the starter set,
#: not a ceiling.
#:
#: Each entry: id, label, kind (tool|llm), integration (None/google/calling).
#: ``requires_confirm`` marks an action whose target could be an address the
#: card's own untrusted source handed us (D-BI6).
_CALENDAR_ACTIONS: tuple[dict[str, Any], ...] = (
    {"id": "prepare_dossier", "label": "Prepare a dossier",
     "kind": "llm", "integration": _INTEGRATION_GOOGLE},
    {"id": "find_a_time", "label": "Find a time that works",
     "kind": "llm", "integration": _INTEGRATION_GOOGLE},
    {"id": "draft_decline", "label": "Draft a decline",
     "kind": "llm", "integration": _INTEGRATION_GOOGLE},
)

#: D-BI6: email-derived cards get LLM-only, draft-level actions — never a
#: send/book/pay action — and the one action with an email-derived target
#: (the reply goes back to whoever sent the email) carries requires_confirm.
_EMAIL_ACTIONS: tuple[dict[str, Any], ...] = (
    {"id": "draft_reply", "label": "Draft a reply",
     "kind": "llm", "integration": _INTEGRATION_GOOGLE,
     "requires_confirm": True},
    {"id": "summarise_thread", "label": "Summarise the thread",
     "kind": "llm", "integration": _INTEGRATION_GOOGLE},
    {"id": "extract_tasks", "label": "Extract tasks",
     "kind": "llm", "integration": _INTEGRATION_GOOGLE},
)

#: Per-cluster fallback (D-PA's cluster vocabulary: health, fitness, travel,
#: work, social, hobbies, family, home, admin). "find directions" is the one
#: ``kind: tool`` entry in this table — deterministic (a maps link built from
#: the card's own ``location`` enrichment), zero model cost, no integration.
_CLUSTER_ACTIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "health": (
        {"id": "call_ahead", "label": "Call ahead to confirm hours",
         "kind": "llm", "integration": _INTEGRATION_CALLING},
        {"id": "find_directions", "label": "Find directions",
         "kind": "tool", "integration": _INTEGRATION_NONE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "fitness": (
        {"id": "find_a_time", "label": "Find a time that works",
         "kind": "llm", "integration": _INTEGRATION_GOOGLE},
        {"id": "find_directions", "label": "Find directions",
         "kind": "tool", "integration": _INTEGRATION_NONE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "travel": (
        {"id": "find_directions", "label": "Find directions",
         "kind": "tool", "integration": _INTEGRATION_NONE},
        {"id": "draft_packing_note", "label": "Draft a packing note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "work": (
        {"id": "draft_reply", "label": "Draft a reply",
         "kind": "llm", "integration": _INTEGRATION_GOOGLE},
        {"id": "summarise_thread", "label": "Summarise the thread",
         "kind": "llm", "integration": _INTEGRATION_GOOGLE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "social": (
        {"id": "draft_message", "label": "Draft a message",
         "kind": "llm", "integration": _INTEGRATION_NONE},
        {"id": "find_a_time", "label": "Find a time that works",
         "kind": "llm", "integration": _INTEGRATION_GOOGLE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "family": (
        {"id": "draft_message", "label": "Draft a message",
         "kind": "llm", "integration": _INTEGRATION_NONE},
        {"id": "find_a_time", "label": "Find a time that works",
         "kind": "llm", "integration": _INTEGRATION_GOOGLE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "hobbies": (
        {"id": "find_related_info", "label": "Find related info",
         "kind": "llm", "integration": _INTEGRATION_NONE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "home": (
        {"id": "call_ahead", "label": "Call ahead to confirm hours",
         "kind": "llm", "integration": _INTEGRATION_CALLING},
        {"id": "find_directions", "label": "Find directions",
         "kind": "tool", "integration": _INTEGRATION_NONE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
    "admin": (
        {"id": "call_ahead", "label": "Call ahead to confirm hours",
         "kind": "llm", "integration": _INTEGRATION_CALLING},
        {"id": "find_directions", "label": "Find directions",
         "kind": "tool", "integration": _INTEGRATION_NONE},
        {"id": "draft_reminder", "label": "Draft a reminder note",
         "kind": "llm", "integration": _INTEGRATION_NONE},
    ),
}

#: Sources that get the calendar/email vocabulary instead of the cluster
#: fallback, regardless of the card's cluster.
_SOURCE_ACTIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "calendar": _CALENDAR_ACTIONS,
    "email": _EMAIL_ACTIONS,
}

#: Public wire keys for one action (D-BI5: ``{id, label, kind, est_cost,
#: requires_rung}``), plus ``requires_confirm`` when set (D-BI6). Internal
#: bookkeeping (``integration``) never reaches the client.
_WIRE_KEYS = ("id", "label", "kind", "requires_confirm")


def default_actions_for(*, cluster: str, source: str) -> list[dict[str, Any]]:
    """The starter ``actions[]`` for a new card — pure data, no network.

    Called from ``board_store.add_card`` at add time (D-BI5: "computed
    deterministically at add time — no model call to choose actions"). Never
    reads the pricing catalog or a bot's integrations; that happens on READ
    (:func:`bot_action_context` / :func:`annotate_action`), against whatever
    is true right now.
    """
    table = _SOURCE_ACTIONS.get(source) or _CLUSTER_ACTIONS.get(cluster) \
        or _CLUSTER_ACTIONS["admin"]
    out = []
    for action in table[:MAX_ACTIONS_PER_CARD]:
        entry = {k: action[k] for k in _WIRE_KEYS if k in action}
        entry["_integration"] = action.get("integration")
        out.append(entry)
    return out


def find_action(card: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    """The action named ``action_id`` on ``card``, or ``None``."""
    for action in card.get("actions") or []:
        if action.get("id") == action_id:
            return action
    return None


# ── live gating + cost (READ path only) ────────────────────────────────────

#: Conservative token estimate for one action's turn. Every action here is
#: "one named action" (D-BI4) — a bounded, single-purpose instruction, not an
#: open-ended chat — so one estimate serves the whole starter vocabulary
#: rather than a per-action guess that would just be noise at this size.
_EST_INPUT_TOKENS = 4_000
_EST_OUTPUT_TOKENS = 800


def _google_granted(bot_id: str, network: dict[str, Any]) -> bool:
    from .google_service import is_google_configured
    return is_google_configured(bot_id, network)


def _fast_rung_cost_per_token(
    bot_id: str, network: dict[str, Any], shared_dir: Path,
) -> tuple[float, float] | None:
    """``(input_cost_per_token, output_cost_per_token)`` for the bot's fast
    rung, or ``None`` when either the rung or its pricing is unknown.

    Lazy imports: ``primary_bot`` / ``model_pricing`` live in
    ``packages/analyzer``, loaded by bare name the way every other admin
    caller of them does (e.g. ``routes_bot_config.py``) — not imported at
    module load so a context that never asks for a cost estimate never pays
    for pulling in the model-rungs machinery.
    """
    from primary_bot import bot_tier_models
    from model_pricing import lookup_price, read_pricing_cache

    chain = bot_tier_models(network, bot_id, "fast")
    if not chain:
        return None
    provider, _, bare = chain[0].partition("/")
    if not bare:
        return None
    cache = read_pricing_cache(Path(shared_dir))
    rec = lookup_price(cache, provider, bare)
    if not rec:
        return None
    in_cost = rec.get("input_cost_per_token")
    out_cost = rec.get("output_cost_per_token")
    if in_cost is None or out_cost is None:
        return None
    return float(in_cost), float(out_cost)


def bot_action_context(
    bot_id: str, network: dict[str, Any], shared_dir: Path,
) -> dict[str, Any]:
    """Bot-wide facts every action's gating depends on — resolved ONCE per
    board read, not once per card action (a board can hold thousands of
    cards; the pricing cache and Google config do not vary card to card)."""
    rates = _fast_rung_cost_per_token(bot_id, network, shared_dir)
    llm_cost = None
    if rates is not None:
        in_cost, out_cost = rates
        llm_cost = round(
            _EST_INPUT_TOKENS * in_cost + _EST_OUTPUT_TOKENS * out_cost, 4)
    return {
        "google": _google_granted(bot_id, network),
        "llm_est_cost": llm_cost,
    }


#: Sentinel "current rung" for an action that names no integration at all —
#: strictly greater than any ``requires_rung`` in this table, so it can never
#: be the limiting factor. Distinct from :data:`RUNG_UNBUILT` (which is a
#: real *requirement* an action can carry) even though the two happen to
#: share no comparison that matters — keeping them separate names avoids
#: reading "granted at the unbuilt rung" as anything but a coincidence.
_RUNG_NO_GATE = 99


def _current_rung(integration: str | None, ctx: dict[str, Any]) -> int:
    """The rung this integration is CURRENTLY granted at, for this bot.

    Binary stand-in (see module docstring) — 1 if granted, 0 if not; a
    capability with no integration at all (``None``) is always "granted" at
    every rung an ungated action could ask for.
    """
    if integration is None:
        return _RUNG_NO_GATE  # nothing to gate; never the limiting factor.
    if integration == _INTEGRATION_GOOGLE:
        return RUNG_INTEGRATION if ctx.get("google") else RUNG_NONE
    return RUNG_NONE  # an integration this pod has never built (e.g. calling).


_DISABLED_REASON = {
    _INTEGRATION_GOOGLE: "connect Google (Calendar/Gmail) to enable this action",
    _INTEGRATION_CALLING: "no calling integration on this pod yet",
}


def annotate_action(
    action: dict[str, Any], ctx: dict[str, Any],
) -> dict[str, Any]:
    """One action, ready for the wire: ``est_cost``/``requires_rung`` filled
    from ``ctx`` (see :func:`bot_action_context`), disabled with a reason
    when it cannot run right now — never simply omitted (D-BI5: "shown
    disabled with the reason")."""
    integration: str | None = action.get("_integration")
    out = {k: v for k, v in action.items() if not k.startswith("_")}
    requires_rung = _RUNG_FOR_INTEGRATION.get(integration, RUNG_INTEGRATION)
    out["requires_rung"] = requires_rung
    if action.get("kind") == "tool":
        out["est_cost"] = 0.0
        return out
    current = _current_rung(integration, ctx)
    if requires_rung > current:
        out["est_cost"] = None
        out["disabled_reason"] = _DISABLED_REASON.get(
            integration or "", "not available yet")
        return out
    if ctx.get("llm_est_cost") is None:
        out["est_cost"] = None
        out["disabled_reason"] = (
            "cost estimate unavailable for the fast-rung model")
        return out
    out["est_cost"] = ctx["llm_est_cost"]
    return out


def annotate_card_actions(
    card: dict[str, Any], ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """``card``'s ``actions[]``, each annotated for the wire. Does not mutate
    ``card`` — the caller decides whether to replace or copy."""
    return [annotate_action(a, ctx) for a in (card.get("actions") or [])]


__all__ = [
    "MAX_ACTIONS_PER_CARD",
    "RUNG_NONE", "RUNG_INTEGRATION", "RUNG_UNBUILT",
    "default_actions_for", "find_action",
    "bot_action_context", "annotate_action", "annotate_card_actions",
]
