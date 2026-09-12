"""Bot-facing board routes — ``/api/board-bot/…`` (D-MB4 chat parity).

Design: ``internal/design-pa-mobile-board-2026-08-31.md`` D-MB1 (**one
writer: the admin daemon**) + D-MB4 (the verbs), as amended by
``internal/design-pa-board-interface-v2-2026-09-04.md`` D-BI7 (lanes are
*when*, ``owner`` is *who*) and the ``enrichment{}`` block from
``internal/design-pa-lists-and-board-2026-09-01.md`` §2.

This is the half of the board the BOT talks to. The phone talks to
``routes_board`` (per-user board token); the bot talks to these routes over
the admin daemon's unix socket, and the two write the SAME store through the
SAME ``board_store`` writers under its one process-wide lock. There is no
plugin-side store access at all — a bot with no daemon has no board, by
construction (D-MB1).

    GET  /api/board-bot/cards[?cluster=&lane=&owner=&limit=&include=archived]
                                                               board.list
    POST /api/board-bot/cards                                  board.add
    POST /api/board-bot/cards/<id>/move     {to_lane,reason?}  board.move
    POST /api/board-bot/cards/<id>/assign   {owner}             board.assign
    POST /api/board-bot/cards/<id>/progress {state,note,cost_to_date?}
                                                               board.progress

THE IDENTITY MODEL (mirrors ``directory_bot_routes`` / ``google_bot_routes``):

  The calling bot is bound **server-side** from the kernel-reported peer uid
  of the unix-socket connection (``peer_auth.resolve_peer_bot_id``) — NEVER
  from a path segment, a query parameter or a body field. That is why these
  paths carry no ``<bot_id>``: there is nothing for a bot to name, so a bot
  cannot reach another bot's board even by trying. A TCP request (the admin
  UI's binding) and an unrecognized peer uid both get 403.

  This is also why the bot side needs no board token. The token is the
  PHONE's credential — a bearer secret handed to a device. Minting one for
  the bot would put a long-lived board credential in a bot-readable file for
  no gain, when the socket already proves who the caller is below the LLM.

WHY A SEPARATE PATH PREFIX. ``/api/board/`` is the user surface: it is exempt
from the admin device-cookie gate (it carries its own token auth) and it is
one of the two prefixes the tailnet listener will route. ``/api/board-bot/``
is neither — the listener 404s it, so these routes are reachable only on the
loopback and unix-socket bindings, and the peer-uid check then means only the
socket can actually use them. Two auth models, two namespaces, no path where
one file's rule silently governs the other's routes.

CAPS. ``board.list`` is a per-turn read: whatever it returns rides in the
model's context. So the wire is bounded here (``MAX_LIST_LIMIT``) and the
plugin bounds its rendering again (``BoardTool.ts``) — the board must not
become a per-turn history tax (context economy CE-2/CE-3).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Tuple

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from ..board_store import (
    DELEGATION_STATES, LANES, OWNERS, WRITE_LOCK, assign_card,
    create_card, find_settled_duplicate, load_board, move_card,
    set_delegation_progress, visible_cards,
)
from ..config import CANONICAL_SHARED_DIR, load_network
from . import peer_auth
from .board_limits import MAX_WRITE_BODY, read_bounded_json

log = logging.getLogger(__name__)

#: Rows returned when the caller names no ``limit``.
DEFAULT_LIST_LIMIT = 60

#: Hard ceiling on rows in one ``board.list`` response, whatever the caller
#: asks for. A board holds up to ``board_store.MAX_CARDS`` (5000); handing a
#: model 5000 rows because it forgot a filter is the per-turn tax CE-2 exists
#: to prevent, and no legitimate turn reads more of a task board than this.
MAX_LIST_LIMIT = 200

#: The fields one listed card carries. Deliberately NOT the whole card: the
#: note, the enrichment block and the event history are what make a card big,
#: and a list is for choosing which card to act on, not for reading them all.
#: The bot fetches detail by acting on an id.
_LIST_FIELDS = ("id", "title", "lane", "owner", "cluster")


def _card_row(card: dict[str, Any]) -> dict[str, Any]:
    """One card projected down to the list shape."""
    row = {k: card.get(k) for k in _LIST_FIELDS}
    row["owner"] = card.get("owner") or "me"
    delegation = card.get("delegation")
    if isinstance(delegation, dict) and delegation.get("state"):
        row["delegation"] = delegation["state"]
    if card.get("enrichment"):
        # The KEYS only — enough for the bot to know a fact is on file and
        # ask for the card, without paying for the values every list.
        row["enriched"] = sorted(card["enrichment"])
    return row


def register_board_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the bot-facing board API on ``app``."""

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
        )

    def _caller() -> "str | None":
        """The bot this request is bound to, or None (the caller must 403).

        Never reads a bot id from the request — see the module docstring.
        """
        return peer_auth.resolve_peer_bot_id(network_path)

    def _forbidden() -> Tuple[Any, int]:
        return jsonify({
            "error": "this endpoint is reachable only by a bot over the "
                     "admin-daemon unix socket",
        }), 403

    def _bad(message: str, status: int = 400) -> Tuple[Any, int]:
        return jsonify({"error": message}), status

    def _body() -> Tuple[Any, Any]:
        """``(body, None)`` or ``(None, refusal)`` — one bounded-JSON shape."""
        parsed = read_bounded_json(MAX_WRITE_BODY)
        if parsed is None:
            return None, _bad("JSON object body required")
        return parsed, None

    @app.get("/api/board-bot/cards")
    def board_bot_list() -> ResponseReturnValue:
        bot_id = _caller()
        if bot_id is None:
            return _forbidden()
        board = load_board(_shared_dir(), bot_id)
        # Same default as the phone (D-BI2): settled cards older than 30 days
        # are off the board. The bot asking "is this already on there?" wants
        # the same answer the user sees — and when it wants the full record
        # (stocking dedup), ``?include=archived`` gives it.
        cards = visible_cards(
            board, include_archived=request.args.get("include") == "archived")
        for key, allowed in (("cluster", None), ("lane", LANES), ("owner", OWNERS)):
            wanted = (request.args.get(key) or "").strip()
            if not wanted:
                continue
            if allowed is not None and wanted not in allowed:
                return _bad(f"invalid {key}; one of {list(allowed)}")
            if key == "owner":
                cards = [c for c in cards if (c.get("owner") or "me") == wanted]
            else:
                cards = [c for c in cards if c.get(key) == wanted]
        total = len(cards)
        try:
            limit = int(request.args.get("limit") or DEFAULT_LIST_LIMIT)
        except ValueError:
            return _bad("limit must be an integer")
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        return jsonify({
            "bot_id": bot_id,
            "total": total,
            "cap": MAX_LIST_LIMIT,
            "cards": [_card_row(c) for c in cards[:limit]],
        })

    @app.post("/api/board-bot/cards")
    def board_bot_create() -> ResponseReturnValue:
        bot_id = _caller()
        if bot_id is None:
            return _forbidden()
        body, refusal = _body()
        if refusal is not None:
            return refusal
        lane = str(body.get("lane") or "inbox")
        if lane not in LANES:
            return _bad(f"invalid lane; one of {list(LANES)}")
        owner = str(body.get("owner") or "me")
        if owner not in OWNERS:
            return _bad(f"invalid owner; one of {list(OWNERS)}")
        # STOCKING DEDUP (D-BI2). A card carrying an upstream id was stocked
        # from a source, not typed by a person — nobody types a calendar event
        # id — so this is the one door where "a dismissed calendar/email card
        # never returns" can be enforced without second-guessing a deliberate
        # re-add. The dedup is against ``done`` and ``dropped`` INCLUDING
        # archived cards: the tile ages out at 30 days, the decision does not.
        source_id = str(body.get("source_id") or "").strip()
        settled = None
        try:
            # Check and create UNDER ONE LOCK. Read-then-write across the lock
            # boundary is a check that can go stale: two stocking runs posting
            # the same calendar event both read "not a duplicate" and both
            # write, which is precisely the double-add the dedup exists to
            # stop. The extra load inside the lock is one small JSON read.
            with WRITE_LOCK:
                if source_id:
                    settled = find_settled_duplicate(
                        load_board(_shared_dir(), bot_id),
                        title=str(body.get("title") or ""),
                        source_id=source_id, when=body.get("when"))
                if settled is None:
                    card = create_card(
                        _shared_dir(), bot_id,
                        title=str(body.get("title") or ""),
                        cluster=str(body.get("cluster") or "admin"),
                        lane=lane, note=str(body.get("note") or ""),
                        owner=owner,
                        # Free text, but recorded: the learning loop reads
                        # WHERE a card came from, and "manual" would be a lie
                        # on a card the bot stocked from a calendar.
                        source=str(body.get("source") or "bot"),
                        # The upstream id (calendar event, email message) the
                        # stocking dedup matches on — see board_store.D-BI2.
                        source_id=body.get("source_id"),
                        enrichment=body.get("enrichment"),
                        actor="bot",
                    )
        except ValueError as exc:
            return _bad(str(exc))
        if settled is not None:
            # 200, not an error: nothing went wrong, and the caller is told
            # plainly which card already answered this.
            return jsonify({
                "ok": True, "skipped": "already settled",
                "lane": settled.get("lane"), "card": settled,
            })
        return jsonify({"ok": True, "card": card}), 201

    @app.post("/api/board-bot/cards/<card_id>/move")
    def board_bot_move(card_id: str) -> ResponseReturnValue:
        bot_id = _caller()
        if bot_id is None:
            return _forbidden()
        body, refusal = _body()
        if refusal is not None:
            return refusal
        try:
            with WRITE_LOCK:
                card = move_card(_shared_dir(), bot_id, card_id,
                                 str(body.get("to_lane") or ""), actor="bot",
                                 reason=body.get("reason"))
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc))
        return jsonify({"ok": True, "card": card})

    @app.post("/api/board-bot/cards/<card_id>/assign")
    def board_bot_assign(card_id: str) -> ResponseReturnValue:
        bot_id = _caller()
        if bot_id is None:
            return _forbidden()
        body, refusal = _body()
        if refusal is not None:
            return refusal
        owner = str(body.get("owner") or "")
        if owner not in OWNERS:
            return _bad(f"invalid owner; one of {list(OWNERS)}")
        try:
            with WRITE_LOCK:
                card = assign_card(_shared_dir(), bot_id, card_id, owner,
                                   actor="bot")
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc))
        return jsonify({"ok": True, "card": card})

    @app.post("/api/board-bot/cards/<card_id>/progress")
    def board_bot_progress(card_id: str) -> ResponseReturnValue:
        """Bot-only: report on a card that was handed to the bot.

        Bot-only in the strong sense — the endpoint exists ONLY on this
        peer-uid-bound prefix, so the phone has no route to it at all, and
        ``set_delegation_progress`` additionally refuses a card the bot does
        not own. Progress is a report on a hand-over; without one there is
        nothing to report.
        """
        bot_id = _caller()
        if bot_id is None:
            return _forbidden()
        body, refusal = _body()
        if refusal is not None:
            return refusal
        state = str(body.get("state") or "")
        if state not in DELEGATION_STATES:
            return _bad(f"invalid state; one of {list(DELEGATION_STATES)}")
        try:
            with WRITE_LOCK:
                card = set_delegation_progress(
                    _shared_dir(), bot_id, card_id, state=state,
                    note=str(body.get("note") or ""),
                    cost_to_date=body.get("cost_to_date"), actor="bot")
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc))
        return jsonify({"ok": True, "card": card})


__all__ = ["register_board_bot_routes", "DEFAULT_LIST_LIMIT", "MAX_LIST_LIMIT"]
