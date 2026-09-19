"""board_stack.py — D-ST1..9: the Stack's server-side composition, and the
two small pieces of state it owns that the board's own store did not need.

Design: ``internal/design-pa-card-stack-2026-09-14.md`` (D-ST1..9). The
Stack is a second VIEW over ``board_store``'s existing cards and events —
nothing in the store's shape changes beyond what the chip's guardrail
allows: one new event type (``seen``, D-ST3's pass counter) and one new
field on an existing move (``snooze_until``, D-ST4's Later chip, added in
``board_store.move_card``). Composition itself
(:func:`stack_order`) is a PURE function over cards already loaded by the
caller — no I/O, no lock — so the fixture proof in the tests is exactly what
a reviewer can read by eye, and the route (``routes_board.py``) owns every
load/save and the ``WRITE_LOCK``, the same split ``board_actions.py`` already
uses for the board's action-gating logic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import board_store

#: D-ST3: "on the third pass in one day" — the pass counter's warning
#: threshold. Counts today's swipe-ups only; a pass logged yesterday says
#: nothing about whether the card is stuck TODAY.
SEEN_WARN_AT = 3

#: D-ST6: Approve / Decline reuse the store's EXISTING delegation states —
#: the chip's guardrail allows exactly one new event type (``seen``) and one
#: new field (the snooze), so "approved"/"declined" are not new states.
#: Approve settles the hand-over the way any other successful delegation
#: does (``done``); Decline is modelled as ``blocked`` — the same state a
#: missing integration or a rung gate already uses (D-BI4) — because "no,
#: not like that" is exactly the shape of thing that stops a worker without
#: saying how to get unstuck, and the not-yet-built worker
#: (``board-bot-lane-worker``) reads ``blocked`` as "needs a person" either
#: way. If evidence later shows users expect a decline to trigger an
#: automatic retry, that is a reason to widen ``DELEGATION_STATES`` — not to
#: guess here.
DECISION_TO_STATE = {"approve": "done", "decline": "blocked"}


class CardNotReadyForDecision(ValueError):
    """Approve/Decline exist only for a ``returned_for_review`` card
    (D-ST6) — a bot-owned card still ``offered``/``in_progress`` has
    nothing to approve yet. A ``ValueError`` subclass so a bare
    ``except ValueError`` still catches it; ``routes_board.py`` catches
    this one FIRST to map it to 409 (well-formed request, wrong card
    state) instead of the generic 400.
    """

#: The three "Later" chip durations the Stack card face offers (D-ST4). Pure
#: labels — the actual target date is computed client-side (only the
#: browser knows the viewer's local "tomorrow"), so this list exists only to
#: keep the client and this docstring naming the same three options.
SNOOZE_PRESETS = ("tomorrow", "this weekend", "next week")

_SOURCE_WHY = {
    "calendar": "from calendar", "email": "from email",
    "manual": "added by you", "import": "imported",
}


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _when_start(card: dict[str, Any]) -> str | None:
    """The sortable start time of a card's ``enrichment.when``, or None."""
    e = card.get("enrichment") or {}
    field = e.get("when")
    if not isinstance(field, dict):
        return None
    value = field.get("value")
    if isinstance(value, dict):
        start = value.get("start")
        return start if isinstance(start, str) and start else None
    if isinstance(value, str) and value:
        return value
    return None


def _is_bot_return(card: dict[str, Any]) -> bool:
    """D-ST2 group 1: the bot is asking.

    The design names two triggers — "returned_for_review" and "an approval
    waiting" — but the shipped ``delegation`` block
    (:data:`board_store.DELEGATION_STATES`) carries no second, independent
    approval marker, so both read the same field today. Written as its own
    check (not inlined at each call site) so the day a second marker exists,
    one function grows a clause instead of every caller needing to.
    """
    if card.get("owner") != "bot":
        return False
    return (card.get("delegation") or {}).get("state") == "returned_for_review"


