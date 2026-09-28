"""board_auth.py — credential handling for the Board's user surface.

Design: ``internal/design-pa-mobile-board-2026-08-31.md`` (D-MB2).
Threat model + findings: ``internal/review-board-web-surface-2026-09.md``.

THE PROBLEM THIS MODULE EXISTS TO SOLVE. Slice 1 shipped the board token as
a ``?t=<token>`` query parameter — the only shape a bookmark-grade phone
link can carry on first contact. A query parameter is the worst place a
bearer credential can live: it lands in browser history, in the bookmark the
operator is *asked* to make, in the ``Referer`` of any outbound navigation,
and in every access-log line (F-1). It is also the credential for a WRITE
surface since slice 2.

The fix is a one-time upgrade, not a removal:

  1. ``GET /board/<bot>?t=<token>`` still works — old bookmarks must not
     break. On success the response is a **302 to the clean URL** carrying
     ``Set-Cookie`` for the same token: ``HttpOnly`` (JS cannot read it, so
     an XSS in the page cannot exfiltrate the credential), ``SameSite=Strict``
     (no cross-site request carries it — the CSRF defense the cookie's own
     ambient authority makes necessary), and ``Path``-scoped to this bot's
     two board prefixes so it is never sent to the admin plane.
  2. Every later request authenticates by cookie (page + API) or by
     ``Authorization: Bearer`` (the plugin BoardTool, curl, tests).
  3. ``?t=`` remains accepted everywhere so a bookmark, a re-share, or a
     rotated token all work on first contact without operator ceremony —
     *including* on a device still holding the cookie for the token that
     was just rotated away. That case needs the second attempt in
     :func:`authenticate`: the cookie is preferred over ``?t=``, so without
     it the freshly-minted link is refused on its very first open, which is
     the one link ``docs/help/board.md`` tells the operator to mint.

Two cookies are set, not one: a cookie carries exactly one ``Path``, and the
page (``/board/<bot>``) and its API (``/api/board/<bot>/…``) live under
different prefixes. Same name, two paths — the browser stores both and sends
whichever matches. Neither path overlaps the admin plane, so pairing state
and board state stay disjoint in both directions.

WHY NOT ``Secure``: the tailnet listener speaks plain HTTP over WireGuard
(the tailnet is the encryption layer; see ``board_listener``). Marking the
cookie ``Secure`` there would make it unsettable. The flag is applied
whenever the request itself arrived over TLS, and withheld otherwise — so an
HTTPS deployment (``tailscale serve``) gets it and the plain-HTTP tailnet
deployment still works. F-6 in the review.
"""
from __future__ import annotations

from typing import Literal, TypeVar

from flask import request
from werkzeug.wrappers import Response

from ..board_store import validate_bot_id, verify_token

#: ``redirect()`` returns a bare werkzeug Response, the page route returns a
#: flask one. Both are Responses; the TypeVar keeps the caller's own type.
ResponseT = TypeVar("ResponseT", bound=Response)

#: One cookie name, two paths (see module docstring).
BOARD_COOKIE_NAME = "evolve_board"

#: A board session outlives a phone reboot but not a lost phone forever.
#: 30 days matches "bookmark it and forget it" without making the credential
#: effectively permanent; ``evolve-admin board revoke`` is the hard kill.
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600

#: Where the credential came from. Only ``cookie`` carries ambient authority,
#: so only ``cookie`` needs the CSRF posture in :func:`same_origin_ok`.
AuthSource = Literal["bearer", "cookie", "query"]


def cookie_paths(bot_id: str) -> tuple[str, str]:
    """The two ``Path`` scopes this bot's board cookie is issued for.

    Both are exact bot-scoped prefixes: a token for one bot is never sent to
    another bot's board, and never to any admin route.
    """
    return (f"/board/{bot_id}", f"/api/board/{bot_id}")


def presented(bot_id: str) -> tuple[str | None, AuthSource | None]:
    """The credential this request presents, and how.

    Header first (explicit beats ambient), then cookie (the steady state
    after the one-time upgrade), then ``?t=`` (bookmarks and first contact).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        tok = auth[len("Bearer "):].strip()
        if tok:
            return tok, "bearer"
    tok = request.cookies.get(BOARD_COOKIE_NAME)
    if tok:
        return tok, "cookie"
    tok = request.args.get("t")
    if tok:
        return tok, "query"
    return None, None


def authenticate(shared_dir, bot_id: str) -> AuthSource | None:
    """Verify this request's board credential; return HOW it authenticated.

    Fail closed on every path — malformed bot id, unminted board, wrong
    token, no credential at all — and indistinguishably so: the caller turns
    every ``None`` into the same 401, preserving the no-oracle property the
    slice-1 tests pin.

    **Two attempts, not one.** :func:`presented` prefers the cookie, which
    is right for the steady state and wrong for exactly one moment: the
    operator rotates the token and opens the fresh ``?t=`` link on a phone
    that still holds the cookie for the token just rotated away. One attempt
    refuses that link on its first open — and the page's own recovery text
    ("ask the operator for a fresh board link") is then the wrong
    instruction for someone who is holding one. So a cookie that fails to
    verify falls through to the query token rather than ending the request.
    The result is ``"query"``, which is what makes the caller take the
    cookie-upgrade path and replace the stale cookie.

    The no-oracle property is untouched: the second attempt can only turn a
    401 into a success for a caller who already presented a valid token, and
    every failure still returns the same ``None``.
    """
    try:
        validate_bot_id(bot_id)
    except ValueError:
        return None
    token, source = presented(bot_id)
    if token and verify_token(shared_dir, bot_id, token):
        return source
    if source == "cookie":
        query = request.args.get("t")
        if query and verify_token(shared_dir, bot_id, query):
            return "query"
    return None


def same_origin_ok() -> bool:
    """CSRF posture for cookie-authenticated writes.

    ``SameSite=Strict`` is the primary defense — a browser sends the board
    cookie on no cross-site request of any kind, navigation included. This
    is the second layer, for the non-browser and legacy-browser cases: when
    an ``Origin`` header is present it must match the host the request was
    addressed to. An absent ``Origin`` is allowed (same-origin navigations
    and non-browser clients omit it); a *mismatched* one is refused.

    Bearer- and query-authenticated writes never reach this check: they
    carry no ambient authority, so there is nothing for a third-party page
    to forge.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.host or ""
    if not host:
        return False
    # Compare host authority only; the scheme may differ across a TLS
    # terminator (``tailscale serve``) that keeps the same hostname.
    return origin.split("://", 1)[-1].rstrip("/") == host


def set_board_cookies(resp: ResponseT, bot_id: str, token: str) -> ResponseT:
    """Attach the board cookie for both of this bot's board paths."""
    for path in cookie_paths(bot_id):
        resp.set_cookie(
            BOARD_COOKIE_NAME, token,
            max_age=COOKIE_MAX_AGE_SECONDS,
            path=path,
            httponly=True,
            samesite="Strict",
            secure=bool(request.is_secure),
        )
    return resp


def clear_board_cookies(resp: ResponseT, bot_id: str) -> ResponseT:
    """Expire the board cookie on both paths (used when a token stops
    verifying, so a stale cookie doesn't shadow a fresh ``?t=`` link)."""
    for path in cookie_paths(bot_id):
        resp.delete_cookie(BOARD_COOKIE_NAME, path=path, samesite="Strict")
    return resp
