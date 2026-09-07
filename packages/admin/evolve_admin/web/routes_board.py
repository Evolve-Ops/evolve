"""routes_board.py — the Board's user-facing surface (slices 1+2, F8-reviewed).

    GET  /board/<bot_id>                       the mobile page (board.html)
    GET  /board/<bot_id>/manifest.webmanifest  add-to-home-screen manifest
    GET  /board/icon-192.png, /board/icon-512.png   manifest icons
    GET  /api/board/<bot_id>                   the board JSON (ETag'd polling)
    POST /api/board/<bot_id>/cards             create a card
    POST /api/board/<bot_id>/cards/<id>/move   {to_lane} — tap-to-move
    POST /api/board/<bot_id>/cards/<id>/split  {user_part, bot_part} (D-PA4)

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
import threading
from pathlib import Path
from typing import Any, Tuple, Union

from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from werkzeug.wrappers import Response as BaseResponse

from ..board_store import (
    LANES, create_card, load_board, move_card, split_card,
    token_store_readable, validate_bot_id,
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

#: One daemon process serves both the page and (later) the bot's BoardTool;
#: a lock around load-modify-save keeps concurrent taps from losing writes.
_WRITE_LOCK = threading.Lock()

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

    @app.get("/board/<bot_id>")
    def board_page(bot_id: str) -> RouteResult:
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
            token = request.args.get("t") or ""
            resp = redirect(f"/board/{bot_id}", code=302)
            resp.headers.update(_SECURITY_HEADERS)
            return board_auth.set_board_cookies(resp, bot_id, token)

        try:
            html = _PAGE_PATH.read_text(encoding="utf-8")
        except OSError as exc:  # deploy defect, not a client error
            _log.error("board page missing: %s", exc)
            return Response("Board page unavailable.", status=500,
                            mimetype="text/plain")
        resp = Response(html, mimetype="text/html")
        resp.headers.update(_SECURITY_HEADERS)
        return resp

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
        if _auth(bot_id) is None:
            return _unauthorized_json()
        board = load_board(_shared_dir(), bot_id)
        body = json.dumps(board, ensure_ascii=False, sort_keys=True)
        etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers={"ETag": etag,
                                                 **_SECURITY_HEADERS})
        return Response(body, mimetype="application/json",
                        headers={"ETag": etag, **_SECURITY_HEADERS})

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
                card = move_card(_shared_dir(), bot_id, card_id,
                                 str(body.get("to_lane") or ""), actor="user")
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


__all__ = ["register_board_routes", "is_board_path"]