def _parse_snooze(value: Any) -> datetime | None:
    """A ``snooze_until`` value, full timestamp or bare date, or None."""
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _later_due(card: dict[str, Any], now: datetime) -> bool:
    """Whether a ``later`` card has "come due" (D-ST2).

    A card with no ``snooze_until`` at all — every ``later`` card moved
    before this chip existed, and any moved through the board's own sheet —
    carries nothing saying it should wait, so it is due now. An unparsable
    stamp fails the same way :func:`board_store.is_archived` does: toward a
    visible card, never toward a silently hidden one.
    """
    until = card.get("snooze_until")
    if not until:
        return True
    parsed = _parse_snooze(until)
    if parsed is None:
        return True
    return parsed <= now


def stack_order(
    cards: list[dict[str, Any]], briefing: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """D-ST2's composition, top to bottom.

    ``cards`` is whatever the caller already loaded — the route passes
    :func:`board_store.visible_cards`'s result, actions already annotated
    for the wire — this function only orders and filters; it never mutates
    a card. Pure and deterministic given ``now``: no clock read when ``now``
    is supplied, which is what makes the fixture test exact.

    ``done``/``dropped`` never appear (D-ST2) even though
    :func:`board_store.visible_cards` can still hand back a recently-settled
    card (its 30-day tile window is a different question from the Stack's).
    Filtered here by lane membership in the four groups below, not by
    "is it settled", so the check reads the same regardless of how the
    archive filter evolves.
    """
    now = now or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    if briefing and not briefing.get("dismissed"):
        items.append({"kind": "briefing", "briefing": briefing})

    returns: list[dict[str, Any]] = []
    today_cards: list[dict[str, Any]] = []
    inbox_cards: list[dict[str, Any]] = []
    later_due: list[dict[str, Any]] = []
    for card in cards:
        if _is_bot_return(card):
            returns.append(card)
            continue
        lane = card.get("lane")
        if lane == "today":
            today_cards.append(card)
        elif lane == "inbox":
            inbox_cards.append(card)
        elif lane == "later" and _later_due(card, now):
            later_due.append(card)
        # A `later` card not yet due, and everything settled: excluded.

    # Oldest ask first — a return that has been waiting longest gets seen
    # first, the same fairness :func:`board_store.append_event`'s ordering
    # gives every other queue in this store.
    returns.sort(key=lambda c: (c.get("delegation") or {}).get("updated_at")
                 or c.get("created_at") or "")

    def today_key(c: dict[str, Any]) -> tuple[int, str]:
        start = _when_start(c)
        return (0, start) if start else (1, c.get("created_at") or "")

    today_cards.sort(key=today_key)
    inbox_cards.sort(key=lambda c: c.get("created_at") or "")
    later_due.sort(key=lambda c: c.get("created_at") or "")

    for group in (returns, today_cards, inbox_cards, later_due):
        items.extend({"kind": "card", "card": c} for c in group)
    return items


def card_why(card: dict[str, Any]) -> str:
    """D-ST6's one-line "why it's here" — the bot's own ``why_saved`` note
    when it left one, else a plain statement of where the card came from.

    Deliberately no time-of-day text here: only the browser knows the
    viewer's timezone, so the client appends a locale-formatted time from
    ``enrichment.when`` itself when there is one, rather than this function
    guessing at a timezone and risking showing the wrong time.
    """
    e = card.get("enrichment") or {}
    why = e.get("why_saved")
    if isinstance(why, dict):
        value = why.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    source = card.get("source") or "manual"
    return _SOURCE_WHY.get(source, f"from {source}")


def record_card_seen(
    shared_dir: Path, bot_id: str, card_id: str, *, actor: str,
    undo: bool = False, now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """D-ST3's pass counter. Returns ``(card, warn)`` — ``warn`` is True the
    moment today's count reaches :data:`SEEN_WARN_AT`, False on every other
    call including an ``undo``.

    Caller holds ``board_store.WRITE_LOCK`` — this function does not, the
    same lock-lives-in-the-caller convention every other writer in
    ``board_store`` already follows (``board_store.WRITE_LOCK``'s
    docstring). Raises ``KeyError``/``ValueError`` exactly as
    :func:`board_store.resolve_card` does.

    ``undo`` reverses the ONE increment the just-shown toast caused — it
    does not log a second event (the design says "removes the seen
    increment", not "records an unsee"). A count can never go negative, and
    undoing past a day boundary (the toast timed out, midnight passed) is a
    no-op: there is no still-open increment from today to remove.
    """
    today = _today(now)
    board = board_store.load_board(shared_dir, bot_id)
    card = board_store.resolve_card(board, card_id)
    card_id = card["id"]
    prev = card.get("seen_today") or {}
    count = prev.get("count", 0) if prev.get("date") == today else 0
    if undo:
        count = max(0, count - 1)
        card["seen_today"] = {"date": today, "count": count}
        board_store.save_board(shared_dir, bot_id, board)
        return card, False
    count += 1
    card["seen_today"] = {"date": today, "count": count}
    board_store.save_board(shared_dir, bot_id, board)
    board_store.append_event(shared_dir, bot_id, {
        "event": "seen", "card": card_id, "title": card.get("title"),
        "count_today": count, "actor": actor,
    }, now=now)
    return card, count >= SEEN_WARN_AT


def record_decision(
    shared_dir: Path, bot_id: str, card_id: str, *,
    decision: str, note: str = "", actor: str,
) -> dict[str, Any]:
    """D-ST6: Approve / Decline on a ``returned_for_review`` card.

    Reuses :func:`board_store.set_delegation_progress` — the same
    ``delegation`` event every other progress update already logs (see
    :data:`DECISION_TO_STATE`). Raises ``ValueError`` for anything but
    "approve"/"decline", and whatever ``set_delegation_progress`` itself
    raises for a card that is not the bot's to report on, or an over-long
    note. Raises :class:`CardNotReadyForDecision` when the card IS the
    bot's but is not currently ``returned_for_review`` — Approve/Decline
    are a reaction to a specific hand-back, not a general state setter, so
    a card still ``offered``/``in_progress`` is refused rather than
    silently reinterpreted. Caller holds ``WRITE_LOCK``, same as
    :func:`record_card_seen`.
    """
    state = DECISION_TO_STATE.get(decision)
    if state is None:
        raise ValueError(
            f"invalid decision; one of {sorted(DECISION_TO_STATE)}")
    board = board_store.load_board(shared_dir, bot_id)
    card = board_store.resolve_card(board, card_id)
    if (card.get("owner") or "me") != "bot":
        raise ValueError(
            "that card is not assigned to the bot — assign it first")
    current = (card.get("delegation") or {}).get("state")
    if current != "returned_for_review":
        raise CardNotReadyForDecision(
            "that card isn't awaiting a decision "
            f"(current state: {current or 'none'})")
    prefix = "approved" if decision == "approve" else "declined"
    note = note.strip()
    progress_note = f"{prefix} by user" + (f" — {note}" if note else "")
    return board_store.set_delegation_progress(
        shared_dir, bot_id, card_id, state=state,
        note=progress_note, actor=actor)


def today_stats(
    shared_dir: Path, bot_id: str, *, now: datetime | None = None,
) -> dict[str, int]:
    """D-ST7's "done today N · dropped N · handed off N" — counted from
    TODAY's event log, the same file :func:`board_store.append_event`
    writes, so this can never drift from what actually happened today."""
    day = _today(now)
    path = board_store.board_dir(shared_dir, bot_id) / "events" / f"{day}.jsonl"
    done = dropped = handed_off = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        event = row.get("event")
        if event == "triaged" and row.get("to") == "done":
            done += 1
        elif event == "dropped":
            dropped += 1
        elif event == "assigned" and row.get("to") == "bot":
            handed_off += 1
    return {"done_today": done, "dropped_today": dropped,
            "handed_off_today": handed_off}


def bots_plate(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """D-ST7's "Bot's plate" list for the empty state: cards the bot is
    actively working (``accepted``/``in_progress``) — a card still merely
    ``offered``, or already ``returned_for_review``, belongs in the Stack
    itself (group 1) and is never why the Stack reads as empty."""
    out = []
    for c in cards:
        if c.get("owner") != "bot":
            continue
        state = (c.get("delegation") or {}).get("state")
        if state in ("accepted", "in_progress"):
            out.append(c)
    return out


__all__ = [
    "SEEN_WARN_AT", "DECISION_TO_STATE", "SNOOZE_PRESETS",
    "stack_order", "card_why", "record_card_seen", "record_decision",
    "CardNotReadyForDecision", "today_stats", "bots_plate",
]
