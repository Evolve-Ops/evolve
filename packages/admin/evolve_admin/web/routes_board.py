"""routes_board.py — the Board's user-facing surface (slices 1+2, F8-reviewed;
the Stack view, D-ST1..9, added by the ``board-stack-view`` chip).

    GET  /board/<bot_id>                       the mobile lane board (board.html)
    GET  /board/<bot_id>/stack                 the mobile Stack, one card at a time (D-ST1)
    GET  /board/<bot_id>/manifest.webmanifest  add-to-home-screen manifest
    GET  /board/icon-192.png, /board/icon-512.png   manifest icons
    GET  /api/board/<bot_id>[?include=archived] the board JSON (ETag'd polling)
    GET  /api/board/<bot_id>/stack              the Stack's composed order (D-ST2)
    POST /api/board/<bot_id>/cards             create a card
    POST /api/board/<bot_id>/cards/<id>/move   {to_lane, reason?, snooze_until?} — tap/drag/Later
    POST /api/board/<bot_id>/cards/<id>/assign {owner} — hand it to the bot (D-BI7)
    POST /api/board/<bot_id>/cards/<id>/split  {user_part, bot_part} (D-PA4)
    POST /api/board/<bot_id>/cards/<id>/seen   swipe-up pass counter (D-ST3)
    POST /api/board/<bot_id>/cards/<id>/decision {decision: approve|decline} (D-ST6)
    GET  /api/board/<bot_id>/briefing          the Morning Board panel (D-BI6d)
    POST /api/board/<bot_id>/briefing/dismiss  close it for today

The BOT's half of the same store is ``board_bot_routes`` (``/api/board-bot/…``),
bound to the calling bot by the unix-socket peer uid rather than a token, and
registered next to the other peer-uid bot routes in ``server.py``. Both
modules write through
``board_store`` under its one process-wide lock; neither touches the store's
files directly (D-MB1: one writer, the daemon).

Design: ``internal/design-pa-mobile-board-2026-08-31.md`` (D-MB2/D-MB3).
Threat model, findings and their disposition:
``internal/review-board-web-surface-2026-09.md``.

AUTH — THE RULE THIS FILE EXISTS TO KEEP. These paths are exempt from the
admin device-cookie gate (server.py) because the board is a *user* surface,
not an operator one: pairing a phone as an admin device to look at a task
board would hand it the whole admin plane. The replacement is strictly
narrower, and strictly enforced HERE on every route, fail closed:

  * a per-bot **board token** (minted by the operator, stored only as a
    sha256 hash — ``board_store.mint_token``), presented as
    ``Authorization: Bearer <t>``, as the ``evolve_board`` cookie, or as
    ``?t=<t>`` (see ``board_auth`` for why all three, and why the query form
    is upgraded to a cookie on first contact);
  * no token file on disk ⇒ every request 401s — there is no open mode;
  * unknown bot ids 401 exactly like bad tokens (no bot-id oracle);
  * the exemption in server.py covers ``/board/`` and ``/api/board/``
    prefixes ONLY — adding any route outside those prefixes to this module
    would silently put it behind the wrong gate. Don't.

CSRF posture (F-3). Slice 2's writes carried no ambient authority: the page
fetched with ``credentials: "omit"`` and a bearer header, so there was
nothing for a third-party page to forge. Cookie auth CREATES that ambient
authority, so the cookie is ``SameSite=Strict`` (never sent cross-site, on
any request type including a top-level form POST) and cookie-authenticated
writes additionally require a same-origin ``Origin`` when one is present.
The admin CSRF gate is skipped for board paths — its double-submit token
belongs to the admin device session, which the board deliberately does not
have.

UNAUTHENTICATED BY DESIGN (F-7): the manifest and the two icons. A browser
fetches ``<link rel="manifest">`` without credentials, so gating it would
break add-to-home-screen; the manifest echoes back only the bot id the
caller already supplied and the icons are the shipped Evolve app icons.
Neither is an oracle: a syntactically valid bot id always gets a 200,
whether or not that bot exists.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Tuple, Union

from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from werkzeug.wrappers import Response as BaseResponse

from .. import board_actions
from .. import board_stack
from .. import board_store as board_store_module
from ..board_store import (
    LANES, OWNERS, assign_card, create_card, dismiss_briefing, instruct_card,
    load_board, load_briefing, move_card, record_instruction_none,
    resolve_card, split_card, token_store_readable, validate_bot_id,
    visible_cards,
)
from ..config import CANONICAL_SHARED_DIR, load_network
from ..telemetry import get_logger
from . import board_auth
from .board_limits import (
    AUTH_FAIL_LIMIT, AUTH_FAIL_WINDOW_SECONDS, MAX_WRITE_BODY,
    WRITE_LIMIT, WRITE_WINDOW_SECONDS, RateLimiter, client_key,
    read_bounded_json,
)
from .board_listener import is_board_path  # re-exported for server.py  # noqa: F401

_log = get_logger("web.routes_board")

#: ``redirect()`` returns a bare werkzeug Response rather than a flask one,
#: so the union is stated on the base class both of them satisfy.
RouteResult = Union[BaseResponse, Tuple[BaseResponse, int], Tuple[str, int, dict]]

_PAGE_PATH = Path(__file__).parent / "board.html"
_STACK_PAGE_PATH = Path(__file__).parent / "stack.html"
_ICON_DIR = Path(__file__).parent / "static" / "icons"

#: The board reuses the admin PWA icons rather than minting a second icon
#: set: one pod, one mark.
_ICON_192 = "icon-192.png"
_ICON_512 = "icon-512.png"
#: Android applies the maskable safe-zone crop to any icon declared
#: ``maskable``, so the full-bleed 512 cannot carry that purpose without
#: being clipped on the home screen — the one visual this surface exists to
#: produce. Same split as the admin manifest in ``server.py``.
_ICON_512_MASKABLE = "icon-512-maskable.png"

#: One daemon process serves both the page and the bot's BoardTool
#: (``board_bot_routes``), so the lock around load-modify-save is the STORE's,
#: not this module's — see ``board_store.WRITE_LOCK``. A per-module lock would
#: let a phone tap and a ``board.add`` interleave and lose one of them.
_WRITE_LOCK = board_store_module.WRITE_LOCK

#: F-4. Process-local by design — see board_limits for why that is the right
#: size of mechanism here.
_write_limiter = RateLimiter(WRITE_LIMIT, WRITE_WINDOW_SECONDS)
_auth_fail_limiter = RateLimiter(AUTH_FAIL_LIMIT, AUTH_FAIL_WINDOW_SECONDS)

#: Sent on every board response. ``no-referrer`` keeps a board URL (and, for
#: a pre-upgrade bookmark, its ``?t=``) out of the Referer of anything the
#: user navigates to from this page — F-5.
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _unauthorized_json() -> Tuple[Response, int]:
    """The single 401 shape. Identical for a bad token, an unminted board and
    a bot that does not exist — the no-oracle property slice 1 established."""
    resp = jsonify({"error": "board token required"})
    resp.headers.update(_SECURITY_HEADERS)
    return resp, 401


def register_board_routes(app: Flask, network_path: Path) -> None:
    """Register the board page, its PWA assets, and the board API."""

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
        )

    def _auth(bot_id: str) -> board_auth.AuthSource | None:
        """Authenticate, charging only FAILURES against the failed-auth budget.

        The budget is checked before the verify, so a client already over it
        stops paying for hash comparisons; an over-budget caller is treated
        exactly as unauthenticated (the same 401), so the limiter adds no
        oracle of its own. Successful requests — the page's 30s poll, every
        tap — never consume the budget, which is why this uses the
        over_budget/record pair rather than allow().

        A failure the CLIENT did not cause is not charged. When the token
        hash exists but the daemon cannot read it — a root-owned store, the
        2026-09-04 phone test — every request 401s no matter what token is
        presented, and charging those failures ALSO locked out the correct
        token for the rest of the window: two silences stacked on one pod
        defect. ``token_store_readable`` warns once per bot per process and
        this skips the charge; the 401 itself is unchanged (fail closed, no
        oracle — the client cannot tell the two cases apart).
        """
        who = client_key()
        if _auth_fail_limiter.over_budget(who):
            return None
        shared = _shared_dir()
        source = board_auth.authenticate(shared, bot_id)
        if source is None and token_store_readable(shared, bot_id):
            _auth_fail_limiter.record(who)
        return source

    # ── the page and its PWA assets ────────────────────────────────────────

    def _serve_board_page(bot_id: str, page_path: Path, label: str) -> RouteResult:
        """The auth/upgrade/serve sequence shared by the lane board and the
        Stack (D-ST1: "a second page over the same store"). One
        implementation of the gate rather than two, so a fix to the upgrade
        or the cookie-clear logic can never land on one page and not the
        other.
        """
        # The page itself is gated too: an unauthenticated fetch learns
        # nothing, not even the page shell.
        source = _auth(bot_id)
        if source is None:
            resp = Response("Board token required.", status=401,
                            mimetype="text/plain")
            resp.headers.update(_SECURITY_HEADERS)
            # A cookie that no longer verifies (rotated or revoked token)
            # would otherwise shadow a fresh ``?t=`` link forever, because
            # the cookie is preferred over the query parameter.
            if request.cookies.get(board_auth.BOARD_COOKIE_NAME):
                board_auth.clear_board_cookies(resp, bot_id)
            return resp

        if source == "query":
            # THE UPGRADE (F-1/F-5). A bookmark's ``?t=`` is honoured exactly
            # once per device: it becomes an HttpOnly cookie and the browser
            # is sent to the clean URL, so the credential leaves the address
            # bar, the history entry, and every subsequent access-log line.
            # ``request.path`` (never the query string) names whichever of
            # the two board pages was asked for, so one ``?t=`` link works
            # on both.
            token = request.args.get("t") or ""
            resp = redirect(request.path, code=302)
            resp.headers.update(_SECURITY_HEADERS)
            return board_auth.set_board_cookies(resp, bot_id, token)

        try:
            html = page_path.read_text(encoding="utf-8")
        except OSError as exc:  # deploy defect, not a client error
            _log.error("%s missing: %s", label, exc)
            return Response(f"{label} unavailable.", status=500,
                            mimetype="text/plain")
        resp = Response(html, mimetype="text/html")
        resp.headers.update(_SECURITY_HEADERS)
        return resp

    @app.get("/board/<bot_id>")
    def board_page(bot_id: str) -> RouteResult:
        return _serve_board_page(bot_id, _PAGE_PATH, "Board page")

    @app.get("/board/<bot_id>/stack")
    def board_stack_page(bot_id: str) -> RouteResult:
        return _serve_board_page(bot_id, _STACK_PAGE_PATH, "Stack page")

    @app.get("/board/<bot_id>/manifest.webmanifest")
    def board_manifest(bot_id: str) -> RouteResult:
        """Add-to-home-screen manifest. Unauthenticated by design — see the
        module docstring. Scoped to this bot's board so the installed icon
        opens straight onto it and in-scope navigation stays standalone."""
        try:
            validate_bot_id(bot_id)
        except ValueError:
            return Response("Not found.", status=404, mimetype="text/plain")
        body = {
            "name": "Board",
            "short_name": "Board",
            "description": "Your board — what's on you, what's on the bot.",
            "start_url": f"/board/{bot_id}",
            "scope": f"/board/{bot_id}",
            "display": "standalone",
            # board.html's --bg pair. A manifest carries one value, so this
            # is the light one; the per-theme <meta name="theme-color">
            # tags in the page override it where browsers honour them.
            "background_color": "#f4f5f2",
            "theme_color": "#f4f5f2",
            "icons": [
                {"src": "/board/icon-192.png", "sizes": "192x192",
                 "type": "image/png"},
                {"src": "/board/icon-512.png", "sizes": "512x512",
                 "type": "image/png"},
                {"src": "/board/icon-512-maskable.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
        }
        return Response(json.dumps(body, indent=2),
                        mimetype="application/manifest+json",
                        headers={"Cache-Control": "no-cache",
                                 "Referrer-Policy": "no-referrer"})

    # Static rules, not a ``/board/<name>.png`` converter: a dynamic rule
    # would overlap ``/board/<bot_id>`` and leave which one wins to
    # Werkzeug's rule ordering. Bot ids cannot contain ``.`` (board_store's
    # _BOT_ID_RE), so these two literals can never shadow a real board.
    @app.get("/board/icon-192.png")
    def board_icon_192() -> RouteResult:
        return send_from_directory(_ICON_DIR, _ICON_192, mimetype="image/png")

    @app.get("/board/icon-512.png")
    def board_icon_512() -> RouteResult:
        return send_from_directory(_ICON_DIR, _ICON_512, mimetype="image/png")

    @app.get("/board/icon-512-maskable.png")
    def board_icon_512_maskable() -> RouteResult:
        return send_from_directory(_ICON_DIR, _ICON_512_MASKABLE,
                                   mimetype="image/png")

    # ── the API ────────────────────────────────────────────────────────────

    @app.get("/api/board/<bot_id>")
    def api_board_read(bot_id: str) -> RouteResult:
        """The board the page renders.

        Settled cards older than ``ARCHIVE_AFTER_DAYS`` are filtered OUT by
        default (D-BI2: the record is kept forever, the tile leaves after 30
        days) and come back with ``?include=archived``. The filter is here,
        on the read, and never on the store: nothing is deleted, nothing is
        moved, and the dedup in :func:`board_store.find_settled_duplicate`
        still sees every one of them.
        """
        if _auth(bot_id) is None:
            return _unauthorized_json()
        shared = _shared_dir()
        board = load_board(shared, bot_id)
        include_archived = request.args.get("include") == "archived"
        board = dict(board)
        cards = visible_cards(board, include_archived=include_archived)
        # D-BI5: est_cost/disabled_reason are recomputed against CURRENT pod
        # state on every read (never frozen at add time) — one context
        # resolution per request, not per card (board_actions docstring).
        ctx = board_actions.bot_action_context(bot_id, load_network(network_path), shared)
        annotated = []
        for card in cards:
            if card.get("actions"):
                card = dict(card)
                card["actions"] = board_actions.annotate_card_actions(card, ctx)
            annotated.append(card)
        board["cards"] = annotated
        board["include_archived"] = include_archived
        body = json.dumps(board, ensure_ascii=False, sort_keys=True)
        etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers={"ETag": etag,
                                                 **_SECURITY_HEADERS})
        return Response(body, mimetype="application/json",
                        headers={"ETag": etag, **_SECURITY_HEADERS})

    def _stack_briefing(bot_id: str) -> dict[str, Any] | None:
        """D-ST2 card zero: the Morning Board app's ``GET
        /board/<bot>/briefing`` endpoint, IF that chip has landed. As of
        this chip it has not — there is no such route registered anywhere
        in this codebase yet (``morning-board-gallery-app`` is still
        in-flight) — so this always returns None and the Stack simply has
        no card zero, exactly as the design brief asks ("absent endpoint →
        no card zero, never an error"). When that chip lands, wire its own
        reader in here (most likely a plain function call — a same-process
        HTTP round-trip through this very server for its own data would be
        an odd shape); nothing else in this file needs to change.
        """
        return None

    @app.get("/api/board/<bot_id>/stack")
    def api_board_stack(bot_id: str) -> RouteResult:
        """The Stack's composed order (D-ST2) over the same store the lane
        board reads — see ``board_stack.stack_order``.

        Not ETag'd like the lane board's read: the order can change with
        wall-clock time alone (a ``later`` card becoming due) with no card
        actually edited, and a conditional 304 would hide that reordering
        from a phone that polls.
        """
        if _auth(bot_id) is None:
            return _unauthorized_json()
        shared = _shared_dir()
        board = load_board(shared, bot_id)
        cards = visible_cards(board)
        ctx = board_actions.bot_action_context(bot_id, load_network(network_path), shared)
        annotated = []
        for card in cards:
            card = dict(card)
            if card.get("actions"):
                card["actions"] = board_actions.annotate_card_actions(card, ctx)
            # D-ST6's one-line "why it's here" — computed here, not baked
            # into the store, so it always reflects the card's CURRENT
            # enrichment rather than whatever was true when it was added.
            card["why_line"] = board_stack.card_why(card)
            annotated.append(card)
        items = board_stack.stack_order(annotated, _stack_briefing(bot_id))
        # D-ST7: "You're clear" is for nothing left to look at, full stop —
        # a briefing with no cards behind it is still something to look at,
        # not an empty Stack.
        empty_state = None
        if not items:
            empty_state = {
                **board_stack.today_stats(shared, bot_id),
                "bots_plate": board_stack.bots_plate(annotated),
            }
        body = {"items": items, "empty_state": empty_state}
        resp = jsonify(body)
        resp.headers.update(_SECURITY_HEADERS)
        return resp

    def _ok(payload: dict, status: int = 200) -> RouteResult:
        resp = jsonify(payload)
        resp.headers.update(_SECURITY_HEADERS)
        return resp, status

    def _bad(message: str, status: int) -> RouteResult:
        resp = jsonify({"error": message})
        resp.headers.update(_SECURITY_HEADERS)
        return resp, status

    def _begin_write(bot_id: str) -> Tuple[Any, Any]:
        """Authenticate, rate-limit, and parse a bounded JSON body.

        Returns ``(body, None)`` on success or ``(None, response)`` with the
        refusal already shaped — so each write route reads as its own logic
        and nothing else.
        """
        source = _auth(bot_id)
        if source is None:
            return None, _unauthorized_json()
        # A cross-origin write and a too-fast write are different refusals
        # and get different codes: 403 is terminal (the caller must stop),
        # 429 says retry later. Collapsing them would tell a forged request
        # to try again.
        if source == "cookie" and not board_auth.same_origin_ok():
            _log.warning("board: cross-origin cookie write refused for %s", bot_id)
            return None, _bad("cross-origin write refused", 403)
        if not _write_limiter.allow(bot_id):
            return None, _bad("too many writes; slow down", 429)
        body = read_bounded_json(MAX_WRITE_BODY)
        if body is None:
            return None, _bad("JSON object body required", 400)
        return body, None

    @app.post("/api/board/<bot_id>/cards")
    def api_board_create(bot_id: str) -> RouteResult:
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        lane = body.get("lane") or "inbox"
        if lane not in LANES:
            return _bad(f"invalid lane; one of {list(LANES)}", 400)
        try:
            with _WRITE_LOCK:
                card = create_card(
                    _shared_dir(), bot_id,
                    title=str(body.get("title") or ""),
                    cluster=str(body.get("cluster") or "admin"),
                    lane=lane, note=str(body.get("note") or ""),
                    source="manual", actor="user",
                )
        except ValueError as exc:
            return _bad(str(exc), 400)
        return _ok({"ok": True, "card": card}, 201)

    @app.post("/api/board/<bot_id>/cards/<card_id>/move")
    def api_board_move(bot_id: str, card_id: str) -> RouteResult:
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        try:
            with _WRITE_LOCK:
                # ``reason`` is D-BI2's optional drop tap-reason; ``snooze_until``
                # is D-ST4's Later chip. The store validates each against the
                # one destination it belongs to (``dropped`` / ``later``).
                card = move_card(_shared_dir(), bot_id, card_id,
                                 str(body.get("to_lane") or ""), actor="user",
                                 reason=body.get("reason"),
                                 snooze_until=body.get("snooze_until"))
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc), 400)
        return _ok({"ok": True, "card": card})

    @app.post("/api/board/<bot_id>/cards/<card_id>/assign")
    def api_board_assign(bot_id: str, card_id: str) -> RouteResult:
        """Set a card's owner (D-BI7). ``owner: bot`` is an OFFER — the bot
        accepts or declines; the page only queues, exactly as the retired Bot
        lane did when a card was dragged into it."""
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        owner = str(body.get("owner") or "")
        if owner not in OWNERS:
            return _bad(f"invalid owner; one of {list(OWNERS)}", 400)
        try:
            with _WRITE_LOCK:
                card = assign_card(_shared_dir(), bot_id, card_id, owner,
                                   actor="user")
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc), 400)
        return _ok({"ok": True, "card": card})

    @app.post("/api/board/<bot_id>/cards/<card_id>/split")
    def api_board_split(bot_id: str, card_id: str) -> RouteResult:
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        try:
            with _WRITE_LOCK:
                kid_user, kid_bot = split_card(
                    _shared_dir(), bot_id, card_id,
                    user_part=str(body.get("user_part") or ""),
                    bot_part=str(body.get("bot_part") or ""), actor="user")
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc), 400)
        return _ok({"ok": True, "cards": [kid_user, kid_bot]})

    @app.post("/api/board/<bot_id>/cards/<card_id>/instruct")
    def api_board_instruct(bot_id: str, card_id: str) -> RouteResult:
        """D-BI5: a tap is an instruction, not a chat.

        Body is one of ``{"action_id": <id>}`` (optionally ``"confirm":
        true`` — D-BI6's second tap for an email-derived target) or
        ``{"none": true}`` — "none of these" (§6 Q3's learning signal, no
        card mutation). Gating (rung/integration, cost) is recomputed HERE
        against current pod state — never trusted from a client-supplied
        value, and never from whatever was frozen on the card at add time.
        """
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        shared = _shared_dir()
        if body.get("none"):
            try:
                with _WRITE_LOCK:
                    card = record_instruction_none(shared, bot_id, card_id, actor="user")
            except KeyError:
                return _bad("no such card", 404)
            return _ok({"ok": True, "card": card})
        action_id = str(body.get("action_id") or "")
        if not action_id:
            return _bad("action_id (or none: true) is required", 400)
        try:
            with _WRITE_LOCK:
                board = load_board(shared, bot_id)
                try:
                    resolved = resolve_card(board, card_id)
                except KeyError:
                    return _bad("no such card", 404)
                except ValueError as exc:
                    return _bad(str(exc), 400)
                real_id = resolved["id"]
                action = board_actions.find_action(resolved, action_id)
                if action is None:
                    return _bad("no such action on this card", 404)
                ctx = board_actions.bot_action_context(
                    bot_id, load_network(network_path), shared)
                live = board_actions.annotate_action(action, ctx)
                if live.get("disabled_reason"):
                    return _bad(
                        f"that action is not available: {live['disabled_reason']}",
                        409)
                if live.get("requires_confirm") and not body.get("confirm"):
                    resp = jsonify({
                        "error": "this action needs confirmation before it runs",
                        "requires_confirm": True,
                    })
                    resp.headers.update(_SECURITY_HEADERS)
                    return resp, 409
                card = instruct_card(
                    shared, bot_id, real_id, action_id=action_id,
                    action_label=str(action.get("label") or action_id),
                    est_cost=live.get("est_cost"), actor="user")
        except KeyError:
            return _bad("no such card", 404)
        return _ok({"ok": True, "card": card})

    @app.post("/api/board/<bot_id>/cards/<card_id>/seen")
    def api_board_seen(bot_id: str, card_id: str) -> RouteResult:
        """D-ST3: swipe-up = pass. Reordering the Stack is the page's own
        client-side business (the card goes to the back of THIS session's
        order) — this just logs the pass and reports whether today's count
        just hit the third-pass warning. Body ``{"undo": true}`` reverses
        the toast's own increment (see ``board_stack.record_card_seen``).
        """
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        try:
            with _WRITE_LOCK:
                card, warn = board_stack.record_card_seen(
                    _shared_dir(), bot_id, card_id, actor="user",
                    undo=bool(body.get("undo")))
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc), 400)
        return _ok({"ok": True, "card": card, "warn_third_pass": warn})

    @app.post("/api/board/<bot_id>/cards/<card_id>/decision")
    def api_board_decision(bot_id: str, card_id: str) -> RouteResult:
        """D-ST6: Approve / Decline on a ``returned_for_review`` card — the
        two large targets the Stack shows instead of ``actions[]`` once the
        bot has handed a card back. Body: ``{"decision": "approve"|"decline",
        "note": "..."}``. See ``board_stack.record_decision`` for the state
        mapping and why no new event type was needed.
        """
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        try:
            with _WRITE_LOCK:
                card = board_stack.record_decision(
                    _shared_dir(), bot_id, card_id,
                    decision=str(body.get("decision") or ""),
                    note=str(body.get("note") or ""), actor="user")
        except KeyError:
            return _bad("no such card", 404)
        except ValueError as exc:
            return _bad(str(exc), 400)
        return _ok({"ok": True, "card": card})

    @app.get("/api/board/<bot_id>/briefing")
    def api_board_briefing(bot_id: str) -> RouteResult:
        """The Morning Board panel (D-BI6d): the SAME composition already
        sent as the daily message, never recomposed for the page. ``None``
        when the app has not stocked this bot yet — a day-one board with no
        panel is a normal state, not an error."""
        if _auth(bot_id) is None:
            return _unauthorized_json()
        briefing = load_briefing(_shared_dir(), bot_id)
        if briefing is None:
            return _ok({"ok": True, "briefing": None})
        return _ok({"ok": True, "briefing": {
            "date": briefing.get("date"), "text": briefing.get("text"),
            "dismissed": bool(briefing.get("dismissed_at")),
        }})

    @app.post("/api/board/<bot_id>/briefing/dismiss")
    def api_board_briefing_dismiss(bot_id: str) -> RouteResult:
        """Close the panel for its own day only — per-user-per-day (D-BI6e);
        v1's board is single-user, so this is per-bot-per-day. Dismissing a
        stale (already-rotated) day is a no-op, not an error: the panel the
        tap was aimed at is already gone."""
        _body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        try:
            with _WRITE_LOCK:
                briefing = dismiss_briefing(_shared_dir(), bot_id)
        except KeyError:
            return _ok({"ok": True, "dismissed": False})
        return _ok({"ok": True, "dismissed": True, "date": briefing.get("date")})


    @app.post("/api/board/<bot_id>/cards/<card_id>/approval")
    def api_board_approval(bot_id: str, card_id: str) -> RouteResult:
        """D-BI4 §5: approve or decline an externally-visible act the bot
        proposed. Body: ``{"granted": true|false}``.

        Delegates the whole decision to ``board_worker.resume_delegation`` —
        it records the grant/decline (with the USER as actor; see
        board_worker.py's module-docstring deviation 4) AND the resulting
        terminal ``board.progress``. Resuming on a grant is a bounded,
        deterministic finish (the draft was already produced and its cost
        already charged when the approval was requested), so doing it inline
        on this request is safe — unlike the LLM steps in
        ``run_delegation``, which only the subscriber ever dispatches.
        """
        body, refusal = _begin_write(bot_id)
        if refusal is not None:
            return refusal
        if "granted" not in body or not isinstance(body.get("granted"), bool):
            return _bad("granted (bool) is required", 400)
        from ..board_worker import resume_delegation
        try:
            with _WRITE_LOCK:
                try:
                    resolved = resolve_card(load_board(_shared_dir(), bot_id), card_id)
                except KeyError:
                    return _bad("no such card", 404)
                result = resume_delegation(
                    _shared_dir(), bot_id, resolved["id"],
                    granted=bool(body["granted"]), actor="user")
                if result.get("outcome") == "skipped":
                    return _bad("that card has no pending approval to resolve", 409)
                card = resolve_card(load_board(_shared_dir(), bot_id), resolved["id"])
        except ValueError as exc:
            return _bad(str(exc), 400)
        return _ok({"ok": True, "card": card})


__all__ = ["register_board_routes", "is_board_path"]
